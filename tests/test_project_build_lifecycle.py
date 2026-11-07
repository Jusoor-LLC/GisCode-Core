"""A finished build's real outcome drives the project lifecycle.

The coding loop returning is not proof the project builds. These tests pin the
repaired contract: project verification decides COMPLETE, an honest failure end
FAILED, an explicit retry reopens REVIEW through the store, and COMPLETE and
CANCELLED stay terminal.
"""
from __future__ import annotations

import threading
import time

import pytest

from giscode.agentic.types import ExecutionPhase, TaskResult
from giscode.gui.project_workspace import BuildStatus, ProjectWorkspaceController
from giscode.planer import plan_to_project_dict
from giscode.planer.engine import PlanerEngine
from giscode.projects import ProjectLifecycle, ProjectStore
from giscode.projects.approval import clear_in_flight_registry
from giscode.projects.workspace import ProjectWorkspaceError
from giscode.storage import ChatStore


@pytest.fixture(autouse=True)
def _reset_in_flight():
    clear_in_flight_registry()
    yield
    clear_in_flight_registry()


@pytest.fixture
def stores(tmp_path):
    project_store = ProjectStore(str(tmp_path / "projects.db"))
    chat_store = ChatStore(str(tmp_path / "chats.db"))
    yield project_store, chat_store
    project_store.close()
    chat_store.close()


def _ready_plan(project_id: str) -> dict:
    engine = PlanerEngine(project_id=project_id)
    engine.start_with_idea("Build a small calculator with care")
    for _ in range(15):
        open_questions = [q for q in engine.plan.open_questions if not q.answered]
        if not open_questions:
            break
        for question in open_questions:
            engine.answer_question(question.id, "a-" + question.id)
    engine.set_application_type("web app")
    for name in "abc":
        engine.add_feature(name)
    for name in "xy":
        engine.add_component(name)
    for name in "s1s2s3":
        engine.add_screen(name)
    for name in "m1m2m3":
        engine.add_roadmap_item(name)
    engine.set_branding("br")
    assert engine.is_ready_for_review()
    return plan_to_project_dict(engine.build_plan())


class _Agent:
    """Stand-in for the application's existing coding loop."""

    def __init__(self, *, result=None, block=False, changed=None):
        self.requests = []
        self.cancels = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self._result = result or TaskResult(
            task_id="build", status=ExecutionPhase.COMPLETE
        )
        self._changed = changed
        if not block:
            self.release.set()

    def start(self, request):
        self.requests.append(request)
        self.started.set()
        self.release.wait(10)
        return self._result

    def cancel(self):
        self.cancels += 1
        self._result.status = ExecutionPhase.CANCELLED
        self.release.set()
        return self._result

    def changed_files_for(self, task_id):
        if self._changed is None:
            raise RuntimeError("this agent cannot say what it changed")
        return list(self._changed)


def _open(controller, store, tmp_path, name, tests=None):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    if tests is not None:
        (root / "test_project.py").write_bytes(tests)
    session = controller.create_project(name=name, root_path=str(root))
    project_id = session.project_id
    store.set_plan(project_id, _ready_plan(project_id))
    controller.close()
    controller.open_project(project_id)
    return project_id


