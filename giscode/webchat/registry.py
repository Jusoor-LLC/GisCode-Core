"""Provider registry and persistent AI Web Chat provider selection."""
from __future__ import annotations

from typing import Iterable

from .adapters import ADAPTERS, WebChatAdapter
from .providers import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_PREFERENCE_KEY,
    PROVIDERS,
    WebChatProvider,
    get_provider,
)


class WebChatRegistry:
    """Ordered provider definitions plus their available adapters."""

    def __init__(self, providers: Iterable[WebChatProvider] | None = None) -> None:
        self._providers = tuple(providers or PROVIDERS)
        self._by_id = {provider.id: provider for provider in self._providers}
        self._adapters: dict[str, WebChatAdapter] = {}
        for provider in self._providers:
            if not provider.available:
                continue
            adapter_class = ADAPTERS.get(provider.id, WebChatAdapter)
            self._adapters[provider.id] = adapter_class(provider)

    def all(self) -> tuple[WebChatProvider, ...]:
        return self._providers

    def get(self, provider_id: str | None) -> WebChatProvider | None:
        return self._by_id.get(str(provider_id or "").strip())

    def require(self, provider_id: str | None) -> WebChatProvider:
        provider = self.get(provider_id)
        if provider is None:
            raise KeyError(f"unknown AI Web Chat provider: {provider_id!r}")
        return provider

    def default(self) -> WebChatProvider:
        return self.get(DEFAULT_PROVIDER_ID) or self._providers[0]

    def adapter(self, provider_id: str | None) -> WebChatAdapter | None:
        return self._adapters.get(str(provider_id or "").strip())

    def choices(self) -> list[tuple[str, str]]:
        return [(provider.id, provider.display_name) for provider in self._providers]

    def available(self) -> tuple[WebChatProvider, ...]:
        return tuple(provider for provider in self._providers if provider.available)


DEFAULT_REGISTRY = WebChatRegistry()


def default_registry() -> WebChatRegistry:
    return DEFAULT_REGISTRY


class ProviderSelection:
    """Read/write the selected provider through the profile settings object."""

    def __init__(self, settings, registry: WebChatRegistry | None = None) -> None:
        self._settings = settings
        self._registry = registry or DEFAULT_REGISTRY

    def _stored_id(self) -> str:
        try:
            raw = self._settings.get(PROVIDER_PREFERENCE_KEY, DEFAULT_PROVIDER_ID)
        except Exception:
            return DEFAULT_PROVIDER_ID
        return str(raw or DEFAULT_PROVIDER_ID).strip() or DEFAULT_PROVIDER_ID

    def selected(self) -> WebChatProvider:
        provider = self._registry.get(self._stored_id())
        return provider or self._registry.default()

    def select(self, provider_id: str) -> WebChatProvider:
        provider = self._registry.require(provider_id)
        setter = getattr(self._settings, "set", None)
        if not callable(setter):
            raise RuntimeError("the profile settings cannot be saved")
        setter(PROVIDER_PREFERENCE_KEY, provider.id)
        return provider
