"""ChatGPT plan inference through the official Responses API.

Sign in with ChatGPT authorizes *ChatGPT plan usage*, not API billing. This
provider therefore:

* sends the OAuth access token as the bearer credential (never an API key);
* uses ``POST /v1/responses`` with ``store: false`` and ``stream: true``;
* omits every field the plan route rejects (``temperature``,
  ``max_output_tokens``, ``previous_response_id``, ``metadata``, ...);
* treats ``response.completed`` as the only success signal;
* reports usage-limit, eligibility, revocation and capability errors exactly,
  and never falls back to the API-key provider or another billing path.

The account store owns refresh, rotation and revocation. This module owns the
wire protocol and the streaming shape that ``ProviderChatBackend`` already
expects.
"""
from __future__ import annotations

import contextlib
import time
from typing import Callable, Optional
from urllib.parse import urlparse

from giscode.providers.base import (
    CHATGPT,
    Provider,
    ProviderError,
    ProviderModel,
    ProviderStatus,
    join_url,
)
from giscode.providers.chatgpt_auth import (
    PLAN_SCOPE,
    ChatGPTAccount,
    ChatGPTAccountStore,
    plan_error,
)
from giscode.providers.redaction import redact_text
from giscode.providers.streaming import (
    SSE_DONE_EVENT,
    StreamCancelled,
    StreamDelta,
    iter_sse_objects,
    stream_response,
)
from giscode.providers.usage import UsageLedger

#: The only host this provider talks to in production. Tests replace it to
#: point at a deterministic loopback server.
API_HOST = "api.openai.com"
API_ENDPOINT = "https://api.openai.com"


