"""One chat surface: Chat types tasks, Projects reviews and approves plans.

Stage 2A measured that the Projects tab's "Planer Chat" panel was already
unmapped, but its transcript, prompt, Send/Stop and attach row were still
built, still loaded history, and still offered a second image path nobody
could reach. Stage 2B removed that panel. These tests pin the resulting
arrangement:

* Chat owns the only transcript and the only task input;
* Projects → Planer keeps a single-line plan request plus its four actions;
* the request line still sends through the existing threaded worker;
* refreshing a project no longer reads the Planer chat history.
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import messagebox, ttk
from types import SimpleNamespace

import pytest

from giscode.gui.main_window import MainWindow
from giscode.projects import Project, ProjectStore
from giscode.storage import ChatStore
from tests.conftest import _create_tk_root, _safe_destroy


@pytest.fixture
def window(tmp_path, monkeypatch):
    dialogs: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        messagebox, "showinfo",
        lambda title, msg, **kw: dialogs.append(("info", title, msg)),
    )
    monkeypatch.setattr(
        messagebox, "showerror",
        lambda title, msg, **kw: dialogs.append(("error", title, msg)),
    )

    project_store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    chat_store = ChatStore(db_path=str(tmp_path / "chats.db"))
    project = project_store.create_project(
        Project.create("Surface", str(tmp_path / "Surface"))
    )

    root = _create_tk_root()
    root.withdraw()
    win = MainWindow(
        root,
        terminal_executor=None,
        project_store=project_store,
        chat_store=chat_store,
    )
    root.update_idletasks()
    win._test_dialogs = dialogs
    try:
        yield SimpleNamespace(
            win=win, root=root, project=project, dialogs=dialogs
        )
    finally:
        _safe_destroy(root)
        for store in (project_store, chat_store):
            try:
                store.close()
            except Exception:
                pass


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _planer_tab(win):
    """The Projects → Planer sub-tab frame (not the whole Projects tab)."""
    view = win._project_workspace_view
    tabs = view._workspace_tabs
    return next(
        tabs.nametowidget(tab)
        for tab in tabs.tabs()
        if tabs.tab(tab, "text") == "Planer"
    )


def _texts(widget):
    return [w for w in _walk(widget) if isinstance(w, tk.Text)]


def _entries(widget):
    return [
        w for w in _walk(widget)
        if w.winfo_class() in ("Entry", "TEntry")
    ]


def _labels(widget):
    return [
        str(w.cget("text"))
        for w in _walk(widget)
        if isinstance(w, ttk.Label)
    ]


def _buttons(widget):
    return {
        str(w.cget("text")): w
        for w in _walk(widget)
        if isinstance(w, ttk.Button)
    }


def _open_project(window) -> None:
    win = window.win
    win._selected_project_id = window.project.id
    win._project_workspace_controller.open_project(window.project.id)
    window.root.update_idletasks()


def _pump_until(root, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.005)


# ------------------------------------------------ one input, one transcript


def test_chat_is_the_only_surface_with_a_transcript_and_input(window):
    win = window.win
    # Core hosts the conversation-history panel inside Chat; it is a read-only
    # history viewer, closed by default, and is not a second transcript.
    history_texts = set()
    if getattr(win, "_chat_history_host", None) is not None:
        assert win._chat_history_host.winfo_manager() == ""
        history_texts = set(_texts(win._chat_history_host))
    chat_texts = [
        widget for widget in _texts(win._tab_frames["Chat"]) if widget not in history_texts
    ]
    assert win.response_text in chat_texts
    assert win.task_entry in chat_texts
    assert len(chat_texts) == 2, "Chat should hold exactly a transcript and an input"
    assert win.task_entry.winfo_manager() == "pack"

    # The Planer sub-tab has no Text widget left: no transcript, no prompt.
    planer = _planer_tab(win)
    assert _texts(planer) == []

    # Its only typed input is the plan request line.
    entries = _entries(planer)
    assert entries == [win._project_workspace_view._plan_entry]


def test_projects_tab_labels_the_request_box_as_planning_only(window):
    labels = _labels(window.win._tab_frames["Projects"])
    assert "Plan request (planning only)" in labels
    assert any("Chat runs tasks" in label for label in labels), (
        "the tab must say Chat runs tasks and this box authors the plan"
    )


def test_chat_is_the_default_tab(window):
    win = window.win
    shown = win._notebook.tab(win._notebook.select(), "text")
    assert shown == "Chat", f"window landed on {shown!r}"


# ------------------------------------------------ the plan path still works


def test_plan_sends_through_the_send_path_and_updates_the_reply(
    window, monkeypatch
):
    win = window.win
    _open_project(window)
    view = win._project_workspace_view
    calls: list[str] = []

    def fake_send(message):
        calls.append(message)
        return SimpleNamespace(
            success=True, assistant_message="Planned: add a login screen."
        )

    monkeypatch.setattr(view._controller, "send_planer_message", fake_send)

    view._plan_entry.delete(0, tk.END)
    view._plan_entry.insert(0, "Add a login screen")
    assert view._on_plan_request() == "break"
    _pump_until(window.root, lambda: "Planned" in view._plan_reply_var.get())

    assert calls == ["Add a login screen"]
    assert "Planned: add a login screen." in view._plan_reply_var.get()
    assert view._plan_entry.get() == "", "the request line is cleared after sending"
    assert view._status_var.get() == "Planer responded"


def test_plan_actions_remain_available_and_wired(window):
    win = window.win
    _open_project(window)
    view = win._project_workspace_view
    buttons = _buttons(win._tab_frames["Projects"])

    for label in ("Plan", "Save Plan", "Get Readiness", "Approve & Start"):
        assert label in buttons, f"{label!r} disappeared from the Planer tab"
        assert str(buttons[label].cget("state")) != "disabled"

    buttons["Get Readiness"].invoke()
    window.root.update_idletasks()
    assert "readiness" in view._plan_reply_var.get().lower()


def test_refreshing_a_project_does_not_reload_planer_chat_history(
    window, monkeypatch
):
    """The removed panel used to repaint a hidden transcript on every refresh."""
    win = window.win
    _open_project(window)
    view = win._project_workspace_view

    def boom():
        raise AssertionError("refresh still reads the removed Planer chat history")

    monkeypatch.setattr(view._controller, "load_chat_history", boom)
    view._refresh_all()          # must not raise
    window.root.update_idletasks()
