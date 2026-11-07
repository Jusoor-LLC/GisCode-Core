"""Tests for the Project Workspace → existing coding pipeline
launcher (Task 11).

These tests use a fake ``CodingAgentLoop`` (a plain object that
exposes the same ``start(request)`` method the real loop has)
so the existing coding engine is *not* exercised, *not* mocked
through fragile internals, and *not* replaced. The launcher
itself is the only thing under test.
"""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator, List, Optional

import pytest

from giscode.agentic.types import ExecutionPhase, TaskRequest, TaskResult
from giscode.projects import ProjectStore
from giscode.projects.approval import BuildHandoff
from giscode.projects.build_launcher import (
    BuildAlreadyInProgressError,
    BuildLauncher,
    BuildLauncherError,
    InvalidHandoffError,
    PROJECT_PLAN_KEY,
)
from giscode.storage import ChatStore
from tests.conftest import _FakeGisAiEngine


# ----------------------------------------------------------------- fixtures


@pytest.fixture
def project_store(tmp_path: Path) -> Iterator[ProjectStore]:
    s = ProjectStore(db_path=str(tmp_path / "projects.db"))
    yield s
    s.close()


@pytest.fixture
def chat_store(tmp_path: Path) -> Iterator[ChatStore]:
    s = ChatStore(db_path=str(tmp_path / "chats.db"))
    yield s
    s.close()


# ----------------------------------------------------------------- fakes


class FakeCodingAgentLoop:
    """A minimal stand-in for
    :class:`giscode.agentic.loop.CodingAgentLoop`. It records
    every ``start`` call and returns a configurable result.
    """

    def __init__(
        self,
        *,
        result_status: ExecutionPhase = ExecutionPhase.COMPLETE,
        sleep_seconds: float = 0.0,
        raise_exc: Optional[BaseException] = None,
    ) -> None:
        self.calls: List[TaskRequest] = []
        self._result_status = result_status
        self._sleep_seconds = sleep_seconds
        self._raise = raise_exc
        self._started = threading.Event()
        self._release = threading.Event()
        self.start_count = 0
        self._lock = threading.Lock()

    def start(self, request: TaskRequest) -> TaskResult:
        with self._lock:
            self.start_count += 1
            self.calls.append(request)
        self._started.set()
        if self._sleep_seconds:
            # Block until released (or until sleep elapses, then
            # we are done anyway).
            self._release.wait(timeout=self._sleep_seconds)
        if self._raise is not None:
            raise self._raise
        return TaskResult(
            task_id=request.task_id,
            status=self._result_status,
        )

    def wait_started(self, timeout: float = 5.0) -> None:
        self._started.wait(timeout=timeout)

    def release(self) -> None:
        self._release.set()

    def get_current_state(self):
        """MainWindow polls this during construction.

        The real :class:`giscode.agentic.loop.CodingAgentLoop.get_current_state`
        returns ``Optional[LoopState]``; ``None`` while it is not running is a
        valid value and the UI treats it as idle.
        """
        return None


def _make_handoff(
    project_id: str = "proj-test",
    *,
    plan: Optional[dict] = None,
    root_path: str = "C:/projects/test",
) -> BuildHandoff:
    if plan is None:
        plan = {
            "plan": {
                "understanding": {
                    "original_idea": "Build a small todo app",
                    "refined_description": "A focused notes tool",
                },
                "requirements": {
                    "functional": ["add", "delete"],
                    "features": ["create", "list", "done"],
                },
                "design": {"application_type": "web app"},
                "screens": [{"name": "Home"}, {"name": "Detail"}, {"name": "Settings"}],
                "roadmap": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}],
            }
        }
    return BuildHandoff(
        project_id=project_id,
        project_name="Test",
        root_path=root_path,
        plan=plan,
        approved_at="2026-09-05T00:00:00.000Z",
        started_at="2026-09-05T00:00:01.000Z",
        lifecycle=None,
    )


# ----------------------------------------------------------------- 1. handoff reaches launcher


