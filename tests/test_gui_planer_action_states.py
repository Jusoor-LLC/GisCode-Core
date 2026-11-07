"""Planer action states follow the open project (Stage 2G).

Reported problem
----------------
The Projects -> Planer buttons ``Plan`` / ``Save Plan`` / ``Get Readiness`` /
``Approve & Start`` were all enabled while the application reported
``NO_PROJECT``. Three of them raise ``No active project session`` in that state,
so pressing them could only produce an error dialog.

Behaviour pinned here
---------------------
* Without an open project the session-bound actions are disabled; ``Get
  Readiness`` stays enabled because it has no session prerequisite and answers
  ``NO_PLAN`` truthfully.
* Opening or switching to a project enables them; closing disables them again.
* The ``State:`` label follows the same transitions: ``NO_PROJECT`` with no
  session, the real lifecycle name once a project is open, and ``NO_PROJECT``
  again after closing.
* Enabled buttons still call the original handlers.
* Duck-typed controllers that do not report ``has_active_session`` keep the old
  (enabled) behaviour, so nothing that predates the property changes quietly.
"""
from __future__ import annotations

import gc
import tkinter as tk
from types import SimpleNamespace

import pytest

from giscode.gui.project_workspace import (
    ProjectWorkspaceController,
    ProjectWorkspaceView,
)
from giscode.projects import Project, ProjectStore
from giscode.storage import ChatStore


def _enabled(button) -> bool:
    return "disabled" not in button.state()


def _destroy_view(tk_root, view) -> None:
    """Drop this test's widgets before the next test builds more.

    The view owns ``tkinter.Variable`` objects (status lines, plan reply).
    Left packed on the shared root they stay alive until the root dies, and a
    later send worker can trigger a garbage collection that finalizes some of
    them on the worker thread - ``Variable.__del__`` then calls Tcl off the
    main loop and the worker blocks forever. Destroying the frame here keeps
    this module's views out of the next test's heap.
    """
    try:
        view._parent.destroy()
    except tk.TclError:
        return
    try:
        tk_root.update_idletasks()
    except tk.TclError:
        pass
    # Finalize this test's Tk variables here, on the main thread. Left to a
    # later collection they can be finalized on a send worker thread, whose
    # Tcl call then blocks - the failure mode documented in
    # test_gui_project_workspace_threading.py.
    gc.collect()


def _make_view(tmp_path, tk_root):
    project_store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    chat_store = ChatStore(db_path=str(tmp_path / "chats.db"))
    controller = ProjectWorkspaceController(project_store, chat_store)
    view = ProjectWorkspaceView(tk_root, controller)
    tk_root.update()
    return view, controller, project_store, chat_store


def _create_project(controller, tmp_path, name: str):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    session = controller.create_project(name=name, root_path=str(root))
    return session.project


@pytest.fixture
def plan_view(tk_root, tmp_path):
    view, controller, project_store, chat_store = _make_view(tmp_path, tk_root)
    try:
        yield view, controller
    finally:
        controller.close()
        project_store.close()
        chat_store.close()
        _destroy_view(tk_root, view)
        del view
        gc.collect()


def test_actions_are_disabled_without_a_project(plan_view):
    view, controller = plan_view
    assert controller.has_active_session is False

    assert not _enabled(view._plan_button)
    assert not _enabled(view._save_plan_button)
    assert not _enabled(view._approve_button)


def test_readiness_query_and_request_box_stay_available_without_a_project(plan_view):
    view, _controller = plan_view

    # Get Readiness has no session prerequisite: it reports NO_PLAN.
    assert _enabled(view._readiness_button)
    view._readiness_button.invoke()
    assert "NO_PLAN" in view._plan_reply_var.get()

    # The request box stays editable; its Enter path already explains why it
    # cannot send instead of raising.
    assert str(view._plan_entry.cget("state")) in ("normal", "enabled")
    view._plan_entry.delete(0, tk.END)
    view._plan_entry.insert(0, "Add a login screen")
    assert view._on_plan_request() == "break"
    assert "open a project" in view._plan_reply_var.get().lower()
    assert "open a project" in view._status_var.get().lower()


def test_opening_a_project_enables_the_session_bound_actions(plan_view, tmp_path):
    view, controller = plan_view
    project = _create_project(controller, tmp_path, "PlanProj")

    view.show_active_project(project.name, project.id, project.root_path)

    assert controller.has_active_session is True
    assert _enabled(view._plan_button)
    assert _enabled(view._save_plan_button)
    assert _enabled(view._approve_button)
    assert _enabled(view._readiness_button)


def test_switching_projects_keeps_the_actions_enabled(plan_view, tmp_path):
    view, controller = plan_view
    first = _create_project(controller, tmp_path, "FirstProj")
    second = _create_project(controller, tmp_path, "SecondProj")

    view.show_active_project(first.name, first.id, first.root_path)
    assert _enabled(view._save_plan_button)

    view.show_active_project(second.name, second.id, second.root_path)
    assert controller.current_project_id == second.id
    assert _enabled(view._plan_button)
    assert _enabled(view._save_plan_button)
    assert _enabled(view._approve_button)


def test_closing_the_project_disables_them_again(plan_view, tmp_path):
    view, controller = plan_view
    project = _create_project(controller, tmp_path, "CloseProj")
    view.show_active_project(project.name, project.id, project.root_path)
    assert _enabled(view._save_plan_button)

    # What MainWindow._sync_project_workspace_display calls with no selection.
    view.show_active_project("None", None, None)

    assert controller.has_active_session is False
    assert not _enabled(view._plan_button)
    assert not _enabled(view._save_plan_button)
    assert not _enabled(view._approve_button)
    assert _enabled(view._readiness_button)


