"""Backward-compatible ChatGPT names for the shared AI Web Chat guidance engine."""
from __future__ import annotations

from giscode.webchat.guidance import (  # noqa: F401 - compatibility re-exports
    CHATGPT_URL,
    ChatGptGuidance,
    GuidanceError,
    WebChatGuidance,
    build_guidance,
    build_web_chat_guidance,
    guidance_digest,
)

__all__ = [
    "CHATGPT_URL",
    "ChatGptGuidance",
    "GuidanceError",
    "WebChatGuidance",
    "build_guidance",
    "build_web_chat_guidance",
    "guidance_digest",
]
