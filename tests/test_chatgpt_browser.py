from __future__ import annotations

import tkinter as tk
from pathlib import Path

import pytest

from giscode import edition
from giscode.browser.context import ContextBuildError, build_chatgpt_context, redact_secrets
from giscode.browser.guidance import build_guidance
from giscode.browser.host import (
    BrowserRuntimeStatus,
    BrowserStartError,
    BrowserUnavailableError,
    ChatGptBrowserHost,
)


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


class _FakeHost:
    def __init__(self, available=True):
        self.available = available
        self.started = 0
        self.stopped = 0
        self.shutdowns = 0
        self.is_running = False
        self.status = "Browser ready" if available else "Browser unavailable: test"
        self.last_error = ""

    def runtime_status(self):
        return BrowserRuntimeStatus(self.available, "" if self.available else "test runtime missing")

    def start(self):
        if not self.available:
            raise BrowserUnavailableError("test runtime missing")
        self.started += 1
        self.is_running = True
        self.status = "ChatGPT browser open"

    def poll(self):
        return self.is_running

    def stop(self):
        self.stopped += 1
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


def test_context_is_grounded_redacted_and_bounded(project):
    context = build_chatgpt_context(
        project, ["src/app.py", "README.md"], "What should I change?"
    )

    assert context.files == ("src/app.py", "README.md")
    assert context.project_name == "disposable_project"
    assert str(project) not in context.prompt
    assert "src/app.py" in context.prompt
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in context.prompt
    assert "[REDACTED]" in context.prompt
    assert context.redactions >= 1
    assert context.context_sha256 in context.prompt or len(context.context_sha256) == 64


def test_context_rejects_escaping_paths_and_secret_files(project):
    with pytest.raises(ContextBuildError, match="outside"):
        build_chatgpt_context(project, ["../outside.py"], "question")
    with pytest.raises(ContextBuildError, match="credential"):
        build_chatgpt_context(project, [".env"], "question")


def test_context_requires_question_and_real_file(project):
    with pytest.raises(ContextBuildError, match="question"):
        build_chatgpt_context(project, ["README.md"], " ")
    with pytest.raises(ContextBuildError, match="not a file"):
        build_chatgpt_context(project, ["missing.py"], "question")


def test_guidance_is_explicitly_unverified_and_attributed(project):
    context = build_chatgpt_context(project, ["README.md"], "How should I proceed?")
    guidance = build_guidance("Try adding a test first.", context)

    assert guidance.untrusted is True
    assert guidance.project_name == context.project_name
    assert guidance.context_sha256 == context.context_sha256
    block = guidance.to_task_block()
    assert "[UNVERIFIED CHATGPT GUIDANCE]" in block
    assert "not executed or verified" in block
    assert "Try adding a test first." in block
    assert "chatgpt.com" in guidance.source_url


def test_guidance_redacts_secret_values(project):
    context = build_chatgpt_context(project, [], "Question")
    guidance = build_guidance("Use sk-abcdefghijklmnopqrstuvwxyz here.", context)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in guidance.text
    assert "[REDACTED]" in guidance.text
    assert guidance.redactions == 1


def test_redact_secrets_handles_assignments_and_private_keys():
    text = "password = 'super-secret-value'\n-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    cleaned, count = redact_secrets(text)
    assert "super-secret-value" not in cleaned
    assert "abc" not in cleaned
    assert count >= 2


def test_browser_unavailable_is_reported_without_starting_process(tmp_path):
    calls = []

    def factory(url, profile):
        calls.append((url, profile))
        raise AssertionError("factory must not run when the runtime is unavailable")

    host = ChatGptBrowserHost(
        profile_root=tmp_path / "profile",
        probe=lambda: BrowserRuntimeStatus(False, "no runtime"),
        process_factory=factory,
    )
    with pytest.raises(BrowserUnavailableError, match="no runtime"):
        host.start()
    assert calls == []
    assert "unavailable" in host.status.lower()


def test_browser_start_poll_stop_and_persistence(tmp_path):
    processes = []
    profiles = []

    def factory(url, profile):
        profiles.append(profile)
        process = _FakeProcess()
        processes.append(process)
        return process

    host = ChatGptBrowserHost(
        profile_root=tmp_path / "profile",
        probe=lambda: BrowserRuntimeStatus(True),
        process_factory=factory,
    )
    host.start()
    assert host.is_running is True
    assert host.status == "ChatGPT browser open"
    assert profiles == [(tmp_path / "profile").resolve()]
    processes[0].code = 0
    assert host.poll() is False
    assert host.status == "ChatGPT browser closed"
    assert processes[0].terminated is False
    host.stop()


def test_browser_stop_terminates_running_process(tmp_path):
    process = _FakeProcess()
    host = ChatGptBrowserHost(
        profile_root=tmp_path / "profile",
        probe=lambda: BrowserRuntimeStatus(True),
        process_factory=lambda url, profile: process,
    )
    host.start()
    host.stop()
    assert process.terminated is True
    assert host.is_running is False
    assert "closed" in host.status.lower()