def _wait(predicate, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _state(controller, project_id):
    return controller.build_state(project_id)


def test_a_verified_build_reaches_complete(stores, tmp_path):
    project_store, chat_store = stores
    agent = _Agent(changed=["calc.py"])
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    try:
        project_id = _open(
            controller,
            project_store,
            tmp_path,
            "Verified",
            tests=b"def test_ok():\n    assert True\n",
        )

        result = controller.approve_and_start_build()

        assert result.launched is True
        assert _wait(
            lambda: _state(controller, project_id) is not None
            and _state(controller, project_id).status is BuildStatus.COMPLETED
        ), "verification never finished"
        assert project_store.get_project(project_id).lifecycle == (
            ProjectLifecycle.COMPLETE
        )
        report = controller.build_verification(project_id)
        assert report is not None and report.verified is True
        assert "passed" in _state(controller, project_id).message.lower()
    finally:
        agent.release.set()
        controller.close()


def test_failing_project_tests_end_failed_and_keep_the_plan_for_a_retry(
    stores, tmp_path
):
    project_store, chat_store = stores
    agent = _Agent(changed=["calc.py"])
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    try:
        project_id = _open(
            controller,
            project_store,
            tmp_path,
            "FailingTests",
            tests=b"def test_bad():\n    assert False\n",
        )
        plan_before = project_store.get_project(project_id).plan
        files_before = (tmp_path / "FailingTests" / "test_project.py").read_bytes()

        controller.approve_and_start_build()

        assert _wait(
            lambda: _state(controller, project_id) is not None
            and _state(controller, project_id).status is BuildStatus.FAILED
        ), "the failed verification was never recorded"
        assert project_store.get_project(project_id).lifecycle == (
            ProjectLifecycle.FAILED
        )
        report = controller.build_verification(project_id)
        assert report.outcome.value == "tests_failed"

        retry = controller.retry_build()

        assert retry.launched is True
        assert project_store.get_project(project_id).plan == plan_before
        assert (
            tmp_path / "FailingTests" / "test_project.py"
        ).read_bytes() == files_before
        assert _wait(
            lambda: project_store.get_project(project_id).lifecycle
            in {ProjectLifecycle.FAILED, ProjectLifecycle.COMPLETE}
        )
        # The suite still fails, so the retry honestly ends FAILED again.
        assert project_store.get_project(project_id).lifecycle == (
            ProjectLifecycle.FAILED
        )
    finally:
        agent.release.set()
        controller.close()


def test_an_approved_build_that_never_started_can_be_retried(stores, tmp_path):
    project_store, chat_store = stores
    agent = _Agent()
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    try:
        project_id = _open(controller, project_store, tmp_path, "NeverStarted")
        project_store.transition_lifecycle(project_id, ProjectLifecycle.REVIEW)
        project_store.transition_lifecycle(project_id, ProjectLifecycle.APPROVED)

        retry = controller.retry_build()

        assert retry.launched is True
        assert agent.started.wait(5)
        assert _wait(
            lambda: _state(controller, project_id) is not None
            and _state(controller, project_id).status is BuildStatus.FAILED
        )
    finally:
        agent.release.set()
        controller.close()


def test_complete_and_cancelled_projects_cannot_be_retried(stores, tmp_path):
    project_store, chat_store = stores
    controller = ProjectWorkspaceController(project_store, chat_store)
    try:
        complete_id = _open(controller, project_store, tmp_path, "Done")
        for target in (
            ProjectLifecycle.REVIEW,
            ProjectLifecycle.APPROVED,
            ProjectLifecycle.BUILDING,
            ProjectLifecycle.TESTING,
            ProjectLifecycle.VERIFICATION,
            ProjectLifecycle.COMPLETE,
        ):
            project_store.transition_lifecycle(complete_id, target)

        with pytest.raises(ProjectWorkspaceError) as exc:
            controller.retry_build()
        assert "COMPLETE" in str(exc.value)
        assert project_store.get_project(complete_id).lifecycle == (
            ProjectLifecycle.COMPLETE
        )

        cancelled_id = _open(controller, project_store, tmp_path, "Stopped")
        project_store.transition_lifecycle(cancelled_id, ProjectLifecycle.CANCELLED)
        controller.open_project(cancelled_id)

        with pytest.raises(ProjectWorkspaceError) as exc:
            controller.retry_build()
        assert "CANCELLED" in str(exc.value)
        assert project_store.get_project(cancelled_id).lifecycle == (
            ProjectLifecycle.CANCELLED
        )
    finally:
        controller.close()


def test_a_second_launch_and_a_retry_are_refused_while_one_build_runs(
    stores, tmp_path
):
    project_store, chat_store = stores
    agent = _Agent(block=True)
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    try:
        project_id = _open(controller, project_store, tmp_path, "OneAtATime")
        controller.approve_and_start_build()
        assert agent.started.wait(5)
        assert controller.build_in_flight() is True
        started_before = len(agent.requests)

        with pytest.raises(ProjectWorkspaceError) as second:
            controller.approve_and_start_build()
        assert "already running" in str(second.value)

        with pytest.raises(ProjectWorkspaceError) as retry:
            controller.retry_build()
        assert "already running" in str(retry.value)

        assert len(agent.requests) == started_before
    finally:
        agent.release.set()
        _wait(lambda: controller.build_in_flight() is False)
        controller.close()


def test_a_second_retry_after_a_successful_restart_is_refused(stores, tmp_path):
    """There is no automatic retry loop: the second retry has nothing to reopen."""
    project_store, chat_store = stores
    agent = _Agent(block=True)
    controller = ProjectWorkspaceController(
        project_store, chat_store, coding_agent=agent
    )
    try:
        project_id = _open(controller, project_store, tmp_path, "RetryTwice")
        project_store.transition_lifecycle(project_id, ProjectLifecycle.REVIEW)
        project_store.transition_lifecycle(project_id, ProjectLifecycle.APPROVED)
        controller.retry_build()
        assert agent.started.wait(5)

        with pytest.raises(ProjectWorkspaceError):
            controller.retry_build()
    finally:
        agent.release.set()
        _wait(lambda: controller.build_in_flight() is False)
        controller.close()


def test_completion_verifies_before_the_project_leaves_building(
    stores, tmp_path, monkeypatch
):
    """Anything asking "is this build still running?" must still get yes.

    Verification happens after the launcher dropped its in-flight entry, so the
    window is checked from inside the verification call itself.
    """
    from types import SimpleNamespace

    import giscode.projects.build_verification as mod
    from giscode.projects.build_verification import (
        BuildVerificationOutcome,
        BuildVerificationReport,
    )

    project_store, chat_store = stores
    controller = ProjectWorkspaceController(project_store, chat_store)
    try:
        project_id = _open(controller, project_store, tmp_path, "Finishing")
        for target in (
            ProjectLifecycle.REVIEW,
            ProjectLifecycle.APPROVED,
            ProjectLifecycle.BUILDING,
        ):
            project_store.transition_lifecycle(project_id, target)
        seen = {}

        def stub(project_root, **kwargs):
            seen["in_flight"] = controller.build_in_flight(project_id)
            seen["lifecycle"] = project_store.get_project(project_id).lifecycle
            return BuildVerificationReport(
                outcome=BuildVerificationOutcome.VERIFIED,
                message="stub verification passed",
            )

        monkeypatch.setattr(mod, "verify_completed_build", stub)

        controller._on_build_complete(
            SimpleNamespace(project_id=project_id, task_id="task-1"),
            TaskResult(task_id="task-1", status=ExecutionPhase.COMPLETE),
        )

        assert seen["in_flight"] is True
        assert seen["lifecycle"] is ProjectLifecycle.VERIFICATION
        state = controller.build_state(project_id)
        assert state is not None and state.status is BuildStatus.COMPLETED
        assert controller.build_in_flight(project_id) is False
        assert project_store.get_project(project_id).lifecycle == (
            ProjectLifecycle.COMPLETE
        )
    finally:
        controller.close()


def test_stop_during_verification_reports_cancelled(
    stores, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import giscode.projects.build_verification as mod
    from giscode.projects.build_verification import (
        BuildVerificationOutcome,
        BuildVerificationReport,
    )

    project_store, chat_store = stores
    controller = ProjectWorkspaceController(project_store, chat_store)
    try:
        project_id = _open(controller, project_store, tmp_path, "StopVerifying")
        for target in (
            ProjectLifecycle.REVIEW,
            ProjectLifecycle.APPROVED,
            ProjectLifecycle.BUILDING,
        ):
            project_store.transition_lifecycle(project_id, target)
        with controller._build_lock:
            controller._build_cancel_events[project_id] = threading.Event()
        controller._build_cancel_events[project_id].set()

        def stub(project_root, *, should_cancel=None, **kwargs):
            assert should_cancel is not None and should_cancel() is True
            return BuildVerificationReport(
                outcome=BuildVerificationOutcome.CANCELLED,
                message="Project verification was cancelled.",
            )

        monkeypatch.setattr(mod, "verify_completed_build", stub)

        controller._on_build_complete(
            SimpleNamespace(project_id=project_id, task_id="task-2"),
            TaskResult(task_id="task-2", status=ExecutionPhase.COMPLETE),
        )

        state = controller.build_state(project_id)
        assert state is not None and state.status is BuildStatus.CANCELLED
        assert project_store.get_project(project_id).lifecycle == (
            ProjectLifecycle.CANCELLED
        )
    finally:
        controller.close()
