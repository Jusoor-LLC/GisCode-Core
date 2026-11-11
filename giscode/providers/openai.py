"""Official OpenAI API provider (API key, not a ChatGPT subscription).

This module deliberately targets the documented OpenAI REST API at
``https://api.openai.com``.  A ChatGPT Plus/Pro subscription is a different
product: it does not grant API access, and GisCode does not harvest browser
cookies, passwords, MFA codes or subscription tokens to pretend otherwise.
The user supplies an API key through the environment or the protected local
store.

Paid calls are opt-in.  Constructing this provider performs no network request;
``paid_calls_allowed`` must be true before ``chat`` or ``stream_chat`` will
send anything billable.  The provider lists models with ``GET /v1/models`` and
uses ``POST /v1/chat/completions`` so the same router, planner and coding loop
that serve local Ollama/llama.cpp targets can use it unchanged.

Usage is recorded from the provider's own ``usage`` block.  Cost is only
estimated when the user configured current input/output prices; GisCode does
not ship a stale price table that would make an estimate look authoritative.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from giscode.providers.base import (
    OPENAI,
    Provider,
    ProviderError,
    ProviderModel,
    ProviderStatus,
    join_url,
)
from giscode.providers.credentials import (
    CredentialStore,
    EnvironmentCredentialStore,
    default_credential_store,
    masked_credential_status,
)
from giscode.providers.http import request_json
from giscode.providers.lmstudio import _first_message_content, _stream_openai_chat
from giscode.providers.redaction import redact_messages, redact_text
from giscode.providers.usage import UsageLedger

OFFICIAL_ENDPOINT = "https://api.openai.com"
OFFICIAL_HOST = "api.openai.com"

#: Shown by the settings UI and included in refusals.  Keep this factual: the
#: user pays OpenAI through their API account, not through GisCode.
BILLING_DISCLOSURE = (
    "OpenAI API requests are billed to your OpenAI API account. A ChatGPT "
    "subscription is not API access and does not include these requests. "
    "GisCode makes no paid request until you explicitly enable billable calls."
)


class OpenAIProvider(Provider):
    """An OpenAI API provider that is disabled for billable calls by default."""

    kind = OPENAI
    label = "OpenAI"
    supports_streaming = True
    stream_format = "sse"
    supports_download = False
    supports_pause_resume = False
    supports_delete = False
    download_note = "OpenAI models are served by the API; there is nothing to download."
    # Images are not sent unless a later model-capability check says they are
    # supported. GisCode does not infer vision from a model name.
    allow_vision = False

    def __init__(
        self,
        endpoint: str = OFFICIAL_ENDPOINT,
        *,
        api_key: str = "",
        api_key_env: str = "OPENAI_API_KEY",
        credential_store: Optional[CredentialStore] = None,
        paid_calls_allowed: bool = False,
        pricing: Optional[dict] = None,
        usage_path: Optional[Path | str] = None,
        usage_ledger: Optional[UsageLedger] = None,
        max_request_chars: int = 200_000,
        **kwargs,
    ) -> None:
        kwargs.setdefault("allow_remote", True)
        super().__init__(endpoint or OFFICIAL_ENDPOINT, **kwargs)
        host = (urlparse(self.endpoint).hostname or "").strip().lower()
        if host != OFFICIAL_HOST:
            raise ValueError(
                "The official OpenAI provider can only use https://api.openai.com. "
                "Use a separate OpenAI-compatible provider for another endpoint."
            )
        self._api_key_explicit = str(api_key or "")
        self._api_key_env = str(api_key_env or "OPENAI_API_KEY")
        self._credentials = credential_store or default_credential_store()
        self._paid_calls_allowed = bool(paid_calls_allowed)
        self._ledger = usage_ledger or UsageLedger(usage_path, pricing=pricing)
        self._max_request_chars = max(1_000, int(max_request_chars or 200_000))
        if pricing is not None and usage_ledger is None:
            self._ledger.set_pricing(pricing)

    # ------------------------------------------------------------- credentials
    @property
    def paid_calls_allowed(self) -> bool:
        return self._paid_calls_allowed

    def set_paid_calls_allowed(self, allowed: bool) -> None:
        """Change consent for future calls without touching the key."""
        self._paid_calls_allowed = bool(allowed)

    def credential_status(self) -> str:
        if self._api_key_explicit or os.environ.get(self._api_key_env):
            return "set in the environment"
        return masked_credential_status(self._credentials)

    def _credential(self) -> str:
        """Resolve the API key at call time without storing it in settings."""
        if self._api_key_explicit:
            return self._api_key_explicit
        env_value = os.environ.get(self._api_key_env, "")
        if env_value:
            return str(env_value)
        try:
            return self._credentials.get("openai", "")
        except Exception:
            return ""

    def headers(self) -> dict:
        """Build auth headers from the live credential source."""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = self._credential()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _require_key(self) -> str:
        key = self._credential()
        if not key:
            raise ProviderError(
                "OpenAI API key is not configured.",
                action=(
                    "Set OPENAI_API_KEY in the environment, or save the key in "
                    "Settings using the protected Windows credential store."
                ),
            )
        return key

    def secret_values(self) -> tuple[str, ...]:
        key = self._credential()
        return (key,) if key else ()

    def _safe_error(self, exc: ProviderError) -> ProviderError:
        """Keep a server's error text from echoing the key back to a log or UI."""
        secrets = self.secret_values()
        return ProviderError(
            redact_text(str(exc), secrets),
            action=redact_text(str(getattr(exc, "action", "") or ""), secrets),
        )

    def _prepare_messages(self, messages: list[dict]) -> list[dict]:
        """Redact credentials and cap the outbound request size.

        This is intentionally local to the provider as well as the router
        boundary: a caller that invokes the provider directly must not be able
        to bypass redaction.
        """
        cleaned = redact_messages(messages, self.secret_values())
        try:
            size = len(json.dumps(cleaned, ensure_ascii=False))
        except (TypeError, ValueError):
            size = 0
        if size > self._max_request_chars:
            raise ProviderError(
                f"The OpenAI request is too large ({size} characters; limit "
                f"{self._max_request_chars}).",
                action="Reduce the task context or raise max_request_chars.",
            )
        return cleaned

    def _require_paid_consent(self) -> None:
        if not self._paid_calls_allowed:
            raise ProviderError(
                "OpenAI billable API calls are disabled.",
                action=(
                    "Enable 'Allow billable OpenAI API calls' in Settings after "
                    "reviewing the billing disclosure. No request was sent."
                ),
            )

    # ------------------------------------------------------------- status/models
    def status(self) -> ProviderStatus:
        if not self._credential():
            return ProviderStatus(
                kind=self.kind,
                label=self.label,
                endpoint=self.secret_free_endpoint(),
                available=False,
                detail="OpenAI API key is not configured.",
            )
        if not self._paid_calls_allowed:
            return ProviderStatus(
                kind=self.kind,
                label=self.label,
                endpoint=self.secret_free_endpoint(),
                available=False,
                detail=(
                    "API key is present, but billable calls are disabled. "
                    "Review the billing disclosure and enable them in Settings."
                ),
            )
        try:
            models = self.list_models()
        except ProviderError as exc:
            return ProviderStatus(
                kind=self.kind,
                label=self.label,
                endpoint=self.secret_free_endpoint(),
                available=False,
                detail=str(self._safe_error(exc)),
            )
        return ProviderStatus(
            kind=self.kind,
            label=self.label,
            endpoint=self.secret_free_endpoint(),
            available=True,
            detail="OpenAI API answered.",
            model_count=len(models),
        )

    def list_models(self) -> list[ProviderModel]:
        # A request is not sent before the user has explicitly enabled the
        # remote API. Listing is free, but it still leaves this computer.
        self._require_paid_consent()
        self._require_key()
        try:
            body = request_json(
                join_url(self.endpoint, "v1/models"),
                timeout=self.timeout,
                headers=self.headers(),
            )
        except ProviderError as exc:
            raise self._safe_error(exc) from exc
        entries = (body or {}).get("data") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            return []
        models: list[ProviderModel] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identifier = str(entry.get("id") or "").strip()
            if not identifier:
                continue
            models.append(
                ProviderModel(
                    id=identifier,
                    provider=self.kind,
                    name=identifier,
                    capabilities=("text", "chat"),
                    installed=True,
                    description="Available from the official OpenAI API",
                )
            )
        return sorted(models, key=lambda item: item.id.lower())

    # ------------------------------------------------------------- completions
    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        max_tokens: int = 256,
        temperature: float = 0.2,
        timeout: Optional[float] = None,
        request_id: Optional[str] = None,
    ) -> str:
        self._require_paid_consent()
        self._require_key()
        if not model:
            raise ProviderError(
                "No OpenAI model was selected.",
                action="Choose a model that GET /v1/models returns.",
            )
        prepared = self._prepare_messages(messages)
        try:
            body = request_json(
                join_url(self.endpoint, "v1/chat/completions"),
                method="POST",
                payload={
                    "model": model,
                    "messages": prepared,
                    "max_completion_tokens": max(1, int(max_tokens or 1)),
                    "temperature": float(temperature),
                    "stream": False,
                },
                timeout=timeout or max(self.timeout, 180.0),
                headers=self.headers(),
            )
        except ProviderError as exc:
            raise self._safe_error(exc) from exc
        content = _first_message_content(body, provider="OpenAI")
        usage = body.get("usage") if isinstance(body, dict) else None
        self._ledger.record(
            provider=self.kind,
            model=model,
            request_id=str(request_id or ""),
            usage=usage if isinstance(usage, dict) else {},
        )
        return content

    def stream_chat(
        self,
        model: str,
        messages: list[dict],
        *,
        max_tokens: int = 256,
        temperature: float = 0.2,
        timeout: Optional[float] = None,
        idle_timeout: Optional[float] = None,
        cancel_event=None,
        should_stop=None,
        request_id: Optional[str] = None,
    ):
        self._require_paid_consent()
        self._require_key()
        if not model:
            raise ProviderError(
                "No OpenAI model was selected.",
                action="Choose a model that GET /v1/models returns.",
            )
        stream = _stream_openai_chat(
            self,
            model,
            self._prepare_messages(messages),
            max_tokens=max(1, int(max_tokens or 1)),
            temperature=temperature,
            timeout=timeout,
            idle_timeout=idle_timeout,
            cancel_event=cancel_event,
            should_stop=should_stop,
            include_usage=True,
            token_field="max_completion_tokens",
        )
        try:
            for delta in stream:
                if delta.done:
                    self._ledger.record(
                        provider=self.kind,
                        model=model,
                        request_id=str(request_id or ""),
                        usage=delta.usage or {},
                        finish_reason=delta.finish_reason,
                    )
                yield delta
        except ProviderError as exc:
            raise self._safe_error(exc) from exc

    # ------------------------------------------------------------- accounting
    def usage_snapshot(self) -> dict:
        return self._ledger.snapshot()

    def usage_description(self) -> str:
        return self._ledger.describe()

    def reset_usage(self) -> None:
        self._ledger.reset()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<OpenAIProvider {self.secret_free_endpoint()} "
            f"paid_calls={'on' if self._paid_calls_allowed else 'off'}>"
        )


