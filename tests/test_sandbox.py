import os
import sys
import threading
import time

import pytest

from giscode.agentic.loop import CodingAgentLoop
from giscode.agentic.types import ActionType, ExecutionStep
from giscode.inference.api import InferenceAPI
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.sandbox.backends import DockerBackend, UnavailableBackend
from giscode.sandbox.executor import SandboxExecutor
from giscode.sandbox.types import SandboxBackend, SandboxConfig, SandboxRequest, SandboxResult, SandboxStatus
from giscode.task_control.manager import TaskManager
from giscode.terminal.executor import CommandExecutor, CommandRequest
from giscode.workspace.workspace import Workspace


def test_sandbox_executor_initialization(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = SandboxConfig(backend=SandboxBackend.UNAVAILABLE)
    executor = SandboxExecutor(workspace, config=config)
    assert executor is not None
    assert executor.backend == SandboxBackend.UNAVAILABLE
    assert executor.available is False


def test_sandbox_unavailable_backend(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = SandboxExecutor(workspace)
    result = executor.create_sandbox()
    assert result.status == SandboxStatus.UNAVAILABLE
    assert result.error is not None
    assert "unavailable" in result.error.lower() or "backend" in result.error.lower()


def test_sandbox_unavailable_execute(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = SandboxExecutor(workspace)
    request = SandboxRequest(command="echo", arguments=["hello"])
    result = executor.execute(request)
    assert result.status == SandboxStatus.UNAVAILABLE
    assert result.error is not None


def test_sandbox_docker_backend_availability():
    config = SandboxConfig(backend=SandboxBackend.DOCKER, image="nonexistent-image:latest")
    backend = DockerBackend(config)
    assert backend.available is False or backend.has_suitable_image() is False


def test_sandbox_docker_backend_no_suitable_image():
    config = SandboxConfig(backend=SandboxBackend.DOCKER, image="nonexistent-image:latest")
    backend = DockerBackend(config)
    assert backend.has_suitable_image() is False


def test_sandbox_create_sandbox_unavailable(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = SandboxConfig(backend=SandboxBackend.UNAVAILABLE)
    executor = SandboxExecutor(workspace, config=config)
    result = executor.create_sandbox()
    assert result.status == SandboxStatus.UNAVAILABLE


def test_sandbox_destroy_idle(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = SandboxExecutor(workspace)
    result = executor.destroy()
    assert result is None


def test_sandbox_get_status_idle(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = SandboxExecutor(workspace)
    assert executor.get_status() == SandboxStatus.IDLE


def test_sandbox_config_defaults():
    config = SandboxConfig()
    assert config.backend == SandboxBackend.UNAVAILABLE
    assert config.network_enabled is False
    assert config.default_timeout == 60.0
    assert config.remove_after_execution is True
    assert config.workspace_mount_path == "/workspace"


def test_sandbox_config_custom():
    config = SandboxConfig(
        backend=SandboxBackend.DOCKER,
        image="python:3.11-slim",
        network_enabled=True,
        memory_limit="256m",
        cpu_limit=1.0,
        default_timeout=120.0,
    )
    assert config.backend == SandboxBackend.DOCKER
    assert config.image == "python:3.11-slim"
    assert config.network_enabled is True
    assert config.memory_limit == "256m"
    assert config.cpu_limit == 1.0
    assert config.default_timeout == 120.0


def test_sandbox_request_defaults():
    request = SandboxRequest(command="echo", arguments=["hello"])
    assert request.command == "echo"
    assert request.arguments == ["hello"]
    assert request.working_directory == "/workspace"
    assert request.timeout is None
    assert request.network_enabled is None


def test_sandbox_result_fields():
    result = SandboxResult(
        sandbox_id="test-123",
        backend=SandboxBackend.UNAVAILABLE,
        command="echo",
        exit_code=0,
        stdout="hello\n",
        stderr="",
        elapsed_time=0.1,
        status=SandboxStatus.COMPLETED,
    )
    assert result.sandbox_id == "test-123"
    assert result.stdout == "hello\n"
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.cancelled is False


def test_sandbox_status_enum():
    assert SandboxStatus.IDLE.value == "idle"
    assert SandboxStatus.CREATING.value == "creating"
    assert SandboxStatus.RUNNING.value == "running"
    assert SandboxStatus.COMPLETED.value == "completed"
    assert SandboxStatus.FAILED.value == "failed"
    assert SandboxStatus.CANCELLED.value == "cancelled"
    assert SandboxStatus.TIMED_OUT.value == "timed_out"
    assert SandboxStatus.DESTROYED.value == "destroyed"
    assert SandboxStatus.UNAVAILABLE.value == "unavailable"


def test_sandbox_backend_enum():
    assert SandboxBackend.DOCKER.value == "docker"
    assert SandboxBackend.UNAVAILABLE.value == "unavailable"


def test_unavailable_backend_create():
    backend = UnavailableBackend(SandboxConfig())
    result = backend.create_sandbox("/tmp")
    assert result.status == SandboxStatus.UNAVAILABLE
    assert result.error is not None


def test_unavailable_backend_execute():
    backend = UnavailableBackend(SandboxConfig())
    request = SandboxRequest(command="echo", arguments=["hello"])
    result = backend.execute(request, "/tmp")
    assert result.status == SandboxStatus.UNAVAILABLE
    assert result.error is not None


def test_unavailable_backend_destroy():
    backend = UnavailableBackend(SandboxConfig())
    backend.destroy("test-id")
    assert True


def test_docker_backend_not_available_without_docker():
    config = SandboxConfig(backend=SandboxBackend.DOCKER, image="python:3.11-slim")
    backend = DockerBackend(config)
    if not backend.available:
        result = backend.create_sandbox("/tmp")
        assert result.status == SandboxStatus.UNAVAILABLE


def test_host_command_executor_remains_functional(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    result = executor.execute(CommandRequest(command=sys.executable, arguments=["-c", "print('host-ok')"]))
    assert result.status.value == "completed"
    assert "host-ok" in result.stdout


def test_coding_agent_can_invoke_sandbox_interface(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)
    assert loop._sandbox_executor is not None
    request = SandboxRequest(command="echo", arguments=["sandbox-test"])
    result = loop._sandbox_executor.execute(request)
    assert result is not None
    assert isinstance(result, SandboxResult)


def test_sandbox_network_disabled_by_default():
    config = SandboxConfig()
    assert config.network_enabled is False


def test_sandbox_filesystem_isolation_concept(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = SandboxConfig(backend=SandboxBackend.UNAVAILABLE)
    executor = SandboxExecutor(workspace, config=config)
    forbidden = r"C:\Users\example\Desktop\Projects\GisCodding"
    assert executor.available is False
    request = SandboxRequest(command="ls", arguments=[forbidden])
    result = executor.execute(request)
    assert result.status == SandboxStatus.UNAVAILABLE


def test_sandbox_workspace_only_access_concept(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    config = SandboxConfig(backend=SandboxBackend.UNAVAILABLE)
    executor = SandboxExecutor(workspace, config=config)
    outside = str(tmp_path / "outside")
    request = SandboxRequest(command="ls", arguments=[outside])
    result = executor.execute(request)
    assert result.status == SandboxStatus.UNAVAILABLE


def test_sandbox_previous_tasks_remain_intact():
    from giscode.config.manager import ConfigManager
    from giscode.task_control.manager import TaskManager

    settings = ConfigManager()
    manager = TaskManager()
    task = manager.start_task("preserve-test")
    assert task.status.value == "running"
    manager.complete(success=True)
    assert manager.get_last_task().status.value == "completed"
