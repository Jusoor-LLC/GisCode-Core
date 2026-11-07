import os
import sys
import threading
import time
from pathlib import Path

import pytest

from giscode.agentic.loop import CodingAgentLoop
from giscode.agentic.types import ActionType, ExecutionStep, TaskRequest
from giscode.inference.api import InferenceAPI
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.openhands.adapter import MockOpenHandsAdapter, OpenHandsAdapter, RealOpenHandsAdapter
from giscode.openhands.config import OpenHandsConfig
from giscode.openhands.errors import (
    OpenHandsCancelledError,
    OpenHandsTimeoutError,
    OpenHandsUnavailableError,
    OpenHandsWorkspaceError,
)
from giscode.openhands.types import (
    OpenHandsPhase,
    OpenHandsRequest,
    OpenHandsResult,
    OpenHandsStatus,
)
from giscode.sandbox.executor import SandboxExecutor
from giscode.sandbox.types import SandboxBackend, SandboxConfig
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace


FAKE_RUNTIME_SCRIPT = '''import sys
import time
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--behavior", default="success")
parser.add_argument("--delay", type=float, default=0.0)
parser.add_argument("command", nargs="?")
parser.add_argument("arguments", nargs="*")
args = parser.parse_args()

if args.delay > 0:
    time.sleep(args.delay)

if args.behavior == "failure":
    print("Mock execution failed", file=sys.stderr)
    sys.exit(1)

print(f"Mock execution of {args.command} completed successfully")
sys.exit(0)
'''


def _write_fake_runtime(tmp_path: Path) -> str:
    script = tmp_path / "fake_openhands_runtime.py"
    script.write_text(FAKE_RUNTIME_SCRIPT, encoding="utf-8")
    return str(script)


