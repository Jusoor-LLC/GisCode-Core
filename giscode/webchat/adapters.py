"""Provider-specific adapters over the shared AI Web Chat handoff engine.

The shared modules know nothing about a provider beyond its metadata.  An
adapter supplies the provider object and creates the host appropriate to its
``browser_mode``: the isolated WebView2 host for a qualified embedded provider,
or the system-browser host for one GisCode does not embed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .context import WebChatContext, build_web_chat_context
from .external import ExternalBrowserHost
from .guidance import WebChatGuidance, build_web_chat_guidance
from .host import WebChatBrowserHost
from .providers import WebChatProvider


class WebChatAdapter:
    """Minimal provider adapter contract used by the shared UI."""

    provider: WebChatProvider

    def __init__(self, provider: WebChatProvider) -> None:
        self.provider = provider

    @property
    def capabilities(self):
        return self.provider.capabilities

    def build_context(
        self,
        project_root: str | Path | None,
        selected_files: Iterable[str | Path],
        question: str,
        **kwargs: Any,
    ) -> WebChatContext:
        return build_web_chat_context(
            self.provider, project_root, selected_files, question, **kwargs
        )

    def build_guidance(
        self,
        raw_response: str,
        context: WebChatContext,
        *,
        source_url: str | None = None,
        **kwargs: Any,
    ) -> WebChatGuidance:
        return build_web_chat_guidance(
            raw_response,
            context,
            self.provider,
            source_url=source_url or self.provider.url,
            **kwargs,
        )

    def create_host(self, **kwargs: Any):
        """Create the host this provider's browser mode actually needs."""
        if self.provider.uses_embedded_browser:
            return WebChatBrowserHost(provider=self.provider, **kwargs)
        return ExternalBrowserHost(self.provider, **kwargs)


class ChatGPTAdapter(WebChatAdapter):
    """The first fully qualified embedded browser adapter."""


#: Adapters that need provider-specific behaviour.  Every other available
#: provider uses the shared adapter above; an unavailable provider gets none.
ADAPTERS: dict[str, type[WebChatAdapter]] = {
    "chatgpt": ChatGPTAdapter,
}