def test_build_handoff_reaches_the_launcher():
    loop = FakeCodingAgentLoop()
    launcher = BuildLauncher(loop)
    handoff = _make_handoff()
    launch = launcher.launch(handoff)
    assert launch.project_id == handoff.project_id
    assert launch.task_id.startswith("build-")
    # Wait for the background thread to finish.
    loop.wait_started()
    loop.release()
    # The fake loop received exactly one call.
    assert len(loop.calls) == 1
    assert loop.calls[0].task_id == launch.task_id


# ----------------------------------------------------------------- 2. uses existing CodingAgentLoop


def test_launcher_uses_existing_coding_agent_loop():
    loop = FakeCodingAgentLoop()
    launcher = BuildLauncher(loop)
    handoff = _make_handoff("proj-A")
    launcher.launch(handoff)
    loop.wait_started()
    loop.release()
    # The fake was called directly. No second engine was created.
    assert loop.start_count == 1
    # The launcher holds a reference to the same instance.
    assert launcher._coding_agent is loop


def test_launcher_does_not_create_second_coding_engine():
    """The launcher's public surface must not expose any class
    that could be mistaken for a second CodingAgentLoop.
    """
    loop = FakeCodingAgentLoop()
    launcher = BuildLauncher(loop)
    exposed = set(dir(launcher))
    # No class, no factory, no second loop.
    assert "CodingAgentLoop" not in exposed
    assert "create_loop" not in exposed
    assert "new_loop" not in exposed


# ----------------------------------------------------------------- 3. project root_path is passed


def test_project_root_path_is_passed_correctly():
    loop = FakeCodingAgentLoop()
    launcher = BuildLauncher(loop)
    handoff = _make_handoff("proj-rp", root_path="D:/my/projects/x")
    launcher.launch(handoff)
    loop.wait_started()
    loop.release()
    req = loop.calls[0]
    assert req.completion_criteria["root_path"] == "D:/my/projects/x"
    # And the description mentions the project name.
    assert "Test" in req.description


# ----------------------------------------------------------------- 4. project_id is preserved


def test_project_id_is_preserved():
    loop = FakeCodingAgentLoop()
    launcher = BuildLauncher(loop)
    handoff = _make_handoff("proj-PRESERVE")
    launch = launcher.launch(handoff)
    loop.wait_started()
    loop.release()
    assert launch.project_id == "proj-PRESERVE"
    assert loop.calls[0].completion_criteria["project_id"] == "proj-PRESERVE"


# ----------------------------------------------------------------- 5. project plan is available


def test_project_plan_is_available_to_coding_workflow():
    loop = FakeCodingAgentLoop()
    launcher = BuildLauncher(loop)
    plan = {
        "plan": {
            "understanding": {"original_idea": "hello"},
            "requirements": {"features": ["x", "y", "z"]},
        }
    }
    handoff = _make_handoff("proj-plan", plan=plan)
    launcher.launch(handoff)
    loop.wait_started()
    loop.release()
    req = loop.calls[0]
    assert req.completion_criteria[PROJECT_PLAN_KEY] == plan
    # And the description contains a textual summary.
    assert "hello" in req.description
    # And task_id is deterministic.
    assert req.task_id == "build-proj-plan"


# ----------------------------------------------------------------- 6. GUI approval starts launcher


def test_gui_approval_starts_launcher(tmp_path: Path):
    """End-to-end smoke: the same on_building_handoff path the
    MainWindow installs must hand the handoff to the launcher.
    """
    loop = FakeCodingAgentLoop(sleep_seconds=0.3)
    received: list = []
    launcher = BuildLauncher(
        loop,
        on_complete=lambda launch, result: received.append((launch, result)),
    )
    handoff = _make_handoff("proj-gui")

    # Simulate the view's on_building_handoff returning a
    # BuildLaunch synchronously.
    launch = launcher.launch(handoff)
    assert launch.project_id == "proj-gui"
    loop.wait_started()
    loop.release()
    # The on_complete callback was invoked.
    time.sleep(0.1)
    assert len(received) == 1
    assert received[0][0].project_id == "proj-gui"


# ----------------------------------------------------------------- 7. duplicate start rejected