def build_openai_provider(
    *,
    endpoint: str = OFFICIAL_ENDPOINT,
    api_key: str = "",
    credential_store: Optional[CredentialStore] = None,
    paid_calls_allowed: bool = False,
    pricing: Optional[dict] = None,
    usage_path: Optional[Path | str] = None,
    timeout: float = 5.0,
) -> OpenAIProvider:
    """Small construction helper used by setup code and tests."""
    return OpenAIProvider(
        endpoint,
        api_key=api_key,
        credential_store=credential_store,
        paid_calls_allowed=paid_calls_allowed,
        pricing=pricing,
        usage_path=usage_path,
        timeout=timeout,
    )


def default_environment_store() -> CredentialStore:
    return EnvironmentCredentialStore({"openai": "OPENAI_API_KEY"})


def configure_openai_profile(
    settings,
    *,
    model: str,
    active: bool = True,
    paid_calls_allowed: bool = False,
    label: str = "OpenAI coding",
):
    """Persist the provider consent and one coding profile without a secret.

    This is the setup path used by the Settings UI and callable from a small
    headless script.  It writes only non-secret configuration; the API key is
    resolved from the environment or the protected credential store at call
    time.  It never makes a network request.
    """
    from giscode.providers.profiles import ProfileStore
    from giscode.providers.registry import ProviderRegistry
    from giscode.providers.settings_adapter import as_settings_source

    source = as_settings_source(settings)
    if source is None:
        raise ValueError("settings must support get/set/save")
    model = str(model or "").strip()
    if not model:
        raise ValueError("an OpenAI model id is required")
    registry = ProviderRegistry(source)
    configs = registry.configs()
    config = dict(configs.get(OPENAI) or {})
    config.update(
        {
            "kind": OPENAI,
            "label": "OpenAI",
            "endpoint": OFFICIAL_ENDPOINT,
            "enabled": bool(active),
            "allow_remote": True,
            "api_key_env": "OPENAI_API_KEY",
            "paid_calls_allowed": bool(paid_calls_allowed),
            "model": model,
        }
    )
    configs[OPENAI] = config
    registry.save_configs(configs)

    profiles = ProfileStore(source)
    existing = next(
        (
            profile
            for profile in profiles.for_purpose("coding")
            if profile.provider == OPENAI
        ),
        None,
    )
    if existing is None:
        profile = profiles.create(
            "coding",
            provider=OPENAI,
            model=model,
            label=label,
            make_active=False,
        )
    else:
        profile = profiles.update(existing.id, model=model, label=label)
    if active:
        profiles.set_active(profile.id)
    return profile
