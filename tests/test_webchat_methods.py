"""Access-method tests for AI Web Chat.

The product promise is that a user can pick any of the six providers, that a
chat subscription is never presented as API access, and that GisCode only hosts
a provider window when that provider's embedded lifecycle is qualified.
"""
from __future__ import annotations

import tkinter as tk

import pytest

from giscode import edition
from giscode.webchat.external import ExternalBrowserHost
from giscode.webchat.host import (
    BrowserRuntimeStatus,
    BrowserStartError,
    WebChatBrowserHost,
)
from giscode.webchat.providers import (
    DEFAULT_PROVIDER_ID,
    EMBEDDED,
    EXTERNAL,
    PROVIDERS,
    PROVIDER_PREFERENCE_KEY,
    get_provider,
)
from giscode.webchat.registry import DEFAULT_REGISTRY
from giscode.webchat.session import WebChatSessionManager


class _Settings:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def test_every_target_provider_is_selectable():
    assert [provider.id for provider in PROVIDERS] == [
        "chatgpt",
        "claude",
        "gemini",
        "grok",
        "deepseek",
        "meta_ai",
    ]
    assert DEFAULT_REGISTRY.default().id == DEFAULT_PROVIDER_ID
    for provider in PROVIDERS:
        assert provider.available is True
        assert DEFAULT_REGISTRY.adapter(provider.id) is not None


def test_only_chatgpt_uses_the_embedded_giscode_window():
    embedded = [p.id for p in PROVIDERS if p.browser_mode == EMBEDDED]
    external = [p.id for p in PROVIDERS if p.browser_mode == EXTERNAL]
    assert embedded == ["chatgpt"]
    assert external == ["claude", "gemini", "grok", "deepseek", "meta_ai"]
    for provider in PROVIDERS:
        assert provider.method_label
        assert provider.session_note
        assert provider.auth_guidance


def test_external_providers_state_that_giscode_keeps_no_session():
    for provider in PROVIDERS:
        if provider.browser_mode != EXTERNAL:
            continue
        assert "your own browser" in provider.session_note.lower()
        assert "GisCode" in provider.session_note
        assert provider.embedding_note
        assert provider.status_label == "Ready in your browser"


def test_direct_api_is_reported_separately_from_chat_and_billing():
    for provider in PROVIDERS:
        if provider.api is None:
            assert provider.api_label
            assert "none" in provider.api_label.lower()
            continue
        assert provider.api.docs_url.startswith("https://")
        label = provider.api_label.lower()
        assert "separate" in label
        assert provider.api.product
        assert provider.api.billing_note


def test_meta_ai_has_no_consumer_api_and_points_at_llama_as_a_different_product():
    meta = get_provider("meta_ai")
    assert meta.api is None
    assert "llama" in meta.api_label.lower()
    assert "separate" in meta.api_label.lower()


def test_gemini_records_googles_embedded_sign_in_block():
    gemini = get_provider("gemini")
    assert gemini.browser_mode == EXTERNAL
    assert "embedded" in gemini.embedding_note.lower()
    assert "google" in gemini.embedding_note.lower()


def test_external_host_opens_the_official_url_without_a_profile():
    opened: list[str] = []
    host = ExternalBrowserHost(
        get_provider("claude"), opener=lambda url: opened.append(url) or True
    )

    host.start()

    assert opened == ["https://claude.ai/"]
    assert host.profile_root is None
    assert host.is_running is False
    assert "no session" in host.status
    assert host.runtime_status().available is True


def test_external_host_refuses_another_providers_url():
    host = ExternalBrowserHost(get_provider("gemini"), opener=lambda url: True)
    with pytest.raises(BrowserStartError):
        host.start("https://claude.ai/")


def test_external_host_reports_a_browser_that_refuses_the_url():
    host = ExternalBrowserHost(get_provider("deepseek"), opener=lambda url: False)
    with pytest.raises(BrowserStartError, match="did not accept"):
        host.start()
    assert "did not accept" in host.status


def test_external_host_never_claims_to_own_or_forget_a_session():
    host = ExternalBrowserHost(get_provider("grok"), opener=lambda url: True)
    host.start()
    assert host.poll() is False
    host.stop()
    assert "does not control" in host.status
    with pytest.raises(Exception, match="clear it in your own browser"):
        host.clear_session()
    host.shutdown()  # nothing owned


def test_switching_provider_never_touches_api_or_model_settings():
    settings = _Settings(
        {
            "providers.chatgpt": {"enabled": True},
            "model.selected_model": "local-model",
            "inference.backend": "ollama",
        }
    )
    from giscode.webchat.registry import ProviderSelection

    ProviderSelection(settings).select("gemini")

    assert settings.data[PROVIDER_PREFERENCE_KEY] == "gemini"
    assert settings.data["model.selected_model"] == "local-model"
    assert settings.data["inference.backend"] == "ollama"
    assert settings.data["providers.chatgpt"] == {"enabled": True}


