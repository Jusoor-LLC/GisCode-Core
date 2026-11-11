"""Backward-compatible ChatGPT-branded AI Web Chat view.

The shared implementation lives in :mod:`giscode.gui.ai_web_chat`.  This module
pins the ChatGPT provider so existing imports keep working; the main window
uses the provider-neutral view and its selector.
"""
from __future__ import annotations

from giscode.browser.host import ChatGptBrowserHost  # noqa: F401 - compatibility
from giscode.gui.ai_web_chat import (  # noqa: F401 - re-exported for callers
    AIWebChatView,
    SettingsBridge,
)
from giscode.webchat.providers import CHATGPT_PROVIDER
from giscode.webchat.registry import DEFAULT_REGISTRY


class ChatGPTBrowserView(AIWebChatView):
    """The ChatGPT provider pinned inside the shared AI Web Chat view."""

    def __init__(
        self,
        parent,
        *,
        project_root_provider=None,
        on_guidance=None,
        host=None,
    ) -> None:
        super().__init__(
            parent,
            project_root_provider=project_root_provider,
            on_guidance=on_guidance,
            registry=DEFAULT_REGISTRY,
            host=host,
            provider_id=CHATGPT_PROVIDER.id,
        )


__all__ = ["AIWebChatView", "ChatGPTBrowserView", "ChatGptBrowserHost", "SettingsBridge"]
