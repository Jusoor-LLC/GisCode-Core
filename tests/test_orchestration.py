import sys
import threading
import time
from typing import Optional

import pytest

from giscode.orchestration.checkpoint import CheckpointError, CheckpointManager
from giscode.orchestration.failure import FailureCapture
from giscode.orchestration.orchestrator import BuildTestOrchestrator
from giscode.orchestration.types import (
    BuildRequest,
    BuildResult,
    BuildStatus,
    FailureRecord,
    FixAttempt,
    OrchestrationPhase,
    OrchestrationResult,
    TestRequest,
    TestResult,
    TestStatus,
)
from giscode.sandbox.executor import SandboxExecutor
from giscode.sandbox.types import SandboxBackend, SandboxConfig, SandboxStatus
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace


def test_successful_build(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = SandboxConfig(backend=SandboxBackend.UNAVAILABLE)
    sandbox = SandboxExecutor(workspace, config=config)
    orchestrator = BuildTestOrchestrator(workspace, sandbox)
    request = BuildRequest(command=sys.executable, arguments=["-c", "print('ok')"])
    result = orchestrator.build(request)
    assert result.status == BuildStatus.SANDBOX_UNAVAILABLE


def test_successful_test(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = SandboxConfig(backend=SandboxBackend.UNAVAILABLE)
    sandbox = SandboxExecutor(workspace, config=config)
    orchestrator = BuildTestOrchestrator(workspace, sandbox)
    request = TestRequest(command=sys.executable, arguments=["-c", "print('ok')"])
    result = orchestrator.test(request)
    assert result.status == TestStatus.SANDBOX_UNAVAILABLE


def test_build_and_test_sandbox_unavailable(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = SandboxConfig(backend=SandboxBackend.UNAVAILABLE)
    sandbox = SandboxExecutor(workspace, config=config)
    orchestrator = BuildTestOrchestrator(workspace, sandbox)
    build_request = BuildRequest(command="echo", arguments=["build"])
    test_request = TestRequest(command="echo", arguments=["test"])
    result = orchestrator.build_and_test("task-1", build_request, test_request)
    assert result.status == OrchestrationPhase.FAILED
    assert result.error == "Sandbox unavailable"
    assert result.failure_info["reason"] == "sandbox_unavailable"


def test_build_failure_capture(tmp_path):
    capture = FailureCapture()
    record = capture.capture(
        failure_type="build_failure",
        command="python -m build",
        exit_code=1,
        stderr="ModuleNotFoundError: No module named 'x'",
        summary="Build failed due to missing module",
    )
    assert record.failure_type == "build_failure"
    assert record.exit_code == 1
    assert "ModuleNotFoundError" in record.stderr
    assert record.fingerprint != ""


def test_failure_deduplication(tmp_path):
    capture = FailureCapture()
    record1 = capture.capture(
        failure_type="build_failure",
        command="python -m build",
        exit_code=1,
        stderr="ModuleNotFoundError: No module named 'x'",
    )
    record2 = capture.capture(
        failure_type="build_failure",
        command="python -m build",
        exit_code=1,
        stderr="ModuleNotFoundError: No module named 'x'",
    )
    assert capture.is_duplicate(record2) is True


def test_fix_proposal_creation(tmp_path):
    orchestrator = BuildTestOrchestrator(Workspace(tmp_path / "ws"), SandboxExecutor(Workspace(tmp_path / "ws")))
    failure = FailureRecord(
        failure_type="build_failure",
        command="python -m build",
        exit_code=1,
        summary="Missing module",
    )
    fix = FixAttempt(attempt_number=1, failure=failure, fix_proposal="Install missing module")
    assert fix.attempt_number == 1
    assert fix.fix_proposal == "Install missing module"
    assert fix.applied is False


def test_checkpoint_before_fix(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "main.py", content="print('hello')")
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("before-fix")
    assert checkpoint.exists()
    manager.cleanup(checkpoint)
    assert not checkpoint.exists()


def test_rollback(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "main.py", content="print('hello')")
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("before-fix")
    workspace.write("src", "main.py", content="print('modified')")
    produced = manager.capture_produced()
    outcome = manager.restore(checkpoint, produced=produced)
    assert outcome.status == "rolled_back"
    assert workspace.read("src", "main.py") == "print('hello')"


def test_rollback_without_evidence_fails_closed(tmp_path):
    """No produced fingerprint means no undo, not a whole-tree replacement."""
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "main.py", content="print('hello')")
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("before-fix")
    workspace.write("src", "main.py", content="print('modified')")

    outcome = manager.restore(checkpoint)

    assert outcome.status == "blocked"
    assert outcome.complete is False
    assert "no record of the versions" in outcome.blocked
    assert workspace.read("src", "main.py") == "print('modified')"


def test_retry_limit(tmp_path):
    workspace = Workspace(tmp_path / "ws")

    class FailingSandbox:
        def __init__(self, *args, **kwargs):
            self.call_count = 0

        def execute(self, request):
            self.call_count += 1
            from giscode.sandbox.types import SandboxResult, SandboxBackend, SandboxStatus
            return SandboxResult(
                sandbox_id="test",
                backend=SandboxBackend.UNAVAILABLE,
                command=request.command,
                exit_code=1,
                status=SandboxStatus.FAILED,
            )

        def create_sandbox(self, *args, **kwargs):
            from giscode.sandbox.types import SandboxResult, SandboxBackend, SandboxStatus
            return SandboxResult(sandbox_id="test", backend=SandboxBackend.UNAVAILABLE, command="", status=SandboxStatus.UNAVAILABLE)

        def get_status(self):
            from giscode.sandbox.types import SandboxStatus
            return SandboxStatus.IDLE

        @property
        def backend(self):
            from giscode.sandbox.types import SandboxBackend
            return SandboxBackend.UNAVAILABLE

        @property
        def available(self):
            return False

    failing_sandbox = FailingSandbox()
    orchestrator = BuildTestOrchestrator(workspace, failing_sandbox, max_attempts=2)
    build_request = BuildRequest(command="echo", arguments=["build"])
    test_request = TestRequest(command="echo", arguments=["test"])
    result = orchestrator.build_and_test("task-1", build_request, test_request)
    assert result.status == OrchestrationPhase.ESCALATED
    assert result.failure_info["reason"] == "retry_limit_exhausted"
    assert result.failure_info["max_attempts"] == 2


def test_cancellation(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = SandboxConfig(backend=SandboxBackend.UNAVAILABLE)
    sandbox = SandboxExecutor(workspace, config=config)
    orchestrator = BuildTestOrchestrator(workspace, sandbox)
    orchestrator.cancel()
    assert orchestrator.get_status() == OrchestrationPhase.CANCELLED


def test_timeout_integration(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()

    workspace.write("src", "main.py", content="print('broken')")
    original_content = workspace.read("src", "main.py")

    class SlowSandbox:
        def __init__(self):
            self.call_count = 0

        def execute(self, request):
            from giscode.sandbox.types import SandboxResult, SandboxBackend, SandboxStatus

            self.call_count += 1
            if self.call_count == 1:
                return SandboxResult(
                    sandbox_id="test",
                    backend=SandboxBackend.DOCKER,
                    command=request.command,
                    exit_code=1,
                    status=SandboxStatus.FAILED,
                    stderr="Build failed",
                )
            time.sleep(0.3)
            workspace.write("src", "main.py", content="print('fixed')")
            return SandboxResult(
                sandbox_id="test",
                backend=SandboxBackend.DOCKER,
                command=request.command,
                exit_code=0,
                status=SandboxStatus.COMPLETED,
                stdout="Build passed",
            )

        def create_sandbox(self, *args, **kwargs):
            from giscode.sandbox.types import SandboxResult, SandboxBackend, SandboxStatus

            return SandboxResult(
                sandbox_id="test",
                backend=SandboxBackend.DOCKER,
                command="",
                status=SandboxStatus.RUNNING,
            )

        def get_status(self):
            from giscode.sandbox.types import SandboxStatus

            return SandboxStatus.IDLE

        @property
        def backend(self):
            from giscode.sandbox.types import SandboxBackend

            return SandboxBackend.DOCKER

        @property
        def available(self):
            return True

    sandbox = SlowSandbox()
    orchestrator = BuildTestOrchestrator(
        workspace, sandbox, task_manager=task_manager, max_attempts=5
    )
    task = task_manager.start_task("timeout-test", max_duration=0.2)
    build_request = BuildRequest(command="echo", arguments=["build"])
    test_request = TestRequest(command="echo", arguments=["test"])
    result = orchestrator.build_and_test("task-1", build_request, test_request)

    assert result.status == OrchestrationPhase.TIMED_OUT
    assert result.failure_info["reason"] == "timeout"
    assert workspace.read("src", "main.py") == original_content


def test_sandbox_unavailable_no_host_fallback(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = SandboxConfig(backend=SandboxBackend.UNAVAILABLE)
    sandbox = SandboxExecutor(workspace, config=config)
    orchestrator = BuildTestOrchestrator(workspace, sandbox)
    build_request = BuildRequest(command="echo", arguments=["build"])
    test_request = TestRequest(command="echo", arguments=["test"])
    result = orchestrator.build_and_test("task-1", build_request, test_request)
    assert result.status == OrchestrationPhase.FAILED
    assert result.build_result is not None
    assert result.build_result.status == BuildStatus.SANDBOX_UNAVAILABLE
    assert result.test_result is None


def test_structured_escalation_result(tmp_path):
    workspace = Workspace(tmp_path / "ws")

    class FailingSandbox:
        def __init__(self, *args, **kwargs):
            self.call_count = 0

        def execute(self, request):
            self.call_count += 1
            from giscode.sandbox.types import SandboxResult, SandboxBackend, SandboxStatus
            return SandboxResult(
                sandbox_id="test",
                backend=SandboxBackend.UNAVAILABLE,
                command=request.command,
                exit_code=1,
                status=SandboxStatus.FAILED,
            )

        def create_sandbox(self, *args, **kwargs):
            from giscode.sandbox.types import SandboxResult, SandboxBackend, SandboxStatus
            return SandboxResult(sandbox_id="test", backend=SandboxBackend.UNAVAILABLE, command="", status=SandboxStatus.UNAVAILABLE)

        def get_status(self):
            from giscode.sandbox.types import SandboxStatus
            return SandboxStatus.IDLE

        @property
        def backend(self):
            from giscode.sandbox.types import SandboxBackend
            return SandboxBackend.UNAVAILABLE

        @property
        def available(self):
            return False

    failing_sandbox = FailingSandbox()
    orchestrator = BuildTestOrchestrator(workspace, failing_sandbox, max_attempts=1)
    build_request = BuildRequest(command="echo", arguments=["build"])
    test_request = TestRequest(command="echo", arguments=["test"])
    result = orchestrator.build_and_test("task-1", build_request, test_request)
    assert result.status == OrchestrationPhase.ESCALATED
    assert result.failure_info is not None
    assert result.failure_info["reason"] == "retry_limit_exhausted"


def test_orchestration_result_fields():
    result = OrchestrationResult(
        task_id="test-1",
        status=OrchestrationPhase.IDLE,
        max_attempts=5,
    )
    assert result.task_id == "test-1"
    assert result.max_attempts == 5
    assert len(result.attempts) == 0


def test_build_result_fields():
    result = BuildResult(
        command="echo",
        exit_code=0,
        stdout="hello",
        status=BuildStatus.SUCCESS,
    )
    assert result.stdout == "hello"
    assert result.status == BuildStatus.SUCCESS


def test_test_result_fields():
    result = TestResult(
        command="pytest",
        exit_code=0,
        passed=10,
        failed=0,
        status=TestStatus.PASSED,
    )
    assert result.passed == 10
    assert result.failed == 0
    assert result.status == TestStatus.PASSED


def test_failure_record_fields():
    capture = FailureCapture()
    record = capture.capture(
        failure_type="test_failure",
        command="pytest",
        exit_code=1,
        stderr="AssertionError",
        summary="Test failed",
    )
    assert record.failure_type == "test_failure"
    assert record.exit_code == 1
    assert record.fingerprint != ""


def test_orchestration_phase_enum():
    assert OrchestrationPhase.IDLE.value == "idle"
    assert OrchestrationPhase.BUILDING.value == "building"
    assert OrchestrationPhase.TESTING.value == "testing"
    assert OrchestrationPhase.FIXING.value == "fixing"
    assert OrchestrationPhase.COMPLETE.value == "complete"
    assert OrchestrationPhase.FAILED.value == "failed"
    assert OrchestrationPhase.CANCELLED.value == "cancelled"
    assert OrchestrationPhase.TIMED_OUT.value == "timed_out"
    assert OrchestrationPhase.ESCALATED.value == "escalated"


def test_build_status_enum():
    assert BuildStatus.PENDING.value == "pending"
    assert BuildStatus.RUNNING.value == "running"
    assert BuildStatus.SUCCESS.value == "success"
    assert BuildStatus.FAILED.value == "failed"
    assert BuildStatus.SANDBOX_UNAVAILABLE.value == "sandbox_unavailable"


def test_test_status_enum():
    assert TestStatus.PENDING.value == "pending"
    assert TestStatus.RUNNING.value == "running"
    assert TestStatus.PASSED.value == "passed"
    assert TestStatus.FAILED.value == "failed"
    assert TestStatus.SANDBOX_UNAVAILABLE.value == "sandbox_unavailable"


def test_previous_tasks_remain_intact():
    from giscode.config.manager import ConfigManager
    from giscode.task_control.manager import TaskManager

    settings = ConfigManager()
    manager = TaskManager()
    task = manager.start_task("preserve-test")
    assert task.status.value == "running"
    manager.complete(success=True)
    assert manager.get_last_task().status.value == "completed"


def test_checkpoint_cleanup(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "main.py", content="print('hello')")
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("test")
    assert checkpoint.exists()
    manager.cleanup(checkpoint)
    assert not checkpoint.exists()


def test_checkpoint_error_on_none():
    manager = CheckpointManager(Workspace())
    with pytest.raises(CheckpointError):
        manager.restore(None)


def test_orchestrator_initial_state(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    sandbox = SandboxExecutor(workspace)
    orchestrator = BuildTestOrchestrator(workspace, sandbox)
    assert orchestrator.get_status() == OrchestrationPhase.IDLE