def test_gui_shows_method_api_and_local_lines(tk_root):
    from giscode.gui.ai_web_chat import AIWebChatView

    class _Host:
        is_running = False

        def runtime_status(self):
            from giscode.webchat.host import BrowserRuntimeStatus

            return BrowserRuntimeStatus(True)

        def start(self):
            return None

        def poll(self):
            return False

        def stop(self):
            return None

        def clear_session(self):
            return None

        def shutdown(self):
            return None

    view = AIWebChatView(tk_root, host=_Host())
    view.pack()
    try:
        assert "isolated window" in view.method_var.get()
        assert "OpenAI Platform API" in view.api_var.get()
        assert "separate" in view.api_var.get().lower()
        view._select_provider("gemini")
        assert "your own browser" in view.method_var.get().lower()
        assert "Gemini API" in view.api_var.get()
        assert "Model Manager" in view.local_var.get()
        assert str(view._close_btn["state"]) == "disabled"
        assert str(view._open_btn["state"]) == "normal"
    finally:
        view.shutdown()


def test_gui_opens_an_external_provider_through_the_system_browser(tk_root):
    from giscode.gui.ai_web_chat import AIWebChatView

    opened: list[str] = []
    view = AIWebChatView(
        tk_root,
        settings=_Settings(),
        session_manager=WebChatSessionManager(
            DEFAULT_REGISTRY,
            host_factory=lambda adapter: ExternalBrowserHost(
                adapter.provider, opener=lambda url: opened.append(url) or True
            ),
        ),
    )
    view.pack()
    try:
        view._select_provider("gemini")
        assert view._open_browser() is True
        assert opened == ["https://gemini.google.com/app"]
        assert "in your browser" in view.status_var.get()
    finally:
        view.shutdown()


def test_ai_web_chat_is_shared_by_both_editions(monkeypatch):
    monkeypatch.setenv(edition.EDITION_ENV, "pro")
    assert "AI Web Chat" in edition.PRO_TABS
    assert edition.feature_enabled("ai_web_chat") is True
    monkeypatch.delenv(edition.EDITION_ENV, raising=False)
    assert "AI Web Chat" in edition.tabs()


def test_session_recovery_is_user_driven_and_never_silent():
    embedded = get_provider("chatgpt")
    external = get_provider("claude")
    assert "sign in" in embedded.recovery_guidance.lower()
    assert "cannot" in embedded.recovery_guidance.lower()
    assert "sign in again" in external.recovery_guidance.lower()
    assert "no session to restore" in external.recovery_guidance.lower()
    assert external.recovery_guidance != embedded.recovery_guidance


def test_embedded_browser_can_be_reopened_after_it_closes(tmp_path):
    processes = []

    class _Process:
        def __init__(self):
            self.code = None

        def poll(self):
            return self.code

        def terminate(self):
            self.code = 0

        def kill(self):
            self.code = -9

        def join(self, timeout=None):
            return None

    def factory(url, profile):
        process = _Process()
        processes.append(process)
        return process

    host = WebChatBrowserHost(
        get_provider("chatgpt"),
        tmp_path / "profile",
        probe=lambda: BrowserRuntimeStatus(True),
        process_factory=factory,
    )
    host.start()
    assert host.is_running is True
    processes[0].code = 0
    assert host.poll() is False
    host.start()
    assert host.is_running is True
    assert len(processes) == 2
    host.stop()
    assert host.is_running is False


def test_external_browser_can_be_reopened_without_a_session():
    opened: list[str] = []
    host = ExternalBrowserHost(
        get_provider("gemini"), opener=lambda url: opened.append(url) or True
    )
    host.start()
    host.start()
    assert opened == ["https://gemini.google.com/app"] * 2
    assert host.profile_root is None
    assert host.is_running is False


def test_no_provider_offers_browser_automation():
    for provider in PROVIDERS:
        assert provider.automation_allowed is False
        assert "never types" in provider.automation_note.lower()
    adapter = DEFAULT_REGISTRY.adapter("chatgpt")
    assert not hasattr(adapter, "automate")
    assert not hasattr(adapter, "submit_prompt")


def test_gui_reports_recovery_and_automation_boundary(tk_root):
    from giscode.gui.ai_web_chat import AIWebChatView

    view = AIWebChatView(tk_root, settings=_Settings())
    view.pack()
    try:
        assert "sign in" in view.session_var.get().lower()
        assert "automation" in view.automation_var.get().lower()
        view._select_provider("gemini")
        assert "your own browser" in view.session_var.get().lower()
        assert "sign in again" in view.session_var.get().lower()
        assert "automate" not in view.automation_var.get().lower() or (
            "never" in view.automation_var.get().lower()
        )
    finally:
        view.shutdown()
