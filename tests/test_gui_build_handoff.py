"""Approve & Start must really hand the approved plan to the build launcher.

Stage 3A found the false-success path: ``approve_and_start`` returned a
``BuildHandoff`` and the Projects tab printed "the build was started", while
nothing in production ever constructed a ``BuildLauncher``. The project stayed
in ``BUILDING`` with no coding work running.

These tests pin the repaired contract:

* the existing ``BuildLauncher`` receives the exact handoff that approval
  produced, and the existing ``CodingAgentLoop`` is what it starts;
* a launcher that cannot be built leaves the project in ``PLANNING`` (so the
  user can retry) instead of in a false ``BUILDING``;
* a launch that fails after approval moves ``BUILDING`` to ``FAILED`` and keeps
  the plan;
* failures, completion and cancellation are reported as themselves;
* the tab never prints "the build was started" unless a launch really returned,
  and Stop Build routes to the loop's own cancel path.

All agents and stores here are fakes or temporary databases; no model server is
contacted and no real project is touched.
"""
from __future__ import annotations

import gc
import os
import tempfile
import threading
import time
from typing import Iterator

import pytest

from giscode.agentic.types import ExecutionPhase, TaskRequest, TaskResult
from giscode.gui.project_workspace import (
    BuildStartResult,
    BuildStatus,
    ProjectWorkspaceController,
    ProjectWorkspaceView,
)
from giscode.planer import plan_to_project_dict
from giscode.planer.engine import PlanerEngine
from giscode.projects import ProjectLifecycle, ProjectStore
from giscode.projects.approval import BuildHandoff, clear_in_flight_registry
from giscode.projects.workspace import ProjectWorkspaceError
from giscode.storage import ChatStore
from tests.conftest import tk_available, tk_skip_reason


_TK_OK = tk_available()
pytestmark_gui = pytest.mark.skipif(
    not _TK_OK, reason=tk_skip_reason()
)


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


def _open_ready(controller, project_store, tmp_path, name: str) -> str:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    session = controller.create_project(name=name, root_path=str(root))
    pid = session.project_id
    project_store.set_plan(pid, _make_ready_plan(pid))
    controller.close()
    controller.open_project(pid)
    return pid


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _RecordingCodingAgent:
    """Stand-in for the application's existing ``CodingAgentLoop``."""

    def __init__(self, *, result=None, raises=None, block: bool = False) -> None:
        self.requests: list[TaskRequest] = []
        self.cancels = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self._result = result or TaskResult(
            task_id="fake-build", status=ExecutionPhase.COMPLETE
        )
        self._raises = raises
        if not block:
            self.release.set()

    def start(self, request):
        self.requests.append(request)
        self.started.set()
        self.release.wait(10)
        if self._raises is not None:
            raise self._raises
        return self._result

    def cancel(self):
        self.cancels += 1
        self._result.status = ExecutionPhase.CANCELLED
        self.release.set()
        return self._result


# ------------------------------------------------- 1. the handoff is real


def test_approval_hands_the_approved_plan_to_the_launcher(
    project_store, chat_store, tmp_path
):
    # Block the fake build until this test releases it in ``finally``: the
    # launcher advances BUILDING -> TESTING -> VERIFICATION as soon as the agent
    # returns, so an unblocked fake races the assertion below.
    agent = _RecordingCodingAgent(block=True)
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    try:
        pid = _open_ready(controller, project_store, tmp_path, "Handoff")
        plan_before = project_store.get_project(pid).plan

        result = controller.approve_and_start_build()

        assert result.status == BuildStatus.RUNNING
        assert result.approved is True
        assert result.launched is True
        assert "running" in result.message.lower()
        assert project_store.get_project(pid).lifecycle == ProjectLifecycle.BUILDING

        assert agent.started.wait(5), "the coding loop was never started"
        assert len(agent.requests) == 1
        request = agent.requests[0]
        assert isinstance(request, TaskRequest)
        assert request.task_id == f"build-{pid}"
        criteria = request.completion_criteria or {}
        assert criteria["project_id"] == pid
        assert criteria["root_path"] == project_store.get_project(pid).root_path
        assert criteria["project_plan"] == plan_before
    finally:
        agent.release.set()
        controller.close()