def test_lifecycle_label_reports_no_project_without_a_session(plan_view):
    view, controller = plan_view
    assert controller.has_active_session is False
    assert view._lifecycle_var.get() == "NO_PROJECT"

    # What MainWindow._sync_project_workspace_display calls with no selection.
    view.show_active_project("None", None, None)
    assert view._lifecycle_var.get() == "NO_PROJECT"


def test_opening_a_project_shows_its_lifecycle_state(plan_view, tmp_path):
    view, controller = plan_view
    project = _create_project(controller, tmp_path, "LabelProj")

    view.show_active_project(project.name, project.id, project.root_path)

    assert controller.lifecycle is not None
    assert view._lifecycle_var.get() == controller.lifecycle.name
    assert view._lifecycle_var.get() != "NO_PROJECT"


def test_switching_projects_refreshes_the_lifecycle_label(plan_view, tmp_path):
    view, controller = plan_view
    first = _create_project(controller, tmp_path, "LabelFirst")
    second = _create_project(controller, tmp_path, "LabelSecond")

    view.show_active_project(first.name, first.id, first.root_path)
    assert view._lifecycle_var.get() == controller.lifecycle.name

    view.show_active_project(second.name, second.id, second.root_path)
    assert controller.current_project_id == second.id
    assert view._lifecycle_var.get() == controller.lifecycle.name
    assert view._lifecycle_var.get() != "NO_PROJECT"


def test_closing_the_project_restores_the_no_project_label(plan_view, tmp_path):
    view, controller = plan_view
    project = _create_project(controller, tmp_path, "LabelClose")
    view.show_active_project(project.name, project.id, project.root_path)
    assert view._lifecycle_var.get() != "NO_PROJECT"

    view.show_active_project("None", None, None)

    assert controller.has_active_session is False
    assert view._lifecycle_var.get() == "NO_PROJECT"


def test_label_refresh_neither_calls_a_model_nor_changes_the_plan(
    plan_view, tmp_path, monkeypatch
):
    """Reading the label must stay a local, read-only inspection."""
    view, controller = plan_view
    project = _create_project(controller, tmp_path, "LabelQuiet")
    readiness_calls: list[str] = []

    def _no_model(message):  # pragma: no cover - must never run
        raise AssertionError("label refresh must not send a planer message")

    monkeypatch.setattr(controller, "send_planer_message", _no_model)
    real_readiness = controller.get_readiness

    def _counting_readiness():
        readiness_calls.append("readiness")
        return real_readiness()

    monkeypatch.setattr(controller, "get_readiness", _counting_readiness)

    before = {p.id: dict(p.plan) for p in controller.list_projects()}
    view.show_active_project(project.name, project.id, project.root_path)
    after = {p.id: dict(p.plan) for p in controller.list_projects()}

    assert readiness_calls, "the label should be refreshed from readiness"
    assert before == after


def test_enabled_buttons_still_invoke_the_original_handlers(plan_view, tmp_path, monkeypatch):
    view, controller = plan_view
    project = _create_project(controller, tmp_path, "WiredProj")
    view.show_active_project(project.name, project.id, project.root_path)

    seen: list[str] = []
    monkeypatch.setattr(controller, "save_plan", lambda: seen.append("save"))
    monkeypatch.setattr(
        controller,
        "get_readiness",
        lambda: {"plan_readiness": "DRAFT", "is_ready_for_review": False},
    )
    # The Planer surface now calls the build handoff entry point; the approval
    # call itself is still what it routes to. Returning None keeps this test's
    # fake in the "approval only, nothing launched" shape.
    monkeypatch.setattr(
        controller, "approve_and_start_build", lambda *a, **k: seen.append("start")
    )

    view._save_plan_button.invoke()
    assert "saved" in view._plan_reply_var.get().lower()

    view._readiness_button.invoke()
    assert "DRAFT" in view._plan_reply_var.get()

    view._approve_button.invoke()
    assert seen == ["save", "start"]
    assert "approved" in view._plan_reply_var.get().lower()


def test_duck_typed_controller_without_session_flag_keeps_actions_enabled(tk_root):
    """Legacy controllers that never reported ``has_active_session`` behaved
    as if a session existed; that contract is preserved."""
    class _LegacyController:
        def __init__(self):
            self.calls = []
            self._planer_inference = None

        def list_projects(self):
            return []

        def get_readiness(self):
            return {"plan_readiness": "DRAFT", "is_ready_for_review": False}

        def save_plan(self):
            self.calls.append("save")

        def approve_and_start(self, *a, **k):
            self.calls.append("start")

        def send_planer_message(self, message):
            raise AssertionError("not used")

        def select_existing_project_root(self, root_path):
            self.calls.append(("root", root_path))

    controller = _LegacyController()
    view = ProjectWorkspaceView(tk_root, controller)
    try:
        tk_root.update()
        assert _enabled(view._plan_button)
        assert _enabled(view._save_plan_button)
        assert _enabled(view._approve_button)
    finally:
        _destroy_view(tk_root, view)


def test_state_is_recomputed_when_the_session_disappears_underneath(plan_view, tmp_path):
    """A failed selection sync must not leave stale enabled buttons."""
    view, controller = plan_view
    project = _create_project(controller, tmp_path, "GoneProj")
    view.show_active_project(project.name, project.id, project.root_path)
    assert _enabled(view._save_plan_button)

    controller.close()
    view._refresh_all()

    assert not _enabled(view._save_plan_button)
    assert not _enabled(view._plan_button)
    assert not _enabled(view._approve_button)