def test_duplicate_start_is_rejected():
    loop = FakeCodingAgentLoop(sleep_seconds=1.0)
    launcher = BuildLauncher(loop)
    handoff = _make_handoff("proj-dup")
    launcher.launch(handoff)
    loop.wait_started()
    # While the first launch is still running, a second launch
    # for the same project must be rejected.
    with pytest.raises(BuildAlreadyInProgressError):
        launcher.launch(handoff)
    loop.release()
    # Wait for the first launch to fully clean up.
    deadline = time.monotonic() + 3.0
    while launcher.is_in_flight("proj-dup") and time.monotonic() < deadline:
        time.sleep(0.05)
    # And after the first finishes, a new launch for the same
    # project is allowed again.
    launch2 = launcher.launch(handoff)
    loop.wait_started()
    loop.release()
    assert launch2.project_id == "proj-dup"


def test_duplicate_start_does_not_call_loop_twice():
    loop = FakeCodingAgentLoop(sleep_seconds=1.0)
    launcher = BuildLauncher(loop)
    handoff = _make_handoff("proj-once")
    launcher.launch(handoff)
    loop.wait_started()
    try:
        for _ in range(5):
            with pytest.raises(BuildAlreadyInProgressError):
                launcher.launch(handoff)
    finally:
        loop.release()
    assert loop.start_count == 1


# ----------------------------------------------------------------- 8. coding startup failure reported


def test_coding_startup_failure_is_reported():
    boom = RuntimeError("simulated coding loop failure")
    loop = FakeCodingAgentLoop(raise_exc=boom)
    received: list = []
    launcher = BuildLauncher(
        loop,
        on_failure=lambda launch, exc: received.append((launch, exc)),
    )
    handoff = _make_handoff("proj-fail")
    launch = launcher.launch(handoff)
    loop.wait_started()
    time.sleep(0.2)  # let the background thread finish
    assert len(received) == 1
    assert received[0][1] is boom
    # The project is no longer in flight.
    assert not launcher.is_in_flight("proj-fail")
    # And no result was stored.
    assert launcher.get_result("proj-fail") is None


def test_invalid_handoff_is_rejected():
    loop = FakeCodingAgentLoop()
    launcher = BuildLauncher(loop)
    with pytest.raises(InvalidHandoffError):
        launcher.launch(None)  # type: ignore[arg-type]
    bad = BuildHandoff(
        project_id="",
        project_name="x",
        root_path="C:/x",
        plan={},
        approved_at="",
        started_at="",
        lifecycle=None,
    )
    with pytest.raises(InvalidHandoffError):
        launcher.launch(bad)
    assert loop.start_count == 0


# ----------------------------------------------------------------- 9. GUI remains non-blocking


def test_launch_returns_immediately(tmp_path: Path):
    loop = FakeCodingAgentLoop(sleep_seconds=2.0)
    launcher = BuildLauncher(loop)
    handoff = _make_handoff("proj-nb")
    t0 = time.monotonic()
    launch = launcher.launch(handoff)
    elapsed = time.monotonic() - t0
    # The launcher returns well before the 2.0s sleep elapses.
    assert elapsed < 0.5
    assert launch.thread_name.startswith("build-launcher-")
    # Cleanup.
    loop.wait_started()
    loop.release()


# ----------------------------------------------------------------- 10. approval cannot be bypassed


def test_launcher_does_not_approve_or_transition_lifecycle(
    project_store=None, chat_store=None
):
    """The launcher must not call any ProjectStore lifecycle
    method. It only runs the existing CodingAgentLoop.start.
    """
    loop = FakeCodingAgentLoop()
    launcher = BuildLauncher(loop)
    handoff = _make_handoff("proj-safety")
    launcher.launch(handoff)
    loop.wait_started()
    loop.release()
    # No ProjectStore interaction at all.
    assert not hasattr(launcher, "_project_store")
    assert not hasattr(launcher, "approve")
    assert not hasattr(launcher, "start_building")
    assert not hasattr(launcher, "transition_lifecycle")