def test_the_exact_handoff_is_passed_to_the_launcher(
    project_store, chat_store, tmp_path, monkeypatch
):
    agent = _RecordingCodingAgent(block=True)
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    try:
        pid = _open_ready(controller, project_store, tmp_path, "ExactHandoff")
        captured: dict = {}
        real_ensure = controller._ensure_build_launcher

        def ensure():
            launcher = real_ensure()
            real_launch = launcher.launch

            def spy(handoff, **kwargs):
                captured["handoff"] = handoff
                return real_launch(handoff, **kwargs)

            launcher.launch = spy
            return launcher

        monkeypatch.setattr(controller, "_ensure_build_launcher", ensure)

        result = controller.approve_and_start_build()

        assert result.launched is True
        assert agent.started.wait(5)
        handoff = captured.get("handoff")
        assert isinstance(handoff, BuildHandoff)
        assert handoff.project_id == pid
        assert handoff.lifecycle == ProjectLifecycle.BUILDING
        stored = project_store.get_project(pid)
        assert handoff.plan == stored.plan
        assert handoff.root_path == stored.root_path
    finally:
        agent.release.set()
        controller.close()


def test_approval_alone_does_not_invoke_the_coding_agent(
    project_store, chat_store, tmp_path
):
    """Negative control: the approval bridge must not start work by itself."""
    agent = _RecordingCodingAgent()
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    try:
        pid = _open_ready(controller, project_store, tmp_path, "ApprovalOnly")

        handoff = controller.approve_and_start()

        assert isinstance(handoff, BuildHandoff)
        assert agent.requests == []
        assert controller.build_state() is None
        assert project_store.get_project(pid).lifecycle == ProjectLifecycle.BUILDING
    finally:
        agent.release.set()
        controller.close()


# --------------------------------------- 2. construction failures fail closed


def test_launcher_construction_failure_leaves_the_project_in_planning(
    project_store, chat_store, tmp_path, monkeypatch
):
    import giscode.projects.build_launcher as build_launcher_mod

    class _BoomLauncher:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("launcher unavailable")

    monkeypatch.setattr(build_launcher_mod, "BuildLauncher", _BoomLauncher)
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=object()
    )
    try:
        pid = _open_ready(controller, project_store, tmp_path, "NoLauncher")
        plan_before = project_store.get_project(pid).plan

        with pytest.raises(ProjectWorkspaceError) as exc:
            controller.approve_and_start_build()

        assert "launcher unavailable" in str(exc.value)
        stored = project_store.get_project(pid)
        assert stored.lifecycle == ProjectLifecycle.PLANNING
        assert stored.plan == plan_before
        assert controller.build_state() is None
    finally:
        controller.close()


def test_missing_coding_agent_does_not_cross_the_approval_boundary(
    project_store, chat_store, tmp_path
):
    controller = ProjectWorkspaceController(project_store, chat_store)
    try:
        pid = _open_ready(controller, project_store, tmp_path, "NoAgent")

        with pytest.raises(ProjectWorkspaceError) as exc:
            controller.approve_and_start_build()

        assert "no coding agent" in str(exc.value).lower()
        assert project_store.get_project(pid).lifecycle == ProjectLifecycle.PLANNING
    finally:
        controller.close()


# ------------------------------------ 3. a failed launch after approval recovers


def test_launch_startup_failure_does_not_leave_the_project_building(
    project_store, chat_store, tmp_path, monkeypatch
):
    agent = _RecordingCodingAgent()
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )

    class _RefusingLauncher:
        def launch(self, handoff, **kwargs):
            raise RuntimeError("thread refused to start")

        def is_in_flight(self, project_id):
            return False

        def cancel(self, project_id):
            return False

    try:
        pid = _open_ready(controller, project_store, tmp_path, "LaunchFails")
        plan_before = project_store.get_project(pid).plan
        monkeypatch.setattr(
            controller, "_ensure_build_launcher", lambda: _RefusingLauncher()
        )

        result = controller.approve_and_start_build()

        assert result.status == BuildStatus.LAUNCH_FAILED
        assert result.approved is True
        assert result.launched is False
        assert "thread refused to start" in result.message
        assert "FAILED" in result.message
        stored = project_store.get_project(pid)
        assert stored.lifecycle == ProjectLifecycle.FAILED
        assert stored.plan == plan_before
        assert agent.requests == [], "no coding work may run after a failed launch"
    finally:
        agent.release.set()
        controller.close()