class ChatGPTPlanProvider(Provider):
    """Provider for ChatGPT plan usage over the Responses API."""

    kind = CHATGPT
    label = "ChatGPT plan"
    supports_streaming = True
    stream_format = "sse"
    supports_download = False
    supports_pause_resume = False
    supports_delete = False
    download_note = (
        "ChatGPT plan models are served by OpenAI; there is nothing to download."
    )
    allow_vision = False

    def __init__(
        self,
        endpoint: str = API_ENDPOINT,
        *,
        account_id: str = "",
        account_store: Optional[ChatGPTAccountStore] = None,
        usage_ledger: Optional[UsageLedger] = None,
        max_request_chars: int = 200_000,
        **kwargs,
    ) -> None:
        kwargs.setdefault("allow_remote", True)
        super().__init__(endpoint or API_ENDPOINT, **kwargs)
        host = (urlparse(self.endpoint).hostname or "").strip().lower()
        if host != API_HOST:
            raise ValueError(
                "ChatGPT plan inference can only use https://api.openai.com. "
                "Use the OpenAI-compatible provider for another endpoint."
            )
        self._account_id = str(account_id or "")
        self._store = account_store or ChatGPTAccountStore()
        self._ledger = usage_ledger or UsageLedger()
        self._max_request_chars = max(1_000, int(max_request_chars or 200_000))
        self._models_cache: tuple[float, str, list[ProviderModel]] | None = None

    # ------------------------------------------------------------- account
    @property
    def account_store(self) -> ChatGPTAccountStore:
        return self._store

    def account(self) -> Optional[ChatGPTAccount]:
        if self._account_id:
            return self._store.get_account(self._account_id)
        return self._store.active_account()

    def use_account(self, account_id: str) -> None:
        """Switch which stored registration this provider uses."""
        self._account_id = str(account_id or "")
        self._models_cache = None

    def cache_identity(self) -> tuple[str, str]:
        """Identity that partitions router caches; never a credential.

        ``ModelRouter`` includes this in its backend key so a backend bound to
        one account is never reused after the selected account changes.
        """
        account = self.account()
        return (self.kind, account.account_id if account else "")

    def _account_or_error(self) -> ChatGPTAccount:
        account = self.account()
        if account is None:
            raise ProviderError(
                "No ChatGPT account is connected.",
                action=(
                    "Use 'Continue with ChatGPT' in Settings to sign in. This is "
                    "separate from an OpenAI API key."
                ),
            )
        return account

    def _require_plan(self) -> tuple[ChatGPTAccount, str]:
        """Return the account and a fresh access token, or refuse."""
        account = self._account_or_error()
        if account.needs_reauth:
            raise ProviderError(
                "The ChatGPT plan session needs a new sign-in.",
                action="Use Continue with ChatGPT to reauthorize this account.",
            )
        if not account.plan_usage_enabled:
            raise ProviderError(
                "ChatGPT plan usage is not enabled for this account.",
                action=(
                    "Reauthorize and grant 'chatgpt.tokens.use.direct', or choose "
                    "the separate OpenAI API key provider. GisCode will not switch "
                    "to paid API billing automatically."
                ),
            )
        token = self._store.access_token(account.account_id)
        return account, token

    def _safe_error(self, exc: ProviderError) -> ProviderError:
        secrets = self.secret_values()
        error = ProviderError(
            redact_text(str(exc), secrets),
            action=redact_text(str(getattr(exc, "action", "") or ""), secrets),
        )
        code = str(getattr(exc, "code", "") or "")
        if code:
            setattr(error, "code", code)
        return error

    def secret_values(self) -> tuple[str, ...]:
        account = self.account()
        if account is None:
            return ()
        try:
            tokens = self._store._load_tokens(account.account_id)  # noqa: SLF001
        except Exception:  # noqa: BLE001 - redaction is best effort
            return ()
        return tuple(
            value
            for value in (
                tokens.get("access_token"),
                tokens.get("refresh_token"),
                tokens.get("id_token"),
            )
            if value
        )

    # ------------------------------------------------------------- status
    def status(self) -> ProviderStatus:
        """Local authentication state only: opening the window sends nothing."""
        account = self.account()
        endpoint = self.secret_free_endpoint()
        if account is None:
            return ProviderStatus(
                kind=self.kind,
                label=self.label,
                endpoint=endpoint,
                available=False,
                detail="Not signed in. Use Continue with ChatGPT.",
            )
        if account.needs_reauth:
            return ProviderStatus(
                kind=self.kind,
                label=self.label,
                endpoint=endpoint,
                available=False,
                detail=f"{account.label}: sign in again ({account.last_error or 'session ended'}).",
            )
        if not account.plan_usage_enabled:
            return ProviderStatus(
                kind=self.kind,
                label=self.label,
                endpoint=endpoint,
                available=False,
                detail=f"{account.label}: signed in, but ChatGPT plan usage was not granted.",
            )
        return ProviderStatus(
            kind=self.kind,
            label=self.label,
            endpoint=endpoint,
            available=True,
            detail=f"{account.label}: ChatGPT plan usage granted.",
        )

    # ------------------------------------------------------------- models
    def list_models(self) -> list[ProviderModel]:
        account, token = self._require_plan()
        cached = self._models_cache
        if cached is not None and cached[1] == account.account_id and time.monotonic() - cached[0] < 60.0:
            return list(cached[2])
        try:
            body = self._store._http_json(  # noqa: SLF001 - shared transport
                join_url(self.endpoint, "v1/models"),
                headers={"Authorization": f"Bearer {token}"},
                timeout=max(self.timeout, 20.0),
            )
        except ProviderError as exc:
            raise self._safe_error(exc) from exc
        status = int(body.get("_status", 200) or 200)
        if status != 200:
            raise self._safe_error(_body_error(body))
        entries = body.get("models")
        if not isinstance(entries, list):
            entries = body.get("data")
        models: list[ProviderModel] = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("visibility") or "list") != "list":
                continue
            identifier = str(entry.get("slug") or entry.get("id") or "").strip()
            if not identifier:
                continue
            models.append(
                ProviderModel(
                    id=identifier,
                    provider=self.kind,
                    name=str(entry.get("display_name") or identifier),
                    capabilities=("text", "chat", "coding"),
                    installed=True,
                    description="Available for your ChatGPT plan",
                )
            )
        self._models_cache = (time.monotonic(), account.account_id, models)
        return list(models)

    # ------------------------------------------------------------- requests
    def _prepare_input(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """Split system text into ``instructions``; keep the rest as input.

        The plan route rejects an explicit ``{"type": "message", "role":
        "system"}`` item, so system content must travel in ``instructions``.
        """
        instructions: list[str] = []
        turns: list[dict] = []
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user")
            content = message.get("content")
            if role == "system":
                if isinstance(content, str):
                    instructions.append(content)
                elif isinstance(content, list):
                    instructions.extend(
                        str(part.get("text") or "")
                        for part in content
                        if isinstance(part, dict)
                    )
                continue
            turns.append({"role": role, "content": content})
        if not turns:
            turns = [{"role": "user", "content": ""}]
        return "\n\n".join(text for text in instructions if text), turns

    def _prepare_payload(self, model: str, messages: list[dict]) -> dict:
        instructions, turns = self._prepare_input(messages)
        payload = {
            "model": model,
            "input": turns,
            "store": False,
            "stream": True,
        }
        if instructions:
            payload["instructions"] = instructions
        size = len(str(payload))
        if size > self._max_request_chars:
            raise ProviderError(
                f"The ChatGPT plan request is too large ({size} characters; limit "
                f"{self._max_request_chars}).",
                action="Reduce the task context or raise max_request_chars.",
            )
        return payload

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
        """Aggregate the required stream into one answer.

        The plan route requires ``stream: true`` on every request. ``max_tokens``
        and ``temperature`` are accepted for interface compatibility but are not
        sent, because the preview rejects them.
        """
        pieces: list[str] = []
        for delta in self.stream_chat(
            model,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            request_id=request_id,
        ):
            if delta.text:
                pieces.append(delta.text)
        return "".join(pieces)

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
        if not str(model or "").strip():
            raise ProviderError(
                "No ChatGPT plan model was selected.",
                action="Choose a model that the account's model list returns.",
            )
        account, token = self._require_plan()
        payload = self._prepare_payload(model, messages)

        def _stop_requested() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                return True
            if should_stop is not None:
                try:
                    return bool(should_stop())
                except Exception:  # noqa: BLE001 - a broken predicate is not a cancel
                    return False
            return False

        try:
            with stream_response(
                join_url(self.endpoint, "v1/responses"),
                payload=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                timeout=timeout or max(self.timeout, 180.0),
                idle_timeout=idle_timeout,
                cancel_event=cancel_event,
                should_stop=should_stop,
            ) as chunks:
                yield from self._decode_events(
                    chunks,
                    model=model,
                    request_id=request_id,
                    stop_requested=_stop_requested,
                )
        except ProviderError as exc:
            status = _http_status_in(str(exc))
            if status == 401:
                raise self._safe_error(
                    ProviderError(
                        "ChatGPT rejected the access token.",
                        action="Use Continue with ChatGPT to sign in again. GisCode "
                        "will not fall back to an API key.",
                    )
                ) from exc
            if status == 403:
                raise self._safe_error(
                    ProviderError(
                        "ChatGPT plan usage was not permitted for this account.",
                        action="Check the selected account and workspace, or use the "
                        "separate OpenAI API key provider.",
                    )
                ) from exc
            if status == 429:
                raise self._safe_error(
                    ProviderError(
                        "ChatGPT plan usage is rate limited.",
                        action="Wait before retrying, and check ChatGPT Settings, "
                        "Usage. GisCode will not switch to API-key billing.",
                    )
                ) from exc
            if status in (500, 502, 503, 504):
                raise self._safe_error(
                    ProviderError(
                        f"ChatGPT plan is temporarily unavailable (HTTP {status}).",
                        action="Retry in a moment. GisCode will not switch to an "
                        "API key or another provider.",
                    )
                ) from exc
            raise self._safe_error(exc) from exc

    def _decode_events(
        self,
        chunks,
        *,
        model: str,
        request_id: Optional[str],
        stop_requested: Optional[Callable[[], bool]] = None,
    ):
        completed = False
        for event in iter_sse_objects(chunks):
                if stop_requested is not None and stop_requested():
                    raise StreamCancelled()
                if event is SSE_DONE_EVENT:
                    break
                if not isinstance(event, dict):
                    continue
                if event.get("error") and not event.get("type"):
                    raise self._error_for(event)
                kind = str(event.get("type") or "")
                if kind == "response.output_text.delta":
                    text = str(event.get("delta") or "")
                    if text:
                        if stop_requested is not None and stop_requested():
                            raise StreamCancelled()
                        yield StreamDelta(text=text)
                    continue
                if kind == "response.completed":
                    response = event.get("response")
                    block = response if isinstance(response, dict) else {}
                    usage = block.get("usage") if isinstance(block.get("usage"), dict) else {}
                    tokens = _output_tokens(usage)
                    completed = True
                    self._ledger.record(
                        provider=self.kind,
                        model=model,
                        request_id=str(request_id or ""),
                        usage=usage,
                        finish_reason="completed",
                    )
                    yield StreamDelta(
                        finish_reason="stop",
                        tokens=tokens,
                        usage=usage,
                        done=True,
                    )
                    break
                if kind == "response.incomplete":
                    raise ProviderError(
                        "The ChatGPT plan answer was incomplete.",
                        action="Try again with a smaller request. Partial text was kept.",
                    )
                if kind == "response.failed":
                    raise self._error_for(event)
        if not completed:
            raise ProviderError(
                "The ChatGPT plan stream ended before response.completed.",
                action="Try again. The partial answer is not a finished reply.",
            )

    def _error_for(self, event: dict) -> ProviderError:
        response = event.get("response")
        block = response if isinstance(response, dict) else event
        error = block.get("error") if isinstance(block, dict) else None
        code = ""
        param = ""
        detail = ""
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            param = str(error.get("param") or "")
            detail = str(error.get("message") or "")
        elif isinstance(error, str):
            code = error
        if not code:
            code = str(block.get("code") or "") if isinstance(block, dict) else ""
        exc = plan_error(code, param=param, detail=detail)
        return self._safe_error(exc)

    # ------------------------------------------------------------- usage
    def usage_snapshot(self) -> dict:
        return self._ledger.snapshot()

    def usage_description(self) -> str:
        data = self._ledger.snapshot()
        if not data["requests"]:
            return "no ChatGPT plan requests yet"
        return (
            f"{data['requests']} ChatGPT plan request(s), "
            f"{data['prompt_tokens']} input tokens, "
            f"{data['completion_tokens']} output tokens (plan usage, not API billing)"
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        account = self.account()
        who = (account.email or account.account_id) if account else "not signed in"
        return f"<ChatGPTPlanProvider {who} at {self.secret_free_endpoint()}>"


def _http_status_in(message: str) -> int:
    """Extract an HTTP status from the shared transport's error text."""
    import re

    match = re.search(r"HTTP (\d{3})", message or "")
    try:
        return int(match.group(1)) if match else 0
    except (TypeError, ValueError):
        return 0


def _output_tokens(usage: dict) -> int:
    try:
        return int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def _body_error(body: dict) -> ProviderError:
    status = int(body.get("_status", 200) or 200)
    if status == 401:
        return ProviderError(
            "ChatGPT rejected the access token.",
            action="Use Continue with ChatGPT to sign in again. GisCode will not "
            "fall back to an API key.",
        )
    if status == 403:
        code = str(body.get("code") or "")
        if code:
            return plan_error(code)
        return ProviderError(
            "ChatGPT plan usage was not permitted for this account or region.",
            action="Check the selected account and workspace, or use the separate "
            "OpenAI API key provider.",
        )
    if status == 429:
        return ProviderError(
            "ChatGPT plan usage is rate limited.",
            action="Wait before retrying, and check ChatGPT Settings, Usage. "
            "GisCode will not switch to API-key billing.",
        )
    if status in (500, 502, 503, 504):
        return ProviderError(
            f"ChatGPT plan is temporarily unavailable (HTTP {status}).",
            action="Retry in a moment. GisCode will not switch to an API key or "
            "another provider.",
        )
    detail = ""
    error = body.get("error")
    if isinstance(error, dict):
        detail = str(error.get("message") or error.get("code") or "")
    return ProviderError(
        f"ChatGPT plan inference failed (HTTP {status})"
        + (f": {detail}" if detail else ""),
        action="Check the ChatGPT connection in Settings, then try again.",
    )


def select_chatgpt_account(
    settings, account_id: str, *, store: Optional[ChatGPTAccountStore] = None
) -> ChatGPTAccount:
    """Bind the selected account to the ChatGPT provider configuration.

    The active-account pointer and the provider's ``account_id`` are updated
    together, so the next router/backend built after this call authenticates as
    the selected account instead of the previous one. No token is read, written
    or displayed here. If configuration cannot be saved, the previous active
    account is restored so the picker and the bound provider cannot diverge.
    """
    from giscode.providers.profiles import ProfileStore
    from giscode.providers.registry import ProviderRegistry
    from giscode.providers.settings_adapter import as_settings_source

    source = as_settings_source(settings)
    if source is None:
        raise ValueError("settings must support get/set/save")
    store = store or ChatGPTAccountStore()
    previous = store.active_account_id()
    account = store.set_active(str(account_id or ""))
    try:
        registry = ProviderRegistry(source)
        configs = registry.configs()
        config = dict(configs.get(CHATGPT) or {})
        config.update(
            {
                "kind": CHATGPT,
                "label": str(config.get("label") or "ChatGPT plan"),
                "endpoint": str(config.get("endpoint") or API_ENDPOINT),
                "allow_remote": True,
                "account_id": account.account_id,
            }
        )
        active = ProfileStore(source).active_for("coding")
        if active is not None and active.provider == CHATGPT:
            config["enabled"] = True
        configs[CHATGPT] = config
        registry.save_configs(configs)
    except Exception:
        with contextlib.suppress(Exception):
            if previous:
                store.set_active(previous)
        raise
    return account


def configure_chatgpt_profile(
    settings,
    *,
    model: str,
    account_id: str = "",
    active: bool = True,
    label: str = "ChatGPT plan coding",
):
    """Persist a ``chatgpt`` coding profile without any token.

    Mirrors :func:`giscode.providers.openai.configure_openai_profile`: consent
    and identity selection live in settings; credentials stay in the protected
    account store. No network request is made here.
    """
    from giscode.providers.profiles import ProfileStore
    from giscode.providers.registry import ProviderRegistry
    from giscode.providers.settings_adapter import as_settings_source

    source = as_settings_source(settings)
    if source is None:
        raise ValueError("settings must support get/set/save")
    model = str(model or "").strip()
    if not model:
        raise ValueError("a ChatGPT plan model id is required")
    registry = ProviderRegistry(source)
    configs = registry.configs()
    config = dict(configs.get(CHATGPT) or {})
    config.update(
        {
            "kind": CHATGPT,
            "label": "ChatGPT plan",
            "endpoint": API_ENDPOINT,
            "enabled": bool(active),
            "allow_remote": True,
            "account_id": str(account_id or ""),
            "model": model,
        }
    )
    configs[CHATGPT] = config
    registry.save_configs(configs)

    profiles = ProfileStore(source)
    existing = next(
        (
            profile
            for profile in profiles.for_purpose("coding")
            if profile.provider == CHATGPT
        ),
        None,
    )
    if existing is None:
        profile = profiles.create(
            "coding",
            provider=CHATGPT,
            model=model,
            label=label,
            make_active=False,
        )
    else:
        profile = profiles.update(existing.id, model=model, label=label)
    if active:
        profiles.set_active(profile.id)
    return profile


__all__ = [
    "API_ENDPOINT",
    "API_HOST",
    "ChatGPTPlanProvider",
    "PLAN_SCOPE",
    "configure_chatgpt_profile",
    "select_chatgpt_account",
]
