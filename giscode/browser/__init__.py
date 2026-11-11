"""Core-native AI Web Chat browser and guidance bridge (ChatGPT compatibility)."""
from __future__ import annotations

from giscode.webchat import (  # noqa: F401 - provider-neutral surface
    BrowserHostError,
    BrowserRuntimeStatus,
    BrowserStartError,
    BrowserUnavailableError,
    ChatGptContext,
    ChatGptGuidance,
    ContextBuildError,
    GuidanceError,
    WebChatBrowserHost,
    WebChatContext,
    WebChatGuidance,
    WebChatProvider,
    build_chatgpt_context,
    build_guidance,
    build_web_chat_context,
    build_web_chat_guidance,
    default_profile_root,
    guidance_digest,
    probe_webview2_runtime,
    redact_secrets,
)
from .guidance import CHATGPT_URL
from .host import ChatGptBrowserHost

__all__ = [
    "BrowserHostError",
    "BrowserRuntimeStatus",
    "BrowserStartError",
    "BrowserUnavailableError",
    "CHATGPT_URL",
    "ChatGptBrowserHost",
    "ChatGptContext",
    "ChatGptGuidance",
    "ContextBuildError",
    "GuidanceError",
    "WebChatBrowserHost",
    "WebChatContext",
    "WebChatGuidance",
    "WebChatProvider",
    "build_chatgpt_context",
    "build_guidance",
    "build_web_chat_context",
    "build_web_chat_guidance",
    "default_profile_root",
    "guidance_digest",
    "probe_webview2_runtime",
    "redact_secrets",
]
