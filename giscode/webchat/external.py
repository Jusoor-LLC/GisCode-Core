"""System-browser host for providers GisCode does not embed.

Some providers cannot be signed into from an embedded webview (Google blocks
Google Account sign-in from embedded frameworks outright), and none of the
others has had its embedded sign-in qualified in this build.  For those, GisCode
opens the provider's official HTTPS page in the user's default browser and keeps
no profile, cookie jar, session or control channel.

The class deliberately implements the same lifecycle surface as
:class:`giscode.webchat.host.WebChatBrowserHost` so the GUI, registry and
session manager need no provider-specific branches.  ``is_running`` is always
false: the browser belongs to the user, not to GisCode.
"""
from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .host import (
    BrowserHostError,
    BrowserRuntimeStatus,
    BrowserStartError,
    _validate_url,
)
from .providers import WebChatProvider

Opener = Callable[[str], bool]


def _default_opener(url: str) -> bool:
    """Open *url* with the OS default browser; report whether it was accepted."""
    try:
        return bool(webbrowser.open(url, new=2, autoraise=True))
    except Exception:  # noqa: BLE001 - a missing browser is reported, not raised
        return False


class ExternalBrowserHost:
    """Open a provider in the user's browser without owning its session."""

    def __init__(
        self,
        provider: WebChatProvider,
        *,
        opener: Opener | None = None,
        url: str | None = None,
    ) -> None:
        self._provider = provider
        self._opener = opener or _default_opener
        self._url = _validate_url(url or provider.url, provider)
        self._status = "Not opened yet"
        self._last_error = ""
        self._opened = False

    @property
    def provider(self) -> WebChatProvider:
        return self._provider

    @property
    def profile_root(self) -> None:
        """GisCode keeps no profile for an external browser."""
        return None

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def is_running(self) -> bool:
        """Always false: the user's browser is not a GisCode-owned process."""
        return False

    def runtime_status(self) -> BrowserRuntimeStatus:
        return BrowserRuntimeStatus(True, "", "default browser")

    def start(self, url: str | None = None) -> None:
        target = _validate_url(url or self._url, self._provider)
        try:
            accepted = self._opener(target)
        except Exception as exc:  # noqa: BLE001 - report, never crash the app
            self._last_error = f"could not open your browser: {type(exc).__name__}"
            self._status = self._last_error
            raise BrowserStartError(self._last_error) from exc
        if not accepted:
            self._last_error = "your default browser did not accept the address"
            self._status = self._last_error
            raise BrowserStartError(self._last_error)
        self._opened = True
        self._last_error = ""
        self._status = (
            f"{self._provider.display_name} opened in your browser — "
            "sign in there; GisCode keeps no session"
        )

    def poll(self) -> bool:
        """GisCode cannot observe the user's browser, so it is never running."""
        return False

    def stop(self) -> None:
        self._status = (
            f"{self._provider.display_name} is open in your browser; "
            "GisCode does not control or close it"
        )

    def clear_session(self) -> None:
        raise BrowserHostError(
            f"GisCode keeps no {self._provider.display_name} session; "
            "clear it in your own browser"
        )

    def shutdown(self) -> None:
        """Nothing is owned, so there is nothing to stop."""