def test_clear_session_is_isolated_and_refuses_while_open(tmp_path):
    process = _FakeProcess()
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "cookie-store").write_text("not read by Core", encoding="utf-8")
    host = ChatGptBrowserHost(
        profile_root=profile,
        probe=lambda: BrowserRuntimeStatus(True),
        process_factory=lambda url, profile_root: process,
    )
    host.start()
    with pytest.raises(Exception, match="close"):
        host.clear_session()
    host.stop()
    host.clear_session()
    assert not profile.exists()


def test_invalid_browser_url_is_refused(tmp_path):
    host = ChatGptBrowserHost(
        profile_root=tmp_path / "profile",
        probe=lambda: BrowserRuntimeStatus(True),
        process_factory=lambda url, profile: _FakeProcess(),
    )
    with pytest.raises(BrowserStartError):
        host.start("https://example.com/")


def test_core_edition_exposes_ai_web_chat_tab(monkeypatch):
    monkeypatch.delenv(edition.EDITION_ENV, raising=False)
    assert "AI Web Chat" in edition.tabs()
    assert "ai_web_chat" in edition.SHARED_FEATURES
    # The old ChatGPT-only tab label is gone; ChatGPT is a provider now.
    assert "ChatGPT" not in edition.tabs()


def test_gui_context_prepare_browser_and_guidance_handoff(tk_root, project):
    from giscode.gui.chatgpt_browser import ChatGPTBrowserView

    host = _FakeHost()
    inserted = []
    view = ChatGPTBrowserView(
        tk_root,
        project_root_provider=lambda: str(project),
        on_guidance=inserted.append,
        host=host,
    )
    view.pack(fill=tk.BOTH, expand=True)
    try:
        assert view.project_var.get() == str(project.resolve())
        view._files_list.insert(tk.END, "src/app.py")
        view._question_text.insert("1.0", "What should I verify?")
        context = view._prepare_context()
        assert context is not None
        assert "src/app.py" in view._prompt_preview.get("1.0", tk.END)
        assert view._open_browser() is True
        assert host.started == 1
        tk_root.clipboard_clear()
        tk_root.clipboard_append("Add a regression test before changing behavior.")
        view._import_clipboard()
        assert view._guidance is not None
        view._use_guidance()
        assert inserted and "[UNVERIFIED CHATGPT GUIDANCE]" in inserted[0]
        view._close_browser()
        assert host.stopped == 1
    finally:
        view.shutdown()


def test_gui_browser_failure_does_not_raise(tk_root, project):
    from giscode.gui.chatgpt_browser import ChatGPTBrowserView

    host = _FakeHost(available=False)
    view = ChatGPTBrowserView(
        tk_root,
        project_root_provider=lambda: str(project),
        host=host,
    )
    view.pack()
    try:
        assert view._open_browser() is False
        assert "unavailable" in view.status_var.get().lower()
    finally:
        view.shutdown()


def test_main_window_builds_ai_web_chat_tab(tk_root):
    from giscode.gui.main_window import MainWindow

    window = MainWindow(tk_root)
    try:
        assert "AI Web Chat" in window._tab_frames
        assert window._ai_web_chat_view is not None
        # ChatGPT stays the default provider and the old alias still resolves.
        assert window._ai_web_chat_view._provider().id == "chatgpt"
        assert window._chatgpt_browser_view is window._ai_web_chat_view
    finally:
        window._ai_web_chat_view.shutdown()
        tk_root.destroy()


def test_main_window_inserts_guidance_only_into_chat_input(tk_root):
    from giscode.gui.main_window import MainWindow

    window = MainWindow(tk_root)
    try:
        window._use_ai_web_chat_guidance(
            "[UNVERIFIED CHATGPT GUIDANCE]\nReview the API boundary.\n[/UNVERIFIED CHATGPT GUIDANCE]"
        )
        assert "Review the API boundary" in window.task_entry.get("1.0", tk.END)
        assert "unverified" in window._status_var.get().lower()
        # The ChatGPT-era name still inserts into the same input.
        assert callable(window._use_chatgpt_guidance)
    finally:
        window._ai_web_chat_view.shutdown()
        tk_root.destroy()


def test_main_window_starts_when_browser_is_unavailable(monkeypatch, tk_root):
    from giscode.gui import ai_web_chat
    from giscode.gui.main_window import MainWindow

    # The window must build even when the optional browser engine is missing;
    # the AI Web Chat tab reports it instead of taking Core down.
    monkeypatch.setattr(
        ai_web_chat.AIWebChatView,
        "host_factory",
        staticmethod(lambda adapter: _FakeHost(available=False)),
    )
    window = MainWindow(tk_root)
    try:
        assert "AI Web Chat" in window._tab_frames
        assert "unavailable" in window._ai_web_chat_view.status_var.get().lower()
    finally:
        window._ai_web_chat_view.shutdown()
        tk_root.destroy()
