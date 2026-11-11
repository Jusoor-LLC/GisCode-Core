"""Redact secrets before a message leaves this computer.

The local providers never need this: their endpoint is loopback.  A remote
provider does.  This module is deliberately focused on credential-shaped text,
not on rewriting source code: it removes known secret values and common
``api_key = ...`` / bearer-token forms, then leaves the engineering request
intact.  Redaction is applied to provider messages at the last boundary so a
caller cannot accidentally bypass it by calling a provider directly.
"""
from __future__ import annotations

import re
from typing import Iterable

REDACTED = "[REDACTED]"

_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|apikey|client[_-]?secret|secret[_-]?key|"
               r"access[_-]?token|refresh[_-]?token|auth[_-]?token|password|passwd)\b"
               r"\s*[:=]\s*['\"]?([^\s'\"]{4,})"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----.*?"
               r"-----END (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----", re.DOTALL),
)


def redact_text(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Return *text* with known secret values and credential forms removed."""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    result = text
    for secret in extra_secrets or ():
        value = str(secret or "")
        if len(value) >= 6:
            result = result.replace(value, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            # Preserve the field name (api_key:) but replace its value; this
            # keeps the request readable without shipping the credential.
            result = pattern.sub(lambda match: f"{match.group(1)}: {REDACTED}", result)
        else:
            result = pattern.sub(REDACTED, result)
    return result


def redact_messages(messages: list[dict], extra_secrets: Iterable[str] = ()) -> list[dict]:
    """Copy messages, redacting text while leaving image payloads untouched."""
    secret_values = tuple(str(s) for s in (extra_secrets or ()) if s)
    result: list[dict] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        copy = dict(message)
        content = copy.get("content")
        if isinstance(content, str):
            copy["content"] = redact_text(content, secret_values)
        elif isinstance(content, list):
            parts: list = []
            for part in content:
                if isinstance(part, dict):
                    item = dict(part)
                    if isinstance(item.get("text"), str):
                        item["text"] = redact_text(item["text"], secret_values)
                    parts.append(item)
                elif isinstance(part, str):
                    parts.append(redact_text(part, secret_values))
            copy["content"] = parts
        result.append(copy)
    return result
