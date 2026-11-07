"""The one place that knows where "Support the Developer" points.

The sponsorship page belongs to the owner: the page is ``support.sponsors_url``
in settings, or the ``GISCODE_SPONSORS_URL`` environment variable, which wins.
The shipped default is the owner's PayPal page; an install can point it anywhere.
Any plain https page works - GitHub Sponsors and PayPal alike - so the owner can
change provider without a code change. Unconfigured is a normal state -
the button then says so, because guessing an address would send users somewhere
nobody chose.

The handoff is a URL and nothing else: the page opens in the operating system's
default browser, and GisCode never processes a payment, opens an embedded web
view, or reads a credential.
"""
from __future__ import annotations

import os
import webbrowser

#: Environment override, for a run that must not touch the settings file.
ENV = "GISCODE_SPONSORS_URL"
#: Settings key holding the page.
SETTING = "support.sponsors_url"

UNCONFIGURED_MESSAGE = (
    "No sponsorship page is configured.\n\n"
    f"Set \"{SETTING}\" in settings (or {ENV}) to the owner's sponsorship page - "
    "GitHub Sponsors or PayPal - to enable this button. GisCode does not guess "
    "an account address."
)


def normalise(url) -> str:
    """``url`` when it is a plain http(s) address with a host, else ``""``.

    Anything else - a relative path, a ``javascript:`` or ``file:`` target, an
    address with embedded credentials - is refused rather than handed to a
    browser.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if not (lowered.startswith("https://") or lowered.startswith("http://")):
        return ""
    remainder = text.split("://", 1)[1]
    host = remainder.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if not host or "@" in host:
        return ""
    return text


def sponsors_url(get_setting=None) -> str:
    """The configured page: environment first, then settings, else ``""``."""
    from_environment = os.environ.get(ENV, "").strip()
    if from_environment:
        return normalise(from_environment)
    if callable(get_setting):
        try:
            return normalise(get_setting(SETTING, ""))
        except Exception:
            return ""
    return ""


def open_sponsors(url: str, opener=None) -> bool:
    """Ask the system browser to open ``url``. False means nothing was opened."""
    target = normalise(url)
    if not target:
        return False
    opener = opener or webbrowser.open
    try:
        return bool(opener(target, new=2))
    except Exception:
        return False
