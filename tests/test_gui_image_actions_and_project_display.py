"""Focused tests for the top-bar project display and the project boundary.

1. The top-bar Project field showed "+ New Project…" instead of the real
   active project name (or "None").

The Projects-tab Planer image tests that used to live here covered the hidden
Planer chat panel's attach/describe row. Stage 2B removed that panel, so those
cases are gone with the widgets: the surviving Chat-tab image behaviour
(attach, explicit analyze, honest vision-unavailable reporting, cancel) is
covered by ``tests/test_gui_image_ux.py``, and no-project vision routing by
``tests/test_gisai_no_project_vision.py``. What remains here is what still has
a live surface: the project display, the controller's project boundary, and
the plan request line's refusal without a project.
"""
from __future__ import annotations

import tkinter as tk
from types import SimpleNamespace

import pytest

from giscode.gui.main_window import MainWindow
from giscode.gui.project_workspace import (
    ProjectWorkspaceController,
    ProjectWorkspaceView,
)
from giscode.projects import Project, ProjectStore
from giscode.storage import ChatStore
from tests.conftest import _create_tk_root, _safe_destroy

from tests.conftest import tk_available, tk_skip_reason

_TK_OK = tk_available()
pytestmark_gui = pytest.mark.skipif(not _TK_OK, reason=tk_skip_reason())


@pytest.fixture
def planner(tmp_path):
    """A Projects-tab view with NO active project."""
    root = _create_tk_root()
    root.withdraw()
    frame = tk.Frame(root)
    frame.pack()
    project_store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    chat_store = ChatStore(db_path=str(tmp_path / "chats.db"))
    controller = ProjectWorkspaceController(project_store, chat_store)
    view = ProjectWorkspaceView(frame, controller)
    try:
        yield SimpleNamespace(
            root=root, view=view, controller=controller
        )
    finally:
        _safe_destroy(root)
        for store in (project_store, chat_store):
            try:
                store.close()
            except Exception:
                pass


# ---------------------------------------------- project boundary preserved
def test_plan_request_without_a_project_is_refused_honestly(planner):
    """Planning is project-bound: no session, no request, no claim."""
    planner.view._plan_entry.delete(0, tk.END)
    planner.view._plan_entry.insert(0, "Plan a new feature")

    assert planner.view._on_plan_request() == "break"
    assert "open a project" in planner.view._plan_reply_var.get().lower()
    assert planner.view._send_thread is None, "nothing may be sent without a project"


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.load_chat_history(),
        lambda c: c.save_plan(),
        lambda c: c.get_visual_design(),
        lambda c: c.approve_and_start(),
    ],
)
def test_project_operations_still_require_a_project(planner, call):
    """The project boundary is not weakened for any project operation."""
    assert planner.controller.has_active_session is False
    with pytest.raises(Exception) as exc:
        call(planner.controller)
    assert "No active project session" in str(exc.value)


def test_controller_uses_gisai_only_for_the_standalone_image_path(tmp_path):
    """Without a session the image path routes to GisAI; without an engine
    and without a session it must still refuse rather than guess."""
    engineless = ProjectWorkspaceController(
        ProjectStore(db_path=str(tmp_path / "p2.db")),
        ChatStore(db_path=str(tmp_path / "c2.db")),
    )
    with pytest.raises(Exception) as exc:
        engineless.send_planer_message_with_images("describe", ["x.png"])
    assert "No active project session" in str(exc.value)


# ------------------------------------------------------- project display

@pytest.fixture
def header(tmp_path, monkeypatch):
    """A real MainWindow with throwaway stores and captured dialogs."""
    from tkinter import messagebox

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
    project_store.create_project(
        Project.create("Alpha", str(tmp_path / "Alpha"))
    )
    project_store.create_project(
        Project.create("Beta", str(tmp_path / "Beta"))
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
    try:
        yield SimpleNamespace(win=win, root=root, tmp_path=tmp_path, store=project_store)
    finally:
        _safe_destroy(root)
        for store in (project_store, chat_store):
            try:
                store.close()
            except Exception:
                pass


def test_startup_with_no_project_displays_none(header):
    """The reported bug: startup showed "+ New Project…" instead of "None"."""
    assert header.win._active_project_context is None
    assert header.win._project_var.get() == header.win.NO_PROJECT_LABEL
    assert header.win._project_var.get() == "None"


def test_new_project_button_remains_available(header):
    from tkinter import ttk

    def _walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from _walk(child)

    labels = [
        w.cget("text")
        for w in _walk(header.root)
        if isinstance(w, ttk.Button)
    ]
    assert "+ New Project" in labels
    # The placeholder is no longer offered as a project entry.
    assert header.win.NEW_PROJECT_LABEL not in list(header.win._project_combo["values"])
    assert header.win.NO_PROJECT_LABEL in list(header.win._project_combo["values"])


def test_opening_a_project_displays_its_name(header):
    win = header.win
    win._on_open_existing_project = lambda: None  # not used here
    project = header.store.list_projects()[0]
    win._selected_project_id = project.id
    win._project_workspace_controller.open_project(project.id)
    win._select_project_path(project.root_path)
    win._refresh_active_project_display()
    header.root.update_idletasks()

    shown = win._project_var.get()
    assert shown == win._project_label(project)
    assert project.name in shown
    assert shown != win.NO_PROJECT_LABEL
    assert shown != win.NEW_PROJECT_LABEL


def test_switching_projects_updates_the_display(header):
    win = header.win
    first, second = header.store.list_projects()[0], header.store.list_projects()[1]
    for project in (first, second):
        win._selected_project_id = project.id
        win._project_workspace_controller.open_project(project.id)
        win._select_project_path(project.root_path)
        win._refresh_active_project_display()
        assert project.name in win._project_var.get()
    assert second.name in win._project_var.get()
    assert first.name not in win._project_var.get()


def test_closing_a_project_returns_the_display_to_none(header):
    win = header.win
    project = header.store.list_projects()[0]
    win._selected_project_id = project.id
    win._project_workspace_controller.open_project(project.id)
    win._select_project_path(project.root_path)
    win._refresh_active_project_display()
    assert project.name in win._project_var.get()

    win._on_close_project()
    header.root.update_idletasks()

    assert win._active_project_context is None
    assert win._project_var.get() == win.NO_PROJECT_LABEL


def test_selecting_none_closes_the_project_without_creating_one(header, monkeypatch):
    """Choosing "None" means "no project", not "start a new project"."""
    win = header.win
    project = header.store.list_projects()[0]
    win._selected_project_id = project.id
    win._project_workspace_controller.open_project(project.id)
    win._select_project_path(project.root_path)
    win._refresh_active_project_display()

    started: list[int] = []
    monkeypatch.setattr(
        win._project_workspace_view, "_on_start_new", lambda: started.append(1)
    )

    win._project_var.set(win.NO_PROJECT_LABEL)
    win._on_project_selection()
    header.root.update_idletasks()

    assert started == []
    assert win._active_project_context is None
    assert win._project_var.get() == win.NO_PROJECT_LABEL
