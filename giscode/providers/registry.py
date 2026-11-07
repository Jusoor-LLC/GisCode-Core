"""Provider configuration, construction and status reporting.

Provider settings live in ``config.json`` under ``providers`` so they survive
restarts and are visible to a person editing the file. Built-in providers exist
even when the key is absent, which keeps existing installs working: an older
config with no ``providers`` block simply gets the two defaults.

Nothing here contacts a network except the explicit ``status()``/``all_models()``
calls, so constructing a registry is safe in tests and at startup.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from giscode.providers.base import (
    DEFAULT_ENDPOINTS,
    LMSTUDIO,
    OLLAMA,
    OPENAI_COMPAT,
    PROVIDER_KINDS,
    Provider,
    ProviderError,
    ProviderModel,
    ProviderStatus,
)
from giscode.providers.lmstudio import LMStudioProvider
from giscode.providers.ollama import OllamaProvider
from giscode.providers.openai_compat import OpenAICompatibleProvider


def default_provider_configs() -> dict:
    return {
        OLLAMA: {
            "kind": OLLAMA,
            "label": "Ollama",
            "endpoint": DEFAULT_ENDPOINTS[OLLAMA],
            "enabled": True,
            "allow_remote": False,
            "api_key_env": "",
        },
        LMSTUDIO: {
            "kind": LMSTUDIO,
            "label": "LM Studio",
            "endpoint": DEFAULT_ENDPOINTS[LMSTUDIO],
            "enabled": True,
            "allow_remote": False,
            "api_key_env": "",
        },
    }


_BUILDERS = {
    OLLAMA: OllamaProvider,
    LMSTUDIO: LMStudioProvider,
    OPENAI_COMPAT: OpenAICompatibleProvider,
}


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return cleaned or "provider"


class ProviderRegistry:
    """Reads provider settings and builds live provider objects."""

    def __init__(self, settings, *, timeout: float = 5.0) -> None:
        self._settings = settings
        self._timeout = timeout

    # -- configuration -----------------------------------------------------
    def configs(self) -> dict:
        stored = self._settings.get("providers", None)
        merged = default_provider_configs()
        if isinstance(stored, dict):
            for key, value in stored.items():
                if isinstance(value, dict) and value.get("kind") in PROVIDER_KINDS:
                    merged[str(key)] = dict(value)
        return merged

    def save_configs(self, configs: dict) -> None:
        self._settings.set("providers", configs)
        self._settings.save()

    def enabled_ids(self) -> list[str]:
        return [
            key
            for key, value in self.configs().items()
            if str(value.get("enabled", True)).lower() not in ("false", "0", "no")
        ]

    # -- construction ------------------------------------------------------
    def get(self, provider_id: str) -> Optional[Provider]:
        config = self.configs().get(provider_id)
        if not config:
            return None
        return self._build(provider_id, config)

    def _build(self, provider_id: str, config: dict) -> Optional[Provider]:
        kind = str(config.get("kind") or "")
        builder = _BUILDERS.get(kind)
        if builder is None:
            return None
        key_env = str(config.get("api_key_env") or "").strip()
        api_key = os.environ.get(key_env, "") if key_env else ""
        try:
            if kind == OPENAI_COMPAT:
                return builder(
                    str(config.get("endpoint") or ""),
                    label=str(config.get("label") or provider_id),
                    timeout=self._timeout,
                    api_key=api_key,
                    allow_remote=bool(config.get("allow_remote", False)),
                    allow_vision=bool(config.get("vision", False)),
                )
            return builder(
                str(config.get("endpoint") or DEFAULT_ENDPOINTS.get(kind, "")),
                timeout=self._timeout,
                api_key=api_key,
                allow_remote=bool(config.get("allow_remote", False)),
                label=str(config.get("label") or ""),
                allow_vision=bool(config.get("vision", False)),
            )
        except ValueError:
            # A stored endpoint that no longer validates is a configuration
            # problem, not a crash: skip it and let the UI show the others.
            return None

    def list(self) -> list[Provider]:
        providers: list[Provider] = []
        for provider_id in self.enabled_ids():
            provider = self.get(provider_id)
            if provider is not None:
                providers.append(provider)
        return providers

    # -- live status -------------------------------------------------------
    def statuses(self) -> list[ProviderStatus]:
        results: list[ProviderStatus] = []
        for provider_id in self.enabled_ids():
            provider = self.get(provider_id)
            if provider is None:
                results.append(
                    ProviderStatus(
                        kind=str((self.configs().get(provider_id) or {}).get("kind") or ""),
                        label=provider_id,
                        endpoint="",
                        available=False,
                        detail="The configured endpoint is not usable. Check its URL.",
                    )
                )
                continue
            try:
                results.append(provider.status())
            except ProviderError as exc:
                results.append(
                    ProviderStatus(
                        kind=provider.kind,
                        label=provider.label,
                        endpoint=provider.secret_free_endpoint(),
                        available=False,
                        detail=str(exc),
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive
                results.append(
                    ProviderStatus(
                        kind=provider.kind,
                        label=provider.label,
                        endpoint=provider.secret_free_endpoint(),
                        available=False,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
        return results

    def all_models(self) -> list[ProviderModel]:
        models: list[ProviderModel] = []
        for provider_id in self.enabled_ids():
            provider = self.get(provider_id)
            if provider is None:
                continue
            try:
                models.extend(provider.list_models())
            except ProviderError:
                # One unreachable provider must not hide the others' models.
                continue
        return models

    # -- editing -----------------------------------------------------------
    def add_custom(
        self,
        label: str,
        endpoint: str,
        *,
        api_key_env: str = "",
        allow_remote: bool = False,
    ) -> str:
        """Add an OpenAI-compatible endpoint and return its generated id."""
        configs = self.configs()
        base = _slug(label)
        provider_id = base
        suffix = 2
        while provider_id in configs:
            provider_id = f"{base}-{suffix}"
            suffix += 1
        configs[provider_id] = {
            "kind": OPENAI_COMPAT,
            "label": label or "Local endpoint",
            "endpoint": endpoint,
            "enabled": True,
            "allow_remote": bool(allow_remote),
            "api_key_env": api_key_env or "",
        }
        # Validate by building it: an unusable URL must fail here, not at use.
        provider = self._build(provider_id, configs[provider_id])
        if provider is None:
            raise ValueError("That endpoint is not a usable http(s) address.")
        self.save_configs(configs)
        return provider_id

    def set_enabled(self, provider_id: str, enabled: bool) -> None:
        configs = self.configs()
        if provider_id not in configs:
            raise KeyError(f"unknown provider {provider_id!r}")
        configs[provider_id]["enabled"] = bool(enabled)
        self.save_configs(configs)

    def remove(self, provider_id: str) -> None:
        """Remove a custom provider. Built-ins are disabled, never deleted."""
        configs = self.configs()
        if provider_id not in configs:
            raise KeyError(f"unknown provider {provider_id!r}")
        if provider_id in (OLLAMA, LMSTUDIO):
            configs[provider_id]["enabled"] = False
        else:
            configs.pop(provider_id, None)
        self.save_configs(configs)

    def provider_for_model(self, provider_id: str) -> Optional[Provider]:
        return self.get(provider_id)
