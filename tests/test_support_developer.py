"""Core's "Support the Developer" entry points: the Help menu and About dialog.

The sponsorship page belongs to the owner, so the tests pin the two rules the
release task set: no invented account URL (an unconfigured build explains
itself), and no in-app payment or credential handling - the only action is
handing a validated http(s) page to the system browser.
"""
from __future__ import annotations

import pytest

from giscode import support
from giscode.gui import main_window as gui

PAGE = "https://example.com/sponsors"


@pytest.fixture(autouse=True)
def no_ambient_url(monkeypatch):
    """A developer machine must not decide what these tests assert."""
    monkeypatch.delenv(support.ENV, raising=False)


@pytest.fixture
def window(tk_root):
    window = gui.MainWindow(tk_root)
    try:
        yield window
    finally:
        if getattr(window, "_task_manager", None) is not None:
            window._task_manager.cancel()


def test_the_shipped_default_is_the_owner_paypal_page():
    from giscode.config.settings import DEFAULT_CONFIG

    page = DEFAULT_CONFIG["support"]["sponsors_url"]
    assert page == "https://paypal.me/Jusoor1948"
    opened: list[str] = []
    handoff = lambda target, new=0: opened.append(target) or True  # noqa: E731
    assert support.open_sponsors(page, opener=handoff)
    assert opened == [page]


def test_an_empty_setting_still_means_unconfigured():
    assert support.sponsors_url(lambda key, default="": "") == ""


def test_the_configured_page_is_used():
    getter = lambda key, default=None: PAGE  # noqa: E731 - one-line stand-in
    assert support.sponsors_url(getter) == PAGE


def test_the_environment_override_wins(monkeypatch):
    monkeypatch.setenv(support.ENV, "https://example.com/from-env")
    getter = lambda key, default=None: PAGE  # noqa: E731 - one-line stand-in
    assert support.sponsors_url(getter) == "https://example.com/from-env"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "javascript:alert(1)",
        "file:///C:/Windows/System32/calc.exe",
        "ftp://example.com/sponsors",
        "https://",
        "https://user:secret@example.com/",
    ],
)
def test_only_a_plain_http_page_is_opened(url):
    opened: list[str] = []
    assert support.open_sponsors(url, opener=lambda target, new=0: opened.append(target) or True) is False
    assert opened == []


def test_opening_hands_the_url_to_the_browser_once():
    opened: list[str] = []
    assert support.open_sponsors(PAGE, opener=lambda target, new=0: opened.append(target) or True)
    assert opened == [PAGE]


def test_a_browser_that_fails_is_reported_not_raised():
    def broken(target, new=0):
        raise OSError("no default browser")

    assert support.open_sponsors(PAGE, opener=broken) is False


def test_the_help_menu_offers_both_entry_points(window):
    labels = window.help_menu_labels()
    assert "Support the Developer" in labels
    assert "About GisCode" in labels


def test_help_and_about_open_the_same_configured_page(window, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(support, "sponsors_url", lambda *args, **kwargs: PAGE)
    monkeypatch.setattr(
        support,
        "open_sponsors",
        lambda url, opener=None: opened.append(url) or True,
    )

    window._help_menu.invoke(0)  # Help -> Support the Developer
    window._show_about()
    window._about_support_button.invoke()  # About -> Support the Developer

    assert opened == [PAGE, PAGE]


def test_an_unconfigured_button_explains_and_opens_nothing(window, monkeypatch):
    opened: list[str] = []
    shown: list[tuple] = []
    monkeypatch.setattr(support, "sponsors_url", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        support,
        "open_sponsors",
        lambda url, opener=None: opened.append(url) or True,
    )
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *args, **kwargs: shown.append(args))

    window._on_support_developer()

    assert opened == []
    assert shown
    message = shown[0][1]
    assert "No sponsorship page is configured" in message
    assert support.SETTING in message


def test_a_paypal_page_is_accepted_once_it_is_configured(monkeypatch):
    """PayPal works through the same https handoff; the handle here is synthetic."""
    page = "https://paypal.me/example-handle"
    monkeypatch.setenv(support.ENV, page)
    assert support.sponsors_url(lambda key, default=None: "") == page
    opened: list[str] = []
    handoff = lambda target, new=0: opened.append(target) or True  # noqa: E731
    assert support.open_sponsors(support.sponsors_url(), opener=handoff)
    assert opened == [page]


def test_a_paypal_handle_without_a_scheme_is_refused():
    assert support.normalise("paypal.me/example-handle") == ""


def test_help_and_about_open_the_shipped_paypal_page(window, monkeypatch):
    """Both entry points must hand the shipped default to the browser verbatim.

    Here only the browser handoff is replaced: the URL comes from the shipped
    settings, the same way it does for a user who has configured nothing.
    """
    opened: list[str] = []
    monkeypatch.setattr(
        support,
        "open_sponsors",
        lambda url, opener=None: opened.append(url) or True,
    )

    # A dialog here would mean the page resolved empty and would block the run
    # on a real modal, so fail immediately instead.
    def unexpected_dialog(*args, **kwargs):
        raise AssertionError(f"unexpected dialog: {args!r}")

    monkeypatch.setattr(gui.messagebox, "showinfo", unexpected_dialog)
    monkeypatch.setattr(gui.messagebox, "showwarning", unexpected_dialog)

    window._help_menu.invoke(0)  # Help -> Support the Developer
    window._show_about()
    window._about_support_button.invoke()  # About -> Support the Developer

    assert opened == ["https://paypal.me/Jusoor1948"] * 2
