"""Backward-compatible ChatGPT host over the provider-neutral browser host.

``ChatGptBrowserHost`` pins the ChatGPT provider and keeps the one-argument URL
validator and profile-directory helpers the existing ChatGPT tests call.
"""
from __future__ import annotations

from giscode.webchat.host import (  # noqa: F401 - compatibility re-exports
    BrowserHostError,
    BrowserRuntimeStatus,
    BrowserStartError,
    BrowserUnavailableError,
    WebChatBrowserHost,
    _process_alive,
    _process_exitcode,
    default_profile_root as _default_profile_root,
    probe_webview2_runtime,
)
from giscode.webchat.host import ProcessFactory  # noqa: F401 - type re-export
from giscode.webchat.host import _validate_url as _validate_provider_url
from giscode.webchat.providers import CHATGPT_PROVIDER
from giscode.webchat.guidance import CHATGPT_URL


def default_profile_root():
    """The existing isolated ChatGPT profile directory."""
    return _default_profile_root(CHATGPT_PROVIDER)


def _validate_url(url: str) -> str:
    """Validate a URL against ChatGPT's official hosts only."""
    return _validate_provider_url(url, CHATGPT_PROVIDER)


class ChatGptBrowserHost(WebChatBrowserHost):
    """Own at most one visible ChatGPT WebView2 process at a time."""

    def __init__(
        self,
        profile_root=None,
        *,
        probe=probe_webview2_runtime,
        process_factory=None,
        url: str = CHATGPT_URL,
    ) -> None:
        super().__init__(
            CHATGPT_PROVIDER,
            profile_root,
            probe=probe,
            process_factory=process_factory,
            url=url,
        )


__all__ = [
    "BrowserHostError",
    "BrowserRuntimeStatus",
    "BrowserStartError",
    "BrowserUnavailableError",
    "ChatGptBrowserHost",
    "default_profile_root",
    "probe_webview2_runtime",
]
