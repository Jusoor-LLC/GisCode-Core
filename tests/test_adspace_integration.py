"""GisCode AdSpace: rules, providers, and the panel.

The panel must be honest. Neither requested provider can serve a Windows
desktop build (both are mobile-only), so the service must report that plainly,
never fabricate a creative, never make a request, and never show anything to a
Pro user. It must also survive a missing or broken settings source.
"""
from __future__ import annotations

import socket
import tkinter as tk

import pytest

from giscode import edition
from giscode.ads.providers import (
    AdCreative,
    AppLovinProvider,
    HousePromotionProvider,
    ProviderAvailability,
    UnityAdsProvider,
)
from giscode.ads.service import AdSpace

from tests.conftest import tk_available, tk_skip_reason

needs_tk = pytest.mark.skipif(not tk_available(), reason=tk_skip_reason())


class FakeSettings:
    def __init__(self, **values):
        self.values = dict(values)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class BrokenSettings:
    def get(self, key, default=None):
        raise RuntimeError("settings are unavailable")

    def set(self, key, value):
        raise RuntimeError("settings are unavailable")


@pytest.fixture(autouse=True)
def _core(monkeypatch):
    monkeypatch.delenv(edition.EDITION_ENV, raising=False)


# --------------------------------------------------------------------- rules
def test_advertising_is_off_by_default():
    state = AdSpace().state()
    assert state.enabled is False
    assert state.consent is False
    assert state.show_ad is False
    assert state.creative is None
    assert "off" in state.describe().lower()


def test_consent_is_required_before_anything_would_be_requested():
    state = AdSpace(settings=FakeSettings(**{"ads.enabled": True})).state()
    assert state.show_ad is False
    assert state.creative is None
    assert "consent" in state.reason.lower()


def test_no_provider_configured_explains_the_mobile_only_limitation():
    settings = FakeSettings(**{"ads.enabled": True, "ads.consent": True})
    state = AdSpace(settings=settings).state()
    assert state.show_ad is False
    assert "Unity Ads" in state.reason and "AppLovin" in state.reason
    assert "Windows" in state.reason


def test_unity_ads_reports_that_it_cannot_serve_windows():
    settings = FakeSettings(
        **{"ads.enabled": True, "ads.consent": True, "ads.provider": "unity_ads"}
    )
    state = AdSpace(settings=settings).state()
    assert state.available is False
    assert state.show_ad is False
    assert "iOS and Android" in state.reason
    assert state.source == "https://docs.unity.com/en-us/grow/ads/references/faq"


def test_applovin_reports_that_it_cannot_serve_windows():
    settings = FakeSettings(
        **{"ads.enabled": True, "ads.consent": True, "ads.provider": "applovin"}
    )
    state = AdSpace(settings=settings).state()
    assert state.available is False
    assert "Windows" in state.reason
    assert state.source.startswith("https://legal.applovin.com")


def test_an_unknown_provider_is_reported_not_crashed():
    settings = FakeSettings(
        **{"ads.enabled": True, "ads.consent": True, "ads.provider": "not-a-provider"}
    )
    state = AdSpace(settings=settings).state()
    assert state.show_ad is False
    assert "not part of this build" in state.reason


def test_the_house_promotion_is_available_and_disclosed():
    settings = FakeSettings(
        **{"ads.enabled": True, "ads.consent": True, "ads.provider": "house"}
    )
    state = AdSpace(settings=settings).state()
    assert state.show_ad is True
    creative = state.creative
    assert creative is not None
    assert creative.provider == "house"
    assert "not a paid advertisement" in creative.disclosure
    # Text only: nothing renderable as markup or script can arrive this way.
    assert all(isinstance(value, str) for value in (creative.headline, creative.body))


def test_pro_never_gets_a_creative(monkeypatch):
    monkeypatch.setenv(edition.EDITION_ENV, "pro")
    settings = FakeSettings(
        **{"ads.enabled": True, "ads.consent": True, "ads.provider": "house"}
    )
    state = AdSpace(settings=settings).state()
    assert state.ad_free is True
    assert state.show_ad is False
    assert state.creative is None


def test_a_broken_settings_source_fails_safe():
    state = AdSpace(settings=BrokenSettings()).state()
    assert state.show_ad is False
    assert state.enabled is False


