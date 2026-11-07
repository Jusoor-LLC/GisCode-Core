"""Small JSON-over-HTTP helper shared by the providers.

Kept deliberately thin: no retries, no redirects, no connection pooling. Provider
calls are local and interactive, and a slow answer is better reported than
silently retried. Every failure is translated into a :class:`ProviderError` whose
message a person can act on.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Optional

from giscode.providers.base import ProviderError


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[dict] = None,
    timeout: float = 5.0,
    headers: Optional[dict] = None,
) -> Any:
    """Perform one JSON request and return the decoded body (or ``None``).

    The URL is never echoed with credentials: it is built from a validated
    endpoint, and any API key travels in a header, not in the query string.
    """
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method.upper())
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        if exc.code in (401, 403):
            raise ProviderError(
                f"The provider rejected the request ({exc.code}).",
                action="Check the provider's API key or its access settings.",
            ) from exc
        if exc.code == 404:
            raise ProviderError(
                f"The provider has no such endpoint ({exc.code}).",
                action="Check the base URL: it should be the server root, not a page.",
            ) from exc
        suffix = f": {detail}" if detail and detail.strip() else ""
        raise ProviderError(
            f"The provider returned HTTP {exc.code}{suffix}",
            action="Check the provider's own log for the reason.",
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)) or "refused" in str(
            reason
        ).lower():
            raise ProviderError(
                "Nothing is listening on that endpoint.",
                action="Start the provider, or correct the endpoint, then test again.",
            ) from exc
        raise ProviderError(
            f"The endpoint could not be reached: {reason}",
            action="Check that the address and port are right and the provider is running.",
        ) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise ProviderError(
            "The provider did not answer in time.",
            action="It may still be loading a model; try again in a moment.",
        ) from exc

    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(
            "The provider answered with something that is not JSON.",
            action="Confirm the endpoint belongs to the provider's API.",
        ) from exc