def test_recovery_never_rewrites_a_lifecycle_that_is_not_building(
    project_store, chat_store, tmp_path
):
    """Negative control: the recovery helper only touches BUILDING."""
    controller = ProjectWorkspaceController(project_store, chat_store)
    try:
        pid = _open_ready(controller, project_store, tmp_path, "StillPlanning")

        assert controller._recover_build_failure(pid, cancelled=False) is False
        assert project_store.get_project(pid).lifecycle == ProjectLifecycle.PLANNING
    finally:
        controller.close()


# ------------------------------------------- 4. real build outcomes are reported


def test_build_failure_marks_the_project_failed_and_keeps_the_plan(
    project_store, chat_store, tmp_path
):
    agent = _RecordingCodingAgent(raises=RuntimeError("model exploded"))
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    try:
        pid = _open_ready(controller, project_store, tmp_path, "BuildFails")
        plan_before = project_store.get_project(pid).plan

        controller.approve_and_start_build()

        assert _wait_for(
            lambda: controller.build_state(pid) is not None
            and controller.build_state(pid).status == BuildStatus.FAILED
        ), "the build failure was never recorded"
        state = controller.build_state(pid)
        assert "model exploded" in state.message
        stored = project_store.get_project(pid)
        assert stored.lifecycle == ProjectLifecycle.FAILED
        assert stored.plan == plan_before
    finally:
        agent.release.set()
        controller.close()


def test_build_completion_without_project_tests_is_not_verified(
    project_store, chat_store, tmp_path
):
    """A finished loop proves nothing: an absent suite is reported as absent."""
    agent = _RecordingCodingAgent(
        result=TaskResult(task_id="fake", status=ExecutionPhase.COMPLETE)
    )
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    try:
        pid = _open_ready(controller, project_store, tmp_path, "BuildCompletes")

        controller.approve_and_start_build()

        assert _wait_for(
            lambda: controller.build_state(pid) is not None
            and controller.build_state(pid).status == BuildStatus.FAILED
        ), "the unverified completion was never recorded"
        state = controller.build_state(pid)
        assert "no project tests" in state.message.lower()
        assert "passed" not in state.message.lower()
        report = controller.build_verification(pid)
        assert report is not None
        assert report.verified is False
        assert report.outcome.value == "no_tests_run"
        # Nothing was verified, so the project is retryable, not COMPLETE.
        assert project_store.get_project(pid).lifecycle == ProjectLifecycle.FAILED
    finally:
        agent.release.set()
        controller.close()


def test_stop_during_an_active_build_cancels_the_loop_and_the_project(
    project_store, chat_store, tmp_path
):
    agent = _RecordingCodingAgent(block=True)
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    try:
        pid = _open_ready(controller, project_store, tmp_path, "StopBuild")
        plan_before = project_store.get_project(pid).plan

        result = controller.approve_and_start_build()
        assert result.launched is True
        assert agent.started.wait(5)
        assert controller.build_in_flight() is True

        assert controller.cancel_build() is True
        assert agent.cancels == 1
        assert _wait_for(
            lambda: controller.build_state(pid) is not None
            and controller.build_state(pid).status == BuildStatus.CANCELLED
        ), "the cancellation was never recorded"
        stored = project_store.get_project(pid)
        assert stored.lifecycle == ProjectLifecycle.CANCELLED
        assert stored.plan == plan_before
        assert _wait_for(lambda: controller.build_in_flight() is False)
    finally:
        agent.release.set()
        controller.close()


