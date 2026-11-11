"""Provider-neutral unverified guidance import and attribution."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from .context import WebChatContext, redact_secrets
from .providers import CHATGPT_PROVIDER, WebChatProvider

CHATGPT_URL = CHATGPT_PROVIDER.url


class GuidanceError(ValueError):
    """The pasted response cannot be accepted as guidance."""


@dataclass(frozen=True)
class WebChatGuidance:
    """A user-imported web-chat answer and the context it answers."""

    text: str
    source_url: str
    provider_id: str
    provider_name: str
    project_name: str
    context_sha256: str
    imported_at: str
    redactions: int

    @property
    def untrusted(self) -> bool:
        return True

    @property
    def label(self) -> str:
        return f"[UNVERIFIED {self.provider_name.upper()} GUIDANCE]"

    def to_task_block(self) -> str:
        """The only representation intended to enter the Chat task input."""
        closing = f"[/UNVERIFIED {self.provider_name.upper()} GUIDANCE]"
        return (
            f"{self.label}\n"
            f"Source: {self.provider_name} via AI Web Chat (user-imported)\n"
            f"Provider: {self.provider_id}\n"
            f"Project: {self.project_name}\n"
            f"Context SHA-256: {self.context_sha256}\n"
            f"Imported: {self.imported_at}\n"
            "Status: advisory only; not executed or verified by GisCode.\n"
            "---\n"
            f"{self.text}\n"
            f"{closing}"
        )


def _safe_source(provider: WebChatProvider, source_url: str | None) -> str:
    """Return the provider's origin, never a copied tracking/query URL."""
    candidate = str(source_url or provider.url).strip()
    if not provider.allows_url(candidate):
        candidate = provider.url
    host = (urlparse(candidate).hostname or "").lower()
    return f"https://{host}/"


def build_web_chat_guidance(
    raw_response: str,
    context: WebChatContext,
    provider: WebChatProvider,
    *,
    source_url: str | None = None,
    max_chars: int = 60_000,
) -> WebChatGuidance:
    """Sanitize a pasted answer and attach immutable provider provenance."""
    text = str(raw_response or "").strip()
    if not text:
        raise GuidanceError(
            f"the clipboard does not contain a {provider.display_name} response"
        )
    redacted, redactions = redact_secrets(text)
    if len(redacted) > max_chars:
        redacted = redacted[:max_chars].rstrip() + "\n[TRUNCATED BY GISCODE]"
    return WebChatGuidance(
        text=redacted,
        source_url=_safe_source(provider, source_url),
        provider_id=provider.id,
        provider_name=provider.display_name,
        project_name=context.project_name,
        context_sha256=context.context_sha256,
        imported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        redactions=redactions,
    )


def guidance_digest(guidance: WebChatGuidance) -> str:
    """Stable digest for evidence, without exposing the guidance text."""
    return hashlib.sha256(guidance.to_task_block().encode("utf-8")).hexdigest()


# Compatibility aliases for the ChatGPT-only API.
ChatGptGuidance = WebChatGuidance


def build_guidance(
    raw_response: str,
    context: WebChatContext,
    *,
    source_url: str = CHATGPT_URL,
    max_chars: int = 60_000,
) -> WebChatGuidance:
    """Backward-compatible ChatGPT guidance builder."""
    return build_web_chat_guidance(
        raw_response,
        context,
        CHATGPT_PROVIDER,
        source_url=source_url,
        max_chars=max_chars,
    )
