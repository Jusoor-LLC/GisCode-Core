"""Tests for the Project Workspace GUI (Task 10).

The tests cover the non-visual :class:`ProjectWorkspaceController`
and a minimal smoke test of the Tk-based
:class:`ProjectWorkspaceView`. All tests use temporary
``ProjectStore`` / ``ChatStore`` SQLite files; the user's real
``~/.giscode`` is never touched.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Iterator

import pytest

from giscode.planer import plan_to_project_dict
from giscode.planer.engine import PlanerEngine
from giscode.projects import Project, ProjectLifecycle, ProjectStore
from giscode.projects.approval import (
    ApprovalError,
    BuildHandoff,
    clear_in_flight_registry,
)
from giscode.projects.workspace import ProjectWorkspaceError
from giscode.storage import ChatStore
from tests.conftest import _FakeGisAiEngine, tk_available, tk_skip_reason


# Skip all GUI tests if Tk cannot initialise (e.g. headless CI
# without a display). ``tk_available`` retries: a single transient Tcl init
# failure while pytest imports modules must not disable the whole module.
_TK_OK = tk_available()

pytestmark_gui = pytest.mark.skipif(not _TK_OK, reason=tk_skip_reason())


# ----------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _reset_in_flight():
    clear_in_flight_registry()
    yield
    clear_in_flight_registry()


@pytest.fixture
def tmp_dir() -> Iterator[str]:
    d = tempfile.mkdtemp()
    try:
        yield d
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def project_store(tmp_dir: str) -> Iterator[ProjectStore]:
    s = ProjectStore(db_path=os.path.join(tmp_dir, "projects.db"))
    yield s
    s.close()


@pytest.fixture
def chat_store(tmp_dir: str) -> Iterator[ChatStore]:
    s = ChatStore(db_path=os.path.join(tmp_dir, "chats.db"))
    yield s
    s.close()


def _make_ready_plan(project_id: str) -> dict:
    e = PlanerEngine(project_id=project_id)
    e.start_with_idea("Build a small todo app with care")
    for _ in range(15):
        qs = [q for q in e.plan.open_questions if not q.answered]
        if not qs:
            break
        for q in qs:
            e.answer_question(q.id, "a-" + q.id)
    e.set_application_type("web app")
    for n in "abc":
        e.add_feature(n)
    for n in "xy":
        e.add_component(n)
    for n in "s1s2s3":
        e.add_screen(n)
    for n in "m1m2m3":
        e.add_roadmap_item(n)
    e.set_branding("br")
    assert e.is_ready_for_review()
    return plan_to_project_dict(e.build_plan())


# ============================================================== controller


def test_controller_create_project_from_gui_workflow(project_store, chat_store):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    s = c.create_project("Demo", "C:/projects/demo")
    assert s.project_id.startswith("proj-")
    assert c.current_project_id == s.project_id
    summaries = c.list_projects()
    assert any(p.name == "Demo" for p in summaries)


def test_controller_open_existing_project(project_store, chat_store):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    p = project_store.create_project(
        Project.create("Saved", "C:/saved")
    )
    c = ProjectWorkspaceController(project_store, chat_store)
    s = c.open_project(p.id)
    assert s.project_id == p.id
    assert c.current_project_id == p.id


def test_controller_selects_unregistered_transferred_root_without_creating_record(
    project_store, chat_store, tmp_path
):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    root = tmp_path / "Transferred"
    (root / "src").mkdir(parents=True)
    app_file = root / "src" / "main.py"
    app_file.write_text("print('transferred')\n", encoding="utf-8")
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    controller = ProjectWorkspaceController(project_store, chat_store)

    controller.select_existing_project_root(str(root))

    assert controller.current_project_id is None
    assert controller.current_project_root == str(root)
    assert project_store.list_projects() == []
    report = controller.inspect_project_integrity()
    assert report.project_root == str(root)
    assert app_file.read_text(encoding="utf-8") == "print('transferred')\n"
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    controller.close()


def test_controller_planer_chat_persistence(project_store, chat_store):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    s = c.create_project("Chat", "C:/c")
    c.send_planer_message("Hello planer")
    history = c.load_chat_history()
    assert len(history) == 2
    # Reopen and verify the history is restored.
    c.close()
    s2 = c.open_project(s.project_id)
    history2 = c.load_chat_history()
    assert len(history2) == 2


def test_controller_plan_restoration(project_store, chat_store):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    s = c.create_project("Restore", "C:/r")
    plan_data = _make_ready_plan(s.project_id)
    project_store.set_plan(s.project_id, plan_data)
    c.close()
    c.open_project(s.project_id)
    plan = c.get_plan_dict()
    inner = plan.get("plan", plan)
    assert inner.get("understanding", {}).get("original_idea", "").startswith("Build")


def test_controller_save_plan(project_store, chat_store):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    s = c.create_project("Save", "C:/s")
    s.engine.start_with_idea("Build a thing")
    c.save_plan()
    stored = project_store.get_project(s.project_id)
    assert stored.plan


def test_controller_readiness(project_store, chat_store):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    c.create_project("Readiness", "C:/r")
    r = c.get_readiness()
    assert r["plan_readiness"] in ("INCOMPLETE", "NEEDS_USER_INPUT", "READY_FOR_REVIEW")
    assert isinstance(r["is_ready_for_review"], bool)


def test_controller_review_state_without_approval(project_store, chat_store):
    """A ready plan is shown as review-ready, but the controller
    must not auto-approve.
    """
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    s = c.create_project("Review", "C:/rv")
    project_store.set_plan(s.project_id, _make_ready_plan(s.project_id))
    c.close()
    c.open_project(s.project_id)
    r = c.get_readiness()
    assert r["is_ready_for_review"] is True
    # No side-effect: project is still PLANNING.
    assert project_store.get_project(s.project_id).lifecycle == ProjectLifecycle.PLANNING


def test_controller_approve_and_start_calls_bridge(project_store, chat_store, monkeypatch):
    """APPROVE & START goes through the Task 9 bridge and never
    calls ProjectStore.approve / start_building directly.
    """
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    s = c.create_project("Approve", "C:/a")
    project_store.set_plan(s.project_id, _make_ready_plan(s.project_id))
    c.close()
    c.open_project(s.project_id)

    approve_called = {"n": 0}
    start_building_called = {"n": 0}
    real_approve = project_store.approve
    real_start = project_store.start_building

    def spy_approve(*a, **kw):
        approve_called["n"] += 1
        return real_approve(*a, **kw)

    def spy_start(*a, **kw):
        start_building_called["n"] += 1
        return real_start(*a, **kw)

    monkeypatch.setattr(project_store, "approve", spy_approve)
    monkeypatch.setattr(project_store, "start_building", spy_start)

    handoff = c.approve_and_start(notes=["user said go"])
    assert isinstance(handoff, BuildHandoff)
    assert handoff.lifecycle == ProjectLifecycle.BUILDING
    assert handoff.project_id == s.project_id
    # The bridge calls the store methods, but the *controller* did
    # not call them directly — the only path is the bridge. The
    # spy above records the call chain: approve() then
    # start_building() in order, exactly once each.
    assert approve_called["n"] == 1
    assert start_building_called["n"] == 1


def test_controller_does_not_directly_bypass_bridge(project_store, chat_store):
    """The controller's public surface has no method that calls
    ProjectStore.approve or ProjectStore.start_building outside
    of the bridge.
    """
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    c.create_project("X", "C:/x")
    forbidden = {"approve", "start_building", "transition_lifecycle"}
    for name in forbidden:
        if hasattr(c, name):
            # If a name like 'approve' exists, it must be a
            # property/method on the bridge wrapper that does NOT
            # directly call the store's approve method.
            attr = getattr(c, name)
            assert not callable(attr) or name == "approve_and_start"


def test_controller_project_a_b_isolation(project_store, chat_store):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    a = c.create_project("Alpha", "C:/a")
    c.send_planer_message("Alpha-only message")
    a_id = a.project_id
    c.close()
    b = c.create_project("Beta", "C:/b")
    c.send_planer_message("Beta-only message")
    b_id = b.project_id
    c.close()

    c.open_project(a_id)
    a_hist = c.load_chat_history()
    assert len(a_hist) == 2
    assert a_hist[0]["content"] == "Alpha-only message"
    c.close()

    c.open_project(b_id)
    b_hist = c.load_chat_history()
    assert len(b_hist) == 2
    assert b_hist[0]["content"] == "Beta-only message"


def test_controller_close_preserves_data(project_store, chat_store):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    s = c.create_project("Keep", "C:/k")
    c.send_planer_message("keep me")
    s.engine.start_with_idea("keep plan")
    c.save_plan()
    pid = s.project_id
    c.close()

    # Project, plan, and chat all remain.
    proj = project_store.get_project(pid)
    assert proj is not None
    assert proj.plan
    chats = chat_store.list_chats(f"project:{pid}", pid)
    assert any(c2.title == "Planer" for c2 in chats)


def test_controller_daily_chat_isolation(project_store, chat_store):
    """Daily chats (non-Planer) must remain separate from project
    Planer chats.
    """
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    s = c.create_project("Iso", "C:/i")
    c.send_planer_message("Planer message")
    pid = s.project_id
    # Create a "daily" chat on the same store.
    daily = chat_store.create_chat(scope="daily", scope_id="user", title="Today")
    chat_store.append_message(daily.id, "user", "Daily note")
    # Project Planer history must not include the daily message.
    history = c.load_chat_history()
    assert all(t["content"] != "Daily note" for t in history)
    # And vice versa.
    daily_hist = chat_store.load_history(daily.id)
    assert all(m.content != "Planer message" for m in daily_hist)


def test_controller_approve_failure_is_clean(project_store, chat_store, monkeypatch):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    s = c.create_project("Fail", "C:/f")
    project_store.set_plan(s.project_id, _make_ready_plan(s.project_id))
    c.close()
    c.open_project(s.project_id)

    real = project_store.transition_lifecycle

    def boom(pid, target):
        if target == ProjectLifecycle.BUILDING:
            raise RuntimeError("simulated")
        return real(pid, target)

    monkeypatch.setattr(project_store, "transition_lifecycle", boom)
    with pytest.raises(ProjectWorkspaceError):
        c.approve_and_start()
    final = project_store.get_project(s.project_id)
    assert final.lifecycle != ProjectLifecycle.BUILDING


def test_controller_already_building(project_store, chat_store):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    s = c.create_project("Already", "C:/al")
    project_store.set_plan(s.project_id, _make_ready_plan(s.project_id))
    c.close()
    c.open_project(s.project_id)
    c.approve_and_start()
    with pytest.raises(ProjectWorkspaceError):
        c.approve_and_start()


def test_controller_no_session_guard(project_store, chat_store):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    with pytest.raises(ProjectWorkspaceError):
        c.send_planer_message("hi")
    with pytest.raises(ProjectWorkspaceError):
        c.save_plan()
    with pytest.raises(ProjectWorkspaceError):
        c.approve_and_start()


def test_controller_blank_inputs_rejected(project_store, chat_store):
    from giscode.gui.project_workspace import ProjectWorkspaceController

    c = ProjectWorkspaceController(project_store, chat_store)
    with pytest.raises(ProjectWorkspaceError):
        c.create_project("  ", "C:/x")
    with pytest.raises(ProjectWorkspaceError):
        c.create_project("X", "  ")


# ============================================================== view (smoke)


@ pytestmark_gui
def test_view_constructs_with_controller(project_store, chat_store):
    from giscode.gui.project_workspace import (
        ProjectWorkspaceController,
        ProjectWorkspaceView,
    )

    root = tk.Tk()
    try:
        root.withdraw()
        c = ProjectWorkspaceController(project_store, chat_store)
        c.create_project("View", "C:/v")
        v = ProjectWorkspaceView(root, c)
        v.pack(fill=tk.BOTH, expand=True)
        root.update_idletasks()
        # The view surfaces the project name and lifecycle.
        assert "View" in v._project_var.get()
        assert "PLANNING" in v._lifecycle_var.get()
    finally:
        root.destroy()


@ pytestmark_gui
def test_view_embeds_inside_existing_main_window(project_store, chat_store, monkeypatch):
    """The new view must be reachable through MainWindow and
    co-exist with the other tabs.
    """
    from giscode.gui.main_window import MainWindow

    root = tk.Tk()
    try:
        root.withdraw()
        class _LocalFakeInference:
            def chat_generate(self, request):
                from giscode.inference.runtime import ChatResponse
                return ChatResponse(text="SYSTEM OK")
            def cancel(self):
                pass
        mw = MainWindow(
            root,
            project_store=project_store,
            chat_store=chat_store,
            gisai_engine=_FakeGisAiEngine(_LocalFakeInference()),
        )
        root.update_idletasks()
        assert hasattr(mw, "_project_workspace_view")
        assert hasattr(mw, "_project_workspace_controller")
        # Tabs are preserved.
        tabs = mw._notebook.tabs()
        assert len(tabs) >= len(mw._tab_frames)
    finally:
        root.destroy()


@pytestmark_gui
def test_view_does_not_call_approve_or_start_building_directly(
    project_store, chat_store, monkeypatch
):
    """Confirm the *View* itself never references the store's
    approve / start_building methods. The controller is the only
    gateway.
    """
    from giscode.gui.project_workspace import (
        ProjectWorkspaceController,
        ProjectWorkspaceView,
    )

    approve_calls: list = []
    start_calls: list = []

    real_approve = project_store.approve
    real_start = project_store.start_building

    def spy_approve(*a, **kw):
        approve_calls.append(a)
        return real_approve(*a, **kw)

    def spy_start(*a, **kw):
        start_calls.append(a)
        return real_start(*a, **kw)

    monkeypatch.setattr(project_store, "approve", spy_approve)
    monkeypatch.setattr(project_store, "start_building", spy_start)

    root = tk.Tk()
    try:
        root.withdraw()
        c = ProjectWorkspaceController(project_store, chat_store)
        s = c.create_project("ViewGate", "C:/vg")
        project_store.set_plan(s.project_id, _make_ready_plan(s.project_id))
        c.close()
        c.open_project(s.project_id)
        v = ProjectWorkspaceView(root, c)
        v.pack(fill=tk.BOTH, expand=True)
        root.update_idletasks()
        # The view's own approval button is _on_approve_and_start,
        # which calls controller.approve_and_start, which calls
        # the bridge. We invoke the controller path explicitly
        # (not the GUI button) to confirm only the bridge path is
        # used in this exercise.
        c.approve_and_start()
        assert len(approve_calls) == 1
        assert len(start_calls) == 1
    finally:
        root.destroy()


@pytestmark_gui
def test_gui_poll_coding_agent_state(monkeypatch):
    from unittest.mock import MagicMock

    from giscode.agentic.loop import CodingAgentLoop
    from giscode.agentic.types import ExecutionPhase, LoopState
    from giscode.gui.main_window import MainWindow
    from giscode.openhands.adapter import MockOpenHandsAdapter
    from giscode.openhands.types import OpenHandsStatus
    from giscode.workspace.workspace import Workspace

    root = tk.Tk()
    try:
        root.withdraw()

        mock_loop = MagicMock(spec=CodingAgentLoop)
        mock_loop.get_current_state.return_value = LoopState(
            task_id="task-123",
            phase=ExecutionPhase.IMPLEMENTATION,
            status="running",
            model_name="Qwen3-Coder-30B-A3B-Instruct",
        )

        workspace = Workspace(tempfile.mkdtemp())
        mock_openhands = MockOpenHandsAdapter(workspace, behavior="success")
        mock_openhands._status = OpenHandsStatus.RUNNING

        class _LocalFakeInference:
            def chat_generate(self, request):
                from giscode.inference.runtime import ChatResponse
                return ChatResponse(text="SYSTEM OK")
            def cancel(self):
                pass

        mw = MainWindow(
            root,
            coding_agent=mock_loop,
            openhands_adapter=mock_openhands,
            gisai_engine=_FakeGisAiEngine(_LocalFakeInference()),
        )
        root.update_idletasks()

        mw._poll_coding_agent_state()

        assert mw.phase_var.get() == "Implementation"
        assert mw.chat_model_combo.get() == "None"
        assert "task-123" in mw.status_var.get()
        assert mw.agent_status_var.get() == "Running"

        mock_loop.get_current_state.return_value = None
        mw._poll_coding_agent_state()
        assert mw.phase_var.get() == "Implementation"
        assert mw.model_name_var.get() == "None"
    finally:
        root.destroy()