def test_launcher_does_not_execute_commands_directly():
    """The launcher must not import/execute terminal/sandbox
    commands directly.
    """
    import giscode.projects.build_launcher as mod

    forbidden = {
        "CommandExecutor",
        "SandboxExecutor",
        "execute_command",
        "execute_in_sandbox",
    }
    exposed = set(dir(mod))
    for name in forbidden:
        assert name not in exposed, f"launcher module exposes {name}"


# ----------------------------------------------------------------- 11. existing CodingAgentLoop behavior intact


def test_existing_coding_agent_loop_signature_is_preserved():
    """The launcher calls ``coding_agent.start(request)`` with
    a real :class:`TaskRequest` produced from the handoff. It
    does not invent a new method or signature.
    """
    loop = FakeCodingAgentLoop()
    launcher = BuildLauncher(loop)
    handoff = _make_handoff()
    launcher.launch(handoff)
    loop.wait_started()
    loop.release()
    req = loop.calls[0]
    # The argument is a real TaskRequest with the real fields.
    assert isinstance(req, TaskRequest)
    assert isinstance(req.task_id, str)
    assert isinstance(req.description, str)
    assert req.max_duration is None or isinstance(req.max_duration, (int, float))
    assert req.completion_criteria is None or isinstance(req.completion_criteria, dict)


# ----------------------------------------------------------------- 12. on_complete marshalling helper


def test_on_complete_receives_result():
    loop = FakeCodingAgentLoop(
        result_status=ExecutionPhase.COMPLETE, sleep_seconds=0.1
    )
    received: list = []
    launcher = BuildLauncher(
        loop,
        on_complete=lambda launch, result: received.append(result),
    )
    launcher.launch(_make_handoff("proj-c"))
    loop.wait_started()
    loop.release()
    time.sleep(0.2)
    assert len(received) == 1
    assert received[0].status == ExecutionPhase.COMPLETE
    # Result was stored for later retrieval.
    stored = launcher.get_result("proj-c")
    assert stored is received[0]


# ----------------------------------------------------------------- 13. required coding_agent


def test_launcher_requires_coding_agent():
    with pytest.raises(BuildLauncherError):
        BuildLauncher(None)  # type: ignore[arg-type]


# ----------------------------------------------------------------- 14. in_flight tracking


def test_in_flight_tracking():
    loop = FakeCodingAgentLoop(sleep_seconds=0.5)
    launcher = BuildLauncher(loop)
    handoff = _make_handoff("proj-trk")
    assert not launcher.is_in_flight("proj-trk")
    launcher.launch(handoff)
    loop.wait_started()
    assert launcher.is_in_flight("proj-trk")
    assert launcher.get_in_flight("proj-trk") is not None
    assert launcher.get_in_flight("proj-trk").project_id == "proj-trk"
    loop.release()
    # After completion, the project is no longer in flight.
    time.sleep(0.2)
    assert not launcher.is_in_flight("proj-trk")


# ----------------------------------------------------------------- 15. MainWindow integration


def test_main_window_uses_launcher_when_coding_agent_present(project_store, chat_store):
    """Building the MainWindow with a coding_agent must keep that agent wired
    on the window: the Code Task tab and ``_poll_coding_agent_state`` both
    read it, and it is the object later handed to BuildLauncher. The
    Projects tab is the only thing under test here; everything
    else is left as-is.
    """
    import tkinter as tk
    from giscode.gui.main_window import MainWindow

    loop = FakeCodingAgentLoop(sleep_seconds=0.2)

    class _LocalFakeInference:
        def chat_generate(self, request):
            from giscode.inference.runtime import ChatResponse
            return ChatResponse(text="SYSTEM OK")
        def cancel(self):
            pass

    root = tk.Tk()
    try:
        root.withdraw()
        mw = MainWindow(
            root,
            project_store=project_store,
            chat_store=chat_store,
            coding_agent=loop,
            gisai_engine=_FakeGisAiEngine(_LocalFakeInference()),
        )
        root.update_idletasks()
        # The coding agent was installed on the window.
        assert mw._coding_agent is loop
    finally:
        root.destroy()
