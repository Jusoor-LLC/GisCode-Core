"""Backward-compatible ChatGPT names for the shared AI Web Chat context engine.

The implementation lives in :mod:`giscode.webchat.context`, which knows only
provider metadata.  This module keeps the ChatGPT-specific names that existing
callers and tests import, so no caller outside the AI Web Chat package has to
change.
"""
from __future__ import annotations

from giscode.webchat.context import (  # noqa: F401 - compatibility re-exports
    ChatGptContext,
    ContextBuildError,
    WebChatContext,
    build_chatgpt_context,
    build_web_chat_context,
    redact_secrets,
)

__all__ = [
    "ChatGptContext",
    "ContextBuildError",
    "WebChatContext",
    "build_chatgpt_context",
    "build_web_chat_context",
    "redact_secrets",
]
