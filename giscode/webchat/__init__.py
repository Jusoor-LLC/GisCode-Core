"""Provider-neutral AI Web Chat for GisCode Core.

ChatGPT is the default qualified provider. Additional providers are registered
with official destinations and capability contracts but remain unavailable until
their browser lifecycle is independently qualified.
"""
from __future__ import annotations

from .adapters import ChatGPTAdapter, WebChatAdapter
from .context import (
    ChatGptContext,
    ContextBuildError,
    WebChatContext,
    build_chatgpt_context,
    build_web_chat_context,
    redact_secrets,
)
from .guidance import (
    CHATGPT_URL,
    ChatGptGuidance,
    GuidanceError,
    WebChatGuidance,
    build_guidance,
    build_web_chat_guidance,
    guidance_digest,
)
from .external import ExternalBrowserHost
from .host import (
    BrowserHostError,
    BrowserRuntimeStatus,
    BrowserStartError,
    BrowserUnavailableError,
    WebChatBrowserHost,
    default_profile_root,
    probe_webview2_runtime,
)
from .providers import (
    CHATGPT_PROVIDER,
    DEFAULT_PROVIDER_ID,
    EMBEDDED,
    EXTERNAL,
    PROVIDER_BY_ID,
    PROVIDER_PREFERENCE_KEY,
    PROVIDERS,
    ApiAccess,
    WebChatCapabilities,
    WebChatProvider,
    get_provider,
    require_provider,
)
from .registry import (
    DEFAULT_REGISTRY,
    ProviderSelection,
    WebChatRegistry,
    default_registry,
)
from .session import WebChatSessionManager

__all__ = [
    "BrowserHostError",
    "BrowserRuntimeStatus",
    "BrowserStartError",
    "BrowserUnavailableError",
    "CHATGPT_PROVIDER",
    "CHATGPT_URL",
    "ChatGPTAdapter",
    "ChatGptContext",
    "ChatGptGuidance",
    "ContextBuildError",
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_REGISTRY",
    "EMBEDDED",
    "EXTERNAL",
    "ExternalBrowserHost",
    "ApiAccess",
    "GuidanceError",
    "PROVIDER_BY_ID",
    "PROVIDER_PREFERENCE_KEY",
    "PROVIDERS",
    "ProviderSelection",
    "WebChatAdapter",
    "WebChatBrowserHost",
    "WebChatCapabilities",
    "WebChatContext",
    "WebChatGuidance",
    "WebChatProvider",
    "WebChatRegistry",
    "WebChatSessionManager",
    "build_chatgpt_context",
    "build_guidance",
    "build_web_chat_context",
    "build_web_chat_guidance",
    "default_profile_root",
    "default_registry",
    "get_provider",
    "guidance_digest",
    "probe_webview2_runtime",
    "redact_secrets",
    "require_provider",
]
