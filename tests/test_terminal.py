import os
import sys
import time

import pytest

from giscode.terminal.executor import CommandExecutor, WorkspaceRestrictionError
from giscode.terminal.types import CommandRequest, CommandResult, CommandStatus, ExecutorConfig
from giscode.workspace.workspace import Workspace


def test_successful_command(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    request = CommandRequest(
        command=sys.executable,
        arguments=["-c", "print('hello')"],
    )
    result = executor.execute(request)
    assert result.status == CommandStatus.COMPLETED
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.elapsed_time >= 0


def test_stdout_capture(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    request = CommandRequest(
        command=sys.executable,
        arguments=["-c", "print('stdout-output')"],
    )
    result = executor.execute(request)
    assert "stdout-output" in result.stdout


def test_stderr_capture(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    request = CommandRequest(
        command=sys.executable,
        arguments=["-c", "import sys; sys.stderr.write('stderr-output')"],
    )
    result = executor.execute(request)
    assert "stderr-output" in result.stderr


def test_exit_code_capture(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    request = CommandRequest(
        command=sys.executable,
        arguments=["-c", "import sys; sys.exit(42)"],
    )
    result = executor.execute(request)
    assert result.exit_code == 42
    assert result.status == CommandStatus.FAILED


def test_working_directory(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    subdir = workspace.root_path / "subdir"
    subdir.mkdir()
    request = CommandRequest(
        command=sys.executable,
        arguments=["-c", "import os; print(os.getcwd())"],
        working_directory=str(subdir),
    )
    result = executor.execute(request)
    assert str(subdir) in result.stdout


def test_workspace_path_validation(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    with pytest.raises(WorkspaceRestrictionError):
        executor._resolve_working_directory("../../../etc/passwd")


def test_path_traversal_rejection(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    request = CommandRequest(
        command=sys.executable,
        arguments=["-c", "print('test')"],
        working_directory="../../../etc",
    )
    with pytest.raises(WorkspaceRestrictionError):
        executor.execute(request)


def test_outside_workspace_rejection(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    request = CommandRequest(
        command=sys.executable,
        arguments=["-c", "print('test')"],
        working_directory=str(tmp_path),
    )
    with pytest.raises(WorkspaceRestrictionError):
        executor.execute(request)


def test_timeout(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = ExecutorConfig(default_timeout=0.1)
    executor = CommandExecutor(workspace, config=config)
    request = CommandRequest(
        command=sys.executable,
        arguments=["-c", "import time; time.sleep(10)"],
    )
    result = executor.execute(request)
    assert result.status == CommandStatus.TIMED_OUT
    assert result.timed_out is True
    assert result.elapsed_time >= 0.1


def test_cancellation(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = ExecutorConfig(default_timeout=30.0)
    executor = CommandExecutor(workspace, config=config)
    request = CommandRequest(
        command=sys.executable,
        arguments=["-c", "import time; time.sleep(10)"],
        timeout=30.0,
    )

    def run():
        return executor.execute(request)

    thread = __import__("threading").Thread(target=run)
    thread.start()
    time.sleep(0.2)
    executor.cancel()
    thread.join(timeout=5.0)
    assert not thread.is_alive()


def test_output_limit_truncation(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = ExecutorConfig(max_stdout_bytes=100, max_stderr_bytes=100)
    executor = CommandExecutor(workspace, config=config)
    request = CommandRequest(
        command=sys.executable,
        arguments=["-c", "print('x' * 10000)"],
    )
    result = executor.execute(request)
    assert "[OUTPUT TRUNCATED]" in result.stdout or len(result.stdout) <= 200


def test_invalid_command(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    request = CommandRequest(
        command="nonexistent_command_xyz",
        arguments=[],
    )
    result = executor.execute(request)
    assert result.status == CommandStatus.FAILED
    assert result.error is not None


def test_structured_result(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    request = CommandRequest(
        command=sys.executable,
        arguments=["-c", "print('test')"],
    )
    result = executor.execute(request)
    assert isinstance(result, CommandResult)
    assert hasattr(result, "command")
    assert hasattr(result, "exit_code")
    assert hasattr(result, "stdout")
    assert hasattr(result, "stderr")
    assert hasattr(result, "elapsed_time")
    assert hasattr(result, "status")
    assert hasattr(result, "timed_out")
    assert hasattr(result, "cancelled")


def test_elapsed_time(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    request = CommandRequest(
        command=sys.executable,
        arguments=["-c", "import time; time.sleep(0.1); print('done')"],
    )
    result = executor.execute(request)
    assert result.elapsed_time >= 0.1
    assert result.elapsed_time < 2.0


def test_executor_config_defaults():
    config = ExecutorConfig()
    assert config.default_timeout == 60.0
    assert config.max_stdout_bytes == 1024 * 1024
    assert config.max_stderr_bytes == 1024 * 1024


def test_workspace_restriction_error():
    error = WorkspaceRestrictionError("test error")
    assert str(error) == "test error"


def test_command_status_enum():
    assert CommandStatus.PENDING.value == "pending"
    assert CommandStatus.RUNNING.value == "running"
    assert CommandStatus.COMPLETED.value == "completed"
    assert CommandStatus.FAILED.value == "failed"
    assert CommandStatus.CANCELLED.value == "cancelled"
    assert CommandStatus.TIMED_OUT.value == "timed_out"


def test_coding_agent_integration(tmp_path):
    from giscode.agentic.actions import ExecutionStep
    from giscode.agentic.loop import CodingAgentLoop
    from giscode.agentic.types import ActionType
    from giscode.inference.api import InferenceAPI
    from giscode.inference.mocks import MockLlamaCppRuntime
    from giscode.task_control.manager import TaskManager

    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    step = ExecutionStep(
        step_id="term-1",
        description="Run terminal command",
        action_type=ActionType.CREATE,
        target_path="",
        content=None,
    )
    result = loop._executor.execute(step)
    assert result is not None