def test_a_raising_provider_does_not_break_the_panel():
    class Exploding:
        name = "exploding"
        display_name = "Exploding"

        def availability(self):
            raise RuntimeError("boom")

        def creative(self):
            raise RuntimeError("boom")

    settings = FakeSettings(
        **{"ads.enabled": True, "ads.consent": True, "ads.provider": "exploding"}
    )
    state = AdSpace(settings=settings, providers=(Exploding(),)).state()
    assert state.show_ad is False
    assert "could not be queried" in state.reason


def test_the_service_makes_no_network_request(monkeypatch):
    """Not even a socket is opened while deciding what to show."""

    def refuse(*args, **kwargs):
        raise AssertionError("GisCode AdSpace must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    settings = FakeSettings(
        **{"ads.enabled": True, "ads.consent": True, "ads.provider": "house"}
    )
    assert AdSpace(settings=settings).state().show_ad is True


def test_provider_reports_cover_both_requested_providers():
    state = AdSpace().state()
    names = {report.name for report in state.providers}
    assert {"unity_ads", "applovin"} <= names
    for report in state.providers:
        if report.name in {"unity_ads", "applovin"}:
            assert report.availability.available is False
            assert report.availability.source


def test_the_provider_adapters_are_independent_of_the_service():
    """The adapter interface is what a new provider implements."""
    for provider in (UnityAdsProvider(), AppLovinProvider(), HousePromotionProvider()):
        availability = provider.availability()
        assert isinstance(availability, ProviderAvailability)
        creative = provider.creative()
        assert creative is None or isinstance(creative, AdCreative)


# --------------------------------------------------------------------- panel
needs_tk_gui = pytest.mark.skipif(not tk_available(), reason=tk_skip_reason())


def _panel(tk_root, settings):
    """Build the panel on the shared Tk root (a second root is not reliable)."""
    from giscode.gui.adspace import AdSpacePanel

    panel = AdSpacePanel(tk_root, service=AdSpace(settings=settings))
    tk_root.update_idletasks()
    return panel


@needs_tk_gui
def test_the_panel_names_both_unavailable_providers(tk_root):
    panel = _panel(tk_root, FakeSettings())
    try:
        texts = [label.cget("text") for label in panel._provider_rows.values()]
        combined = " ".join(texts)
        assert "not available on Windows desktop" in combined
        assert "iOS and Android" in combined
    finally:
        panel.destroy()


@needs_tk_gui
def test_the_panel_shows_no_creative_when_nothing_is_available(tk_root):
    panel = _panel(tk_root, FakeSettings())
    try:
        assert panel._creative_frame.winfo_manager() == ""
        assert "Advertising is off" in panel._state_label.cget("text")
    finally:
        panel.destroy()


@needs_tk_gui
def test_enabling_advertising_enables_consent_and_persists(tk_root):
    settings = FakeSettings()
    panel = _panel(tk_root, settings)
    try:
        assert str(panel._consent_check.cget("state")) == "disabled"
        panel._enabled_var.set(True)
        panel._on_enabled_toggled()
        tk_root.update_idletasks()
        assert settings.values["ads.enabled"] is True
        assert str(panel._consent_check.cget("state")) == "normal"
    finally:
        panel.destroy()


@needs_tk_gui
def test_the_panel_renders_the_house_creative_when_selected(tk_root):
    settings = FakeSettings(
        **{"ads.enabled": True, "ads.consent": True, "ads.provider": "house"}
    )
    panel = _panel(tk_root, settings)
    try:
        assert panel._creative_frame.winfo_manager() == "pack"
        assert panel._headline_label.cget("text") == "GisCode Pro"
        assert "not a paid advertisement" in panel._disclosure_label.cget("text")
    finally:
        panel.destroy()


@needs_tk_gui
def test_the_panel_is_read_only_when_settings_cannot_be_saved(tk_root):
    panel = _panel(tk_root, BrokenSettings())
    try:
        assert str(panel._enabled_check.cget("state")) == "normal"
        panel._enabled_var.set(True)
        panel._on_enabled_toggled()  # must not raise
        assert panel._enabled_var.get() is False  # the state did not change
    finally:
        panel.destroy()