def test_openhands_adapter_initialization(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = OpenHandsConfig(timeout=30.0)
    task_manager = TaskManager()
    adapter = OpenHandsAdapter
    assert adapter is not None


def test_mock_openhands_success(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    adapter = MockOpenHandsAdapter(workspace, behavior="success")
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.COMPLETED
    assert result.exit_code == 0
    assert "successfully" in result.stdout


def test_mock_openhands_failure(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    adapter = MockOpenHandsAdapter(workspace, behavior="failure")
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.FAILED
    assert result.exit_code == 1
    assert result.error is not None


def test_mock_openhands_cancellation(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    task_manager.start_task("task-1")
    task_manager.cancel()

    adapter = MockOpenHandsAdapter(workspace, task_manager=task_manager, behavior="success")
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.CANCELLED
    assert result.cancelled is True


def test_mock_openhands_timeout(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    task = task_manager.start_task("task-1", max_duration=0.05)
    time.sleep(0.1)

    adapter = MockOpenHandsAdapter(
        workspace, task_manager=task_manager, behavior="success", delay=0.01
    )
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.CANCELLED
    assert result.cancelled is True


def test_mock_openhands_unavailable(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    adapter = MockOpenHandsAdapter(workspace, behavior="unavailable")
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.UNAVAILABLE
    assert result.error is not None


def test_openhands_path_validation(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    adapter = MockOpenHandsAdapter(workspace)
    valid_path = str(workspace.root_path / "src" / "main.py")
    resolved = adapter.validate_path(valid_path)
    assert resolved.is_absolute()


def test_openhands_unauthorized_workspace_rejection(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    adapter = MockOpenHandsAdapter(workspace)
    with pytest.raises(OpenHandsWorkspaceError):
        adapter.validate_path("/etc/passwd")
    with pytest.raises(OpenHandsWorkspaceError):
        adapter.validate_path("../etc/passwd")


def test_openhands_integration_with_task_control(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    task = task_manager.start_task("task-1")
    adapter = MockOpenHandsAdapter(workspace, task_manager=task_manager, behavior="success")
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.COMPLETED
    task_manager.complete(success=True)


def test_openhands_integration_with_orchestration(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    inference = InferenceAPI()
    adapter = MockOpenHandsAdapter(workspace, task_manager=task_manager, behavior="success")
    loop = CodingAgentLoop(workspace, inference, task_manager, openhands_adapter=adapter)

    step = ExecutionStep(
        step_id="step-1",
        description="Create file",
        action_type=ActionType.CREATE,
        target_path="src/main.py",
        content="print('hello')",
    )
    request = TaskRequest(task_id="task-1", description="Test task")
    loop.submit(request)
    loop._task_manager.start_task("task-1")
    result = loop._execute_step(step)
    assert result.success is True
    assert result.error is None


def test_openhands_no_host_fallback(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    adapter = MockOpenHandsAdapter(workspace, behavior="success")
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.COMPLETED
    assert result.error is None


def test_real_openhands_discovery_no_runtime(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    adapter = RealOpenHandsAdapter(workspace, runtime_command=None)
    assert adapter.runtime_available is False
    assert adapter.get_runtime_state() == "not_installed"


def test_real_openhands_unavailable_result(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    adapter = RealOpenHandsAdapter(workspace, runtime_command=None)
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.UNAVAILABLE
    assert "not installed" in result.error


def test_real_openhands_success_with_fake_runtime(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    runtime = _write_fake_runtime(tmp_path)
    config = OpenHandsConfig(runtime_args=[runtime])
    adapter = RealOpenHandsAdapter(workspace, runtime_command=sys.executable, config=config)
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.COMPLETED
    assert result.exit_code == 0
    assert "Mock execution of echo" in result.stdout


def test_real_openhands_failure_with_fake_runtime(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    runtime = _write_fake_runtime(tmp_path)
    config = OpenHandsConfig(runtime_args=[runtime, "--behavior", "failure"])
    adapter = RealOpenHandsAdapter(workspace, runtime_command=sys.executable, config=config)
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.FAILED
    assert result.exit_code == 1
    assert result.error is not None


def test_real_openhands_cancellation(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    task = task_manager.start_task("task-1")
    runtime = _write_fake_runtime(tmp_path)
    config = OpenHandsConfig(runtime_args=[runtime, "--delay", "0.5"])
    adapter = RealOpenHandsAdapter(workspace, runtime_command=sys.executable, config=config, task_manager=task_manager)
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])

    def cancel_after_delay():
        time.sleep(0.1)
        task_manager.cancel()

    cancel_thread = threading.Thread(target=cancel_after_delay)
    cancel_thread.start()
    result = adapter.execute(request)
    cancel_thread.join()

    assert result.status == OpenHandsStatus.CANCELLED
    assert result.cancelled is True


def test_real_openhands_timeout(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    task = task_manager.start_task("task-1", max_duration=0.2)
    runtime = _write_fake_runtime(tmp_path)
    config = OpenHandsConfig(runtime_args=[runtime, "--delay", "0.5"])
    adapter = RealOpenHandsAdapter(workspace, runtime_command=sys.executable, config=config, task_manager=task_manager)
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.TIMED_OUT
    assert result.timed_out is True


def test_real_openhands_cleanup_after_cancellation(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    task = task_manager.start_task("task-1")
    runtime = _write_fake_runtime(tmp_path)
    config = OpenHandsConfig(runtime_args=[runtime, "--delay", "0.5"])
    adapter = RealOpenHandsAdapter(workspace, runtime_command=sys.executable, config=config, task_manager=task_manager)
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])

    def cancel_after_delay():
        time.sleep(0.1)
        task_manager.cancel()

    cancel_thread = threading.Thread(target=cancel_after_delay)
    cancel_thread.start()
    result = adapter.execute(request)
    cancel_thread.join()

    assert result.status == OpenHandsStatus.CANCELLED
    with adapter._process_lock:
        assert adapter._process is None


def test_real_openhands_cleanup_after_timeout(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    task = task_manager.start_task("task-1", max_duration=0.2)
    runtime = _write_fake_runtime(tmp_path)
    config = OpenHandsConfig(runtime_args=[runtime, "--delay", "0.5"])
    adapter = RealOpenHandsAdapter(workspace, runtime_command=sys.executable, config=config, task_manager=task_manager)
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.TIMED_OUT
    with adapter._process_lock:
        assert adapter._process is None


def test_real_openhands_workspace_validation(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    runtime = _write_fake_runtime(tmp_path)
    config = OpenHandsConfig(runtime_args=[runtime])
    adapter = RealOpenHandsAdapter(workspace, runtime_command=sys.executable, config=config)
    valid_path = str(workspace.root_path / "src" / "main.py")
    resolved = adapter.validate_path(valid_path)
    assert resolved.is_absolute()


def test_real_openhands_unauthorized_path_rejection(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    runtime = _write_fake_runtime(tmp_path)
    config = OpenHandsConfig(runtime_args=[runtime])
    adapter = RealOpenHandsAdapter(workspace, runtime_command=sys.executable, config=config)
    with pytest.raises(OpenHandsWorkspaceError):
        adapter.validate_path("/etc/passwd")
    with pytest.raises(OpenHandsWorkspaceError):
        adapter.validate_path("../etc/passwd")


def test_real_openhands_task_manager_integration(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    task = task_manager.start_task("task-1")
    runtime = _write_fake_runtime(tmp_path)
    config = OpenHandsConfig(runtime_args=[runtime])
    adapter = RealOpenHandsAdapter(workspace, runtime_command=sys.executable, config=config, task_manager=task_manager)
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.COMPLETED
    task_manager.complete(success=True)


def test_real_openhands_orchestration_integration(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    inference = InferenceAPI()
    runtime = _write_fake_runtime(tmp_path)
    config = OpenHandsConfig(runtime_args=[runtime])
    adapter = RealOpenHandsAdapter(workspace, runtime_command=sys.executable, config=config, task_manager=task_manager)
    loop = CodingAgentLoop(workspace, inference, task_manager, openhands_adapter=adapter)

    step = ExecutionStep(
        step_id="step-1",
        description="Run command",
        action_type=ActionType.CREATE,
        target_path="src/main.py",
        content="print('hello')",
    )
    request = TaskRequest(task_id="task-1", description="Test task")
    loop.submit(request)
    loop._task_manager.start_task("task-1")
    result = loop._execute_step(step)
    assert result.success is True
    assert result.error is None


def test_real_openhands_sandbox_boundary_preserved(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    sandbox = SandboxExecutor(workspace, config=SandboxConfig(backend=SandboxBackend.UNAVAILABLE))
    runtime = _write_fake_runtime(tmp_path)
    config = OpenHandsConfig(runtime_args=[runtime])
    adapter = RealOpenHandsAdapter(workspace, runtime_command=sys.executable, config=config)
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.COMPLETED
    assert sandbox.available is False


def test_real_openhands_no_host_fallback(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    adapter = RealOpenHandsAdapter(workspace, runtime_command=None)
    request = OpenHandsRequest(task_id="task-1", command="echo", arguments=["hello"])
    result = adapter.execute(request)
    assert result.status == OpenHandsStatus.UNAVAILABLE
    assert result.error is not None


def test_openhands_runtime_discovery():
    from giscode.openhands.adapter import RealOpenHandsAdapter
    from giscode.workspace.workspace import Workspace
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Workspace(tmp)
        adapter = RealOpenHandsAdapter(workspace, runtime_command=None)
        assert adapter.runtime_available is False


def test_map_action_type_to_phase():
    assert CodingAgentLoop._map_action_type_to_phase(ActionType.CREATE) == OpenHandsPhase.CODING
    assert CodingAgentLoop._map_action_type_to_phase(ActionType.MODIFY) == OpenHandsPhase.CODING
    assert CodingAgentLoop._map_action_type_to_phase(ActionType.VERIFY) == OpenHandsPhase.VERIFYING
    assert CodingAgentLoop._map_action_type_to_phase(ActionType.REPAIR) == OpenHandsPhase.CODING
    assert CodingAgentLoop._map_action_type_to_phase(ActionType.READ) == OpenHandsPhase.CODING


def test_execute_step_sets_openhands_phase(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    adapter = MockOpenHandsAdapter(workspace, behavior="success")
    loop = CodingAgentLoop(workspace, inference, task_manager, openhands_adapter=adapter)

    step = ExecutionStep(
        step_id="step-1",
        description="Create file",
        action_type=ActionType.CREATE,
        target_path="src/main.py",
        content="print('hello')",
    )
    result = loop._execute_step(step)
    assert result.success is True
    assert adapter._call_count == 1
    assert adapter._current_task_id == "step-1"