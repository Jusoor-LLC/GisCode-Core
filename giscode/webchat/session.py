"""Session ownership for provider-neutral AI Web Chat browsers."""
from __future__ import annotations

from typing import Callable

from .adapters import WebChatAdapter
from .host import WebChatBrowserHost
from .registry import WebChatRegistry

HostFactory = Callable[[WebChatAdapter], WebChatBrowserHost]


class WebChatSessionManager:
    """Own at most one host per provider; profiles remain provider-scoped."""

    def __init__(
        self,
        registry: WebChatRegistry,
        *,
        host_factory: HostFactory | None = None,
    ) -> None:
        self._registry = registry
        self._host_factory = host_factory
        self._hosts: dict[str, WebChatBrowserHost] = {}

    def register(self, provider_id: str, host: WebChatBrowserHost) -> None:
        self._hosts[provider_id] = host

    def host_for(self, provider_id: str) -> WebChatBrowserHost | None:
        provider = self._registry.get(provider_id)
        if provider is None or not provider.available:
            return None
        existing = self._hosts.get(provider.id)
        if existing is not None:
            return existing
        adapter = self._registry.adapter(provider.id)
        if adapter is None:
            return None
        host = (
            self._host_factory(adapter)
            if self._host_factory is not None
            else adapter.create_host()
        )
        self._hosts[provider.id] = host
        return host

    def stop(self, provider_id: str) -> None:
        host = self._hosts.get(provider_id)
        if host is None:
            return
        try:
            host.shutdown()
        finally:
            self._hosts.pop(provider_id, None)

    def shutdown(self) -> None:
        for host in list(self._hosts.values()):
            try:
                host.shutdown()
            except Exception:
                pass
        self._hosts.clear()
