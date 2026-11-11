"""Regression tests for the provider-neutral AI Web Chat surface.

ChatGPT is the default and the only provider GisCode embeds; every other
provider opens in the user's own browser.  Everything else here is a contract:
the registry records official destinations, an embedded host never starts for a
provider that is not embedded, profiles stay isolated, and the shared handoff
engine never becomes ChatGPT-specific.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from urllib.parse import urlparse

import pytest

from giscode import edition
from giscode.webchat.adapters import ADAPTERS
from giscode.webchat.context import ContextBuildError, build_web_chat_context
from giscode.webchat.guidance import build_web_chat_guidance
from giscode.webchat.host import (
    BrowserHostError,
    BrowserRuntimeStatus,
    BrowserStartError,
    BrowserUnavailableError,
    WebChatBrowserHost,
    default_profile_root,
)
from giscode.webchat.providers import (
    DEFAULT_PROVIDER_ID,
    PROVIDERS,
    PROVIDER_PREFERENCE_KEY,
    get_provider,
)
from giscode.webchat.registry import DEFAULT_REGISTRY, ProviderSelection, WebChatRegistry
from giscode.webchat.session import WebChatSessionManager


class _Settings:
    """Minimal settings object with the ``get``/``set`` contract."""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class _ReadOnlySettings:
    def get(self, key, default=None):
        return default


class _FakeProcess:
    def __init__(self):
        self.code = None
        self.terminated = False
        self.killed = False
        self.pid = 4321

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 0

    def kill(self):
        self.killed = True
        self.code = -9

    def join(self, timeout=None):
        return None


class _RecordingHost:
    """Host double that records lifecycle calls and starts nothing."""

    def __init__(self, available=True):
        self.available = available
        self.starts = 0
        self.stops = 0
        self.shutdowns = 0
        self.is_running = False
        self.status = "Browser ready" if available else "Browser unavailable: test"

    def runtime_status(self):
        return BrowserRuntimeStatus(
            self.available, "" if self.available else "test runtime missing"
        )

    def start(self):
        if not self.available:
            raise BrowserUnavailableError("test runtime missing")
        self.starts += 1
        self.is_running = True
        self.status = "ChatGPT browser open"

    def poll(self):
        return self.is_running

    def stop(self):
        self.stops += 1
        self.is_running = False
        self.status = "ChatGPT browser closed"

    def clear_session(self):
        self.status = "Browser session cleared"

    def shutdown(self):
        self.shutdowns += 1
        self.stop()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "disposable_project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "VALUE = 1\nAPI_KEY = 'sk-abcdefghijklmnopqrstuvwxyz'\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# Disposable\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=should-not-be-sent\n", encoding="utf-8")
    return root


# --------------------------------------------------------------------- registry
def test_default_provider_is_chatgpt():
    assert DEFAULT_PROVIDER_ID == "chatgpt"
    assert DEFAULT_REGISTRY.default().id == "chatgpt"
    assert PROVIDERS[0].id == "chatgpt"
    assert DEFAULT_REGISTRY.default().verified is True
    assert DEFAULT_REGISTRY.adapter("chatgpt") is not None


def test_registry_declares_every_target_provider_in_display_order():
    assert [provider.id for provider in PROVIDERS] == [
        "chatgpt",
        "claude",
        "gemini",
        "grok",
        "deepseek",
        "meta_ai",
    ]
    assert DEFAULT_REGISTRY.choices()[0] == ("chatgpt", "ChatGPT")


def test_chatgpt_is_the_only_embedded_provider_and_all_six_are_selectable():
    embedded = {provider.id for provider in PROVIDERS if provider.verified}
    assert embedded == {"chatgpt"}
    assert set(ADAPTERS) == embedded
    assert all(provider.available for provider in PROVIDERS)
    for provider in PROVIDERS:
        assert DEFAULT_REGISTRY.adapter(provider.id) is not None


def test_provider_urls_are_the_official_https_destinations():
    expected = {
        "chatgpt": "https://chatgpt.com/",
        "claude": "https://claude.ai/",
        "gemini": "https://gemini.google.com/app",
        "grok": "https://grok.com/",
        "deepseek": "https://chat.deepseek.com/",
        "meta_ai": "https://www.meta.ai/",
    }
    for provider in PROVIDERS:
        assert provider.url == expected[provider.id]
        assert provider.allows_url(provider.url)
        assert provider.official_source.startswith("https://")
        assert provider.auth_guidance
        assert "cookie" in provider.auth_guidance.lower() or "does not" in (
            provider.auth_guidance.lower()
        )


def test_provider_url_validation_rejects_foreign_hosts_and_schemes():
    chatgpt = get_provider("chatgpt")
    assert not chatgpt.allows_url("https://example.com/")
    assert not chatgpt.allows_url("http://chatgpt.com/")
    assert not chatgpt.allows_url("https://user:pass@chatgpt.com/")
    assert not chatgpt.allows_url("https://chatgpt.com:8443/")
    assert get_provider("claude").allows_url("https://claude.ai/")


def test_provider_adapter_exposes_the_shared_contract():
    adapter = DEFAULT_REGISTRY.adapter("chatgpt")
    assert adapter is not None
    context = adapter.build_context(None, [], "What is a closure?")
    assert context.provider_id == "chatgpt"
    guidance = adapter.build_guidance("Use a closure.", context)
    assert guidance.provider_id == "chatgpt"
    assert guidance.label == "[UNVERIFIED CHATGPT GUIDANCE]"
    assert adapter.capabilities.project_context is True
    assert adapter.capabilities.prompt_handoff is True


# -------------------------------------------------------------------- selection
def test_provider_selection_defaults_to_chatgpt_and_persists():
    settings = _Settings()
    assert ProviderSelection(settings).selected().id == "chatgpt"

    ProviderSelection(settings).select("claude")

    assert settings.data[PROVIDER_PREFERENCE_KEY] == "claude"
    assert ProviderSelection(settings).selected().id == "claude"


def test_unknown_stored_provider_falls_back_to_the_default():
    settings = _Settings({PROVIDER_PREFERENCE_KEY: "not-a-provider"})
    assert ProviderSelection(settings).selected().id == "chatgpt"


def test_selection_requires_writable_settings():
    with pytest.raises(RuntimeError):
        ProviderSelection(_ReadOnlySettings()).select("claude")
    # Selecting is the only operation that needs a writable store.
    assert ProviderSelection(_ReadOnlySettings()).selected().id == "chatgpt"


def test_provider_choice_never_touches_the_chatgpt_plan_settings():
    assert PROVIDER_PREFERENCE_KEY == "ai_web_chat.provider"
    settings = _Settings({"providers.chatgpt": {"enabled": True}})

    ProviderSelection(settings).select("grok")

    assert settings.data["providers.chatgpt"] == {"enabled": True}
    assert settings.data[PROVIDER_PREFERENCE_KEY] == "grok"


def test_ai_web_chat_is_a_shared_core_surface(monkeypatch):
    monkeypatch.delenv(edition.EDITION_ENV, raising=False)
    assert "AI Web Chat" in edition.tabs()
    assert edition.feature_enabled("ai_web_chat")


# -------------------------------------------------------------- profile isolation
def test_profiles_are_isolated_per_provider(monkeypatch, tmp_path):
    home = tmp_path / "profile"
    monkeypatch.setenv("GISCODE_HOME", str(home))

    assert default_profile_root("chatgpt") == (home / "chatgpt_browser").resolve()
    assert default_profile_root("claude") == (
        home / "ai_web_chat" / "claude"
    ).resolve()

    paths = {default_profile_root(provider) for provider in PROVIDERS}
    assert len(paths) == len(PROVIDERS)


# ------------------------------------------------------------------------- host
def test_embedded_host_refuses_a_system_browser_provider(tmp_path):
    calls = []

    def factory(url, profile):
        calls.append((url, profile))
        return _FakeProcess()

    host = WebChatBrowserHost(
        get_provider("claude"),
        tmp_path / "profile",
        probe=lambda: BrowserRuntimeStatus(True),
        process_factory=factory,
    )
    with pytest.raises(BrowserUnavailableError, match="not qualified"):
        host.start()

    assert calls == []
    assert "not qualified" in host.status


def test_session_manager_creates_one_host_per_provider():
    created = []

    def factory(adapter):
        created.append(adapter.provider.id)
        return _RecordingHost()

    manager = WebChatSessionManager(DEFAULT_REGISTRY, host_factory=factory)

    claude = manager.host_for("claude")
    assert claude is not None
    assert created == ["claude"]

    host = manager.host_for("chatgpt")
    assert host is not None
    assert created == ["claude", "chatgpt"]
    assert manager.host_for("chatgpt") is host


def test_browser_lifecycle_start_poll_stop_and_forget(tmp_path):
    processes = []
    profile = tmp_path / "profile"

    def factory(url, profile_root):
        process = _FakeProcess()
        processes.append(process)
        return process

    host = WebChatBrowserHost(
        get_provider("chatgpt"),
        profile,
        probe=lambda: BrowserRuntimeStatus(True),
        process_factory=factory,
    )
    host.start()
    assert host.is_running is True
    assert "ChatGPT" in host.status

    with pytest.raises(BrowserHostError, match="close"):
        host.clear_session()

    processes[0].code = 0
    assert host.poll() is False
    assert "closed" in host.status.lower()

    profile.mkdir(exist_ok=True)
    (profile / "cookie-store").write_text("not read by Core", encoding="utf-8")
    host.clear_session()
    assert not profile.exists()


def test_browser_url_is_scoped_to_the_selected_provider(tmp_path):
    with pytest.raises(BrowserStartError):
        WebChatBrowserHost(
            get_provider("claude"),
            tmp_path / "profile",
            url="https://chatgpt.com/",
        )

    host = WebChatBrowserHost(
        get_provider("chatgpt"),
        tmp_path / "profile",
        probe=lambda: BrowserRuntimeStatus(True),
        process_factory=lambda url, profile: _FakeProcess(),
    )
    with pytest.raises(BrowserStartError):
        host.start("https://example.com/")


# ---------------------------------------------------------------------- context
def test_context_without_a_project_answers_a_question():
    context = build_web_chat_context(
        get_provider("chatgpt"), None, [], "What is a monad?"
    )

    assert context.project_name == "No project"
    assert context.project_root is None
    assert context.files == ()
    assert "What is a monad?" in context.prompt
    assert "No project context" in context.prompt
    assert len(context.context_sha256) == 64


def test_no_project_question_names_the_selected_provider():
    context = build_web_chat_context(
        get_provider("claude"), None, [], "Explain type erasure."
    )
    assert "Question for Claude:" in context.prompt
    assert context.provider_id == "claude"


def test_no_project_context_refuses_files_and_empty_questions():
    with pytest.raises(ContextBuildError, match="project directory"):
        build_web_chat_context(
            get_provider("chatgpt"), None, ["src/app.py"], "question"
        )
    with pytest.raises(ContextBuildError, match="question"):
        build_web_chat_context(get_provider("chatgpt"), None, [], "   ")


def test_context_preview_redacts_secrets_and_hides_absolute_paths(project):
    context = build_web_chat_context(
        get_provider("grok"), project, ["src/app.py"], "Review this file."
    )

    assert str(project) not in context.prompt
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in context.prompt
    assert "[REDACTED]" in context.prompt
    assert "src/app.py" in context.prompt
    assert context.provider_id == "grok"
    assert context.redactions >= 1


# --------------------------------------------------------------------- guidance
def test_guidance_carries_the_selected_provider_and_its_origin(project):
    for provider_id in ("chatgpt", "claude"):
        provider = get_provider(provider_id)
        context = build_web_chat_context(provider, project, ["README.md"], "How?")
        guidance = build_web_chat_guidance("Add a test first.", context, provider)

        assert guidance.untrusted is True
        assert guidance.provider_id == provider_id
        assert guidance.provider_name == provider.display_name
        assert guidance.label == f"[UNVERIFIED {provider.display_name.upper()} GUIDANCE]"
        assert guidance.source_url == f"https://{urlparse(provider.url).hostname}/"
        block = guidance.to_task_block()
        assert "not executed or verified" in block
        assert f"Provider: {provider_id}" in block


def test_guidance_source_cannot_be_attributed_to_another_site(project):
    provider = get_provider("chatgpt")
    context = build_web_chat_context(provider, project, [], "Question")
    guidance = build_web_chat_guidance(
        "Answer", context, provider, source_url="https://evil.example/?token=1"
    )
    assert guidance.source_url == "https://chatgpt.com/"


def test_empty_response_is_not_accepted_as_guidance():
    provider = get_provider("chatgpt")
    context = build_web_chat_context(provider, None, [], "Question")
    with pytest.raises(Exception, match="clipboard"):
        build_web_chat_guidance("   ", context, provider)


# -------------------------------------------------------------------------- gui
def test_gui_starts_on_the_persisted_provider(tk_root):
    from giscode.gui.ai_web_chat import AIWebChatView

    settings = _Settings({PROVIDER_PREFERENCE_KEY: "claude"})
    view = AIWebChatView(tk_root, settings=settings, host=_RecordingHost())
    view.pack()
    try:
        assert view._provider().id == "claude"
        assert "your browser" in view.provider_status_var.get().lower()
        assert "default browser" in view.status_var.get().lower()
    finally:
        view.shutdown()


def test_gui_switch_provider_keeps_project_and_selected_files(tk_root, project):
    from giscode.gui.ai_web_chat import AIWebChatView

    settings = _Settings()
    view = AIWebChatView(
        tk_root,
        project_root_provider=lambda: str(project),
        settings=settings,
        host=_RecordingHost(),
    )
    view.pack()
    try:
        view._files_list.insert(tk.END, "README.md")
        assert view._provider().id == "chatgpt"

        assert view._select_provider("claude") is not None

        assert view._provider().id == "claude"
        assert settings.data[PROVIDER_PREFERENCE_KEY] == "claude"
        # The active project and the user's file selection survive the switch.
        assert view.project_var.get() == str(project.resolve())
        assert view._files_list.get(0, tk.END) == ("README.md",)
        # A prompt prepared for one provider is never reused for another.
        assert view._context is None
        assert "your own browser" in view.method_var.get().lower()
    finally:
        view.shutdown()


def test_gui_without_a_project_prepares_a_question_only_prompt(tk_root):
    from giscode.gui.ai_web_chat import AIWebChatView

    view = AIWebChatView(tk_root, host=_RecordingHost())
    view.pack()
    try:
        assert "No project selected" in view.project_var.get()
        view._question_text.insert("1.0", "Explain Python decorators.")
        context = view._prepare_context()
        assert context is not None
        assert context.project_name == "No project"
        assert context.files == ()
        assert "Explain Python decorators." in view._prompt_preview.get("1.0", tk.END)
        assert str(view._copy_btn["state"]) == "normal"
    finally:
        view.shutdown()


def test_gui_open_reload_and_close_use_the_owned_browser(tk_root):
    from giscode.gui.ai_web_chat import AIWebChatView

    host = _RecordingHost()
    view = AIWebChatView(tk_root, host=host)
    view.pack()
    try:
        assert view._open_browser() is True
        assert host.starts == 1
        view._reload_browser()
        assert host.stops == 1
        assert host.starts == 2
        view._close_browser()
        assert host.stops == 2
    finally:
        view.shutdown()


def test_gui_external_provider_opens_without_a_giscode_profile(tk_root):
    from giscode.gui.ai_web_chat import AIWebChatView
    from giscode.webchat.external import ExternalBrowserHost

    opened = []

    def factory(adapter):
        return ExternalBrowserHost(
            adapter.provider, opener=lambda url: opened.append(url) or True
        )

    view = AIWebChatView(
        tk_root,
        settings=_Settings(),
        session_manager=WebChatSessionManager(DEFAULT_REGISTRY, host_factory=factory),
    )
    view.pack()
    try:
        view._select_provider("gemini")
        assert view._open_browser() is True
        assert opened == ["https://gemini.google.com/app"]
        assert "in your browser" in view.status_var.get()
    finally:
        view.shutdown()


def test_gui_imports_guidance_with_the_selected_provider_label(tk_root, project):
    from giscode.gui.ai_web_chat import AIWebChatView

    inserted = []
    view = AIWebChatView(
        tk_root,
        project_root_provider=lambda: str(project),
        on_guidance=inserted.append,
        host=_RecordingHost(),
    )
    view.pack()
    try:
        view._select_provider("claude")
        view._files_list.insert(tk.END, "README.md")
        view._question_text.insert("1.0", "How should this be structured?")
        assert view._prepare_context() is not None
        tk_root.clipboard_clear()
        tk_root.clipboard_append("Prefer a pure function here.")
        view._import_clipboard()
        view._use_guidance()
        assert inserted
        assert "[UNVERIFIED CLAUDE GUIDANCE]" in inserted[0]
    finally:
        view.shutdown()


def test_chatgpt_compatibility_view_stays_pinned_to_chatgpt(tk_root):
    from giscode.gui.chatgpt_browser import ChatGPTBrowserView

    view = ChatGPTBrowserView(tk_root, host=_RecordingHost())
    view.pack()
    try:
        assert view._provider().id == "chatgpt"
        assert str(view._open_btn["text"]) == "Open ChatGPT"
        assert str(view._question_label["text"]) == "Question for ChatGPT:"
    finally:
        view.shutdown()


def test_main_window_exposes_ai_web_chat_and_keeps_chatgpt_default(tk_root):
    from giscode.gui.main_window import MainWindow

    window = MainWindow(tk_root)
    try:
        assert "AI Web Chat" in window._tab_frames
        assert "ChatGPT" not in window._tab_frames
        view = window._ai_web_chat_view
        assert view is not None
        assert view is window._chatgpt_browser_view
        assert view._provider().id == "chatgpt"
    finally:
        window._ai_web_chat_view.shutdown()
        tk_root.destroy()