def test_stop_without_a_running_build_is_a_no_op(project_store, chat_store, tmp_path):
    agent = _RecordingCodingAgent()
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    try:
        _open_ready(controller, project_store, tmp_path, "NothingRunning")

        assert controller.cancel_build() is False
        assert agent.cancels == 0
    finally:
        agent.release.set()
        controller.close()


# ------------------------------------------------------------- 5. the view


def _enabled(button) -> bool:
    return "disabled" not in button.state()


def _destroy_view(tk_root, view) -> None:
    try:
        view._parent.destroy()
    except Exception:
        return
    try:
        tk_root.update_idletasks()
    except Exception:
        pass
    gc.collect()


@pytestmark_gui
def test_view_never_claims_a_build_started_after_a_failed_launch(
    tk_root, project_store, chat_store, tmp_path, monkeypatch
):
    controller = ProjectWorkspaceController(project_store, chat_store)
    view = None
    try:
        pid = _open_ready(controller, project_store, tmp_path, "ViewFailure")
        view = ProjectWorkspaceView(tk_root, controller)
        tk_root.update()
        view.show_active_project(
            "ViewFailure", pid, project_store.get_project(pid).root_path
        )
        failure = BuildStartResult(
            status=BuildStatus.LAUNCH_FAILED,
            message=(
                "Approval completed, but the build could not be started: "
                "RuntimeError: boom. The project was moved from BUILDING to FAILED."
            ),
            approved=True,
            launched=False,
            project_id=pid,
            error="RuntimeError: boom",
        )
        monkeypatch.setattr(
            controller, "approve_and_start_build", lambda *a, **k: failure
        )

        view._on_approve_and_start()
        tk_root.update()

        reply = view._plan_reply_var.get()
        status = view._status_var.get()
        assert "could not be started" in reply.lower()
        assert "the build was started" not in reply.lower()
        assert "Build running" != status
        assert "not started" in status.lower()
        # Nothing was launched, so Stop Build is not offered.
        assert not _enabled(view._stop_build_button)
    finally:
        if view is not None:
            _destroy_view(tk_root, view)
            # Drop the last reference here so the view's Tk variables are
            # finalized on this thread by the collect below, not later on a
            # launcher worker thread (see test_gui_project_workspace_threading).
            del view
        gc.collect()
        controller.close()


@pytestmark_gui
def test_view_shows_a_running_build_and_enables_stop(
    tk_root, project_store, chat_store, tmp_path
):
    agent = _RecordingCodingAgent(block=True)
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    view = None
    try:
        pid = _open_ready(controller, project_store, tmp_path, "ViewRunning")
        view = ProjectWorkspaceView(tk_root, controller)
        tk_root.update()
        view.show_active_project(
            "ViewRunning", pid, project_store.get_project(pid).root_path
        )
        assert _enabled(view._approve_button)

        view._on_approve_and_start()
        tk_root.update()

        assert "running" in view._plan_reply_var.get().lower()
        assert _enabled(view._stop_build_button)
        assert not _enabled(view._approve_button)

        agent.release.set()
        # Verification runs on the launcher thread after the build finishes, so
        # wait for its real outcome rather than the end of the launch.
        assert _wait_for(
            lambda: controller.build_state(pid) is not None
            and controller.build_state(pid).status == BuildStatus.FAILED
        ), "verification never finished"
        view._poll_build_state()
        tk_root.update()

        assert not _enabled(view._stop_build_button)
        # This project has no tests, so the tab reports a failure to verify and
        # never claims success.
        assert "failed" in view._status_var.get().lower()
        assert "no project tests" in view._plan_reply_var.get().lower()
        assert not _enabled(view._approve_button)
        # Not verified, but retryable.
        assert _enabled(view._retry_button)
    finally:
        agent.release.set()
        # Let the fake worker finish before the widgets go away, so no launcher
        # callback can touch Tk state from a background thread.
        _wait_for(lambda: controller.build_in_flight() is False, timeout=5)
        if view is not None:
            _destroy_view(tk_root, view)
            del view
        gc.collect()
        controller.close()
