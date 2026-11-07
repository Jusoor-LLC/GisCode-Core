import subprocess
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

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
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace


class OpenHandsAdapter(ABC):
    def __init__(
        self,
        workspace: Workspace,
        config: Optional[OpenHandsConfig] = None,
        task_manager: Optional[TaskManager] = None,
    ):
        self._workspace = workspace
        self._config = config or OpenHandsConfig()
        self._task_manager = task_manager
        self._status = OpenHandsStatus.IDLE
        self._current_task_id: Optional[str] = None

    @abstractmethod
    def execute(self, request: OpenHandsRequest) -> OpenHandsResult:
        pass

    @abstractmethod
    def cancel(self) -> Optional[OpenHandsResult]:
        pass

    def get_status(self) -> OpenHandsStatus:
        return self._status

    def validate_path(self, path: str) -> Path:
        p = Path(path)
        if p.is_absolute():
            resolved = p.resolve()
        else:
            resolved = (self._workspace.root_path / p).resolve()

        try:
            resolved.relative_to(self._workspace.root_path)
        except ValueError:
            raise OpenHandsWorkspaceError(f"Path escapes workspace: {path}")

        if ".." in Path(path).parts:
            raise OpenHandsWorkspaceError(f"Path traversal detected: {path}")

        return resolved

    def _check_task_control(self) -> None:
        if self._task_manager is None:
            return
        active = self._task_manager.get_active_task()
        if active is None:
            last = self._task_manager.get_last_task()
            if last is not None and last.status.value == "timeout":
                raise OpenHandsTimeoutError("Task timed out")
            raise OpenHandsCancelledError("No active task")
        if active.status.value == "cancelled":
            raise OpenHandsCancelledError("Task was cancelled")
        if active.status.value == "timeout":
            raise OpenHandsTimeoutError("Task timed out")


class MockOpenHandsAdapter(OpenHandsAdapter):
    def __init__(
        self,
        workspace: Workspace,
        config: Optional[OpenHandsConfig] = None,
        task_manager: Optional[TaskManager] = None,
        behavior: str = "success",
        delay: float = 0.0,
    ):
        super().__init__(workspace, config, task_manager)
        self._behavior = behavior
        self._delay = delay
        self._call_count = 0
        self._cancelled = False

    def execute(self, request: OpenHandsRequest) -> OpenHandsResult:
        self._call_count += 1
        self._current_task_id = request.task_id
        self._status = OpenHandsStatus.RUNNING

        try:
            self._check_task_control()
        except (OpenHandsCancelledError, OpenHandsTimeoutError):
            self._status = OpenHandsStatus.CANCELLED
            return OpenHandsResult(
                task_id=request.task_id,
                status=OpenHandsStatus.CANCELLED,
                cancelled=True,
                error="Task was cancelled or timed out",
            )

        if self._behavior == "unavailable":
            self._status = OpenHandsStatus.UNAVAILABLE
            return OpenHandsResult(
                task_id=request.task_id,
                status=OpenHandsStatus.UNAVAILABLE,
                error="OpenHands runtime is unavailable",
            )

        if self._delay > 0:
            time.sleep(self._delay)

        try:
            self._check_task_control()
        except OpenHandsCancelledError:
            self._status = OpenHandsStatus.CANCELLED
            return OpenHandsResult(
                task_id=request.task_id,
                status=OpenHandsStatus.CANCELLED,
                cancelled=True,
                error="Task was cancelled during execution",
            )
        except OpenHandsTimeoutError:
            self._status = OpenHandsStatus.TIMED_OUT
            return OpenHandsResult(
                task_id=request.task_id,
                status=OpenHandsStatus.TIMED_OUT,
                timed_out=True,
                error="Task timed out during execution",
            )

        if self._cancelled:
            self._status = OpenHandsStatus.CANCELLED
            return OpenHandsResult(
                task_id=request.task_id,
                status=OpenHandsStatus.CANCELLED,
                cancelled=True,
                error="Execution was cancelled",
            )

        if self._behavior == "failure":
            self._status = OpenHandsStatus.FAILED
            return OpenHandsResult(
                task_id=request.task_id,
                status=OpenHandsStatus.FAILED,
                exit_code=1,
                stderr="Mock execution failed",
                error="Mock execution failed",
            )

        self._status = OpenHandsStatus.COMPLETED
        return OpenHandsResult(
            task_id=request.task_id,
            status=OpenHandsStatus.COMPLETED,
            phase=OpenHandsPhase.COMPLETE,
            exit_code=0,
            stdout="Mock execution completed successfully",
        )

    def cancel(self) -> Optional[OpenHandsResult]:
        self._cancelled = True
        if self._current_task_id is not None:
            self._status = OpenHandsStatus.CANCELLED
            return OpenHandsResult(
                task_id=self._current_task_id,
                status=OpenHandsStatus.CANCELLED,
                cancelled=True,
                error="Execution was cancelled by user",
            )
        return None


class RealOpenHandsAdapter(OpenHandsAdapter):
    def __init__(
        self,
        workspace: Workspace,
        config: Optional[OpenHandsConfig] = None,
        task_manager: Optional[TaskManager] = None,
        runtime_command: Optional[str] = None,
    ):
        super().__init__(workspace, config, task_manager)
        self._runtime_command = runtime_command or self._discover_runtime()
        self._process: Optional[subprocess.Popen] = None
        self._process_lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._runtime_available = self._runtime_command is not None

    def _discover_runtime(self) -> Optional[str]:
        import shutil

        if self._config.executable:
            return self._config.executable
        return shutil.which("openhands")

    @property
    def runtime_available(self) -> bool:
        return self._runtime_available

    def get_runtime_state(self) -> str:
        if not self._runtime_available:
            return "not_installed"
        return self._status.value

    def execute(self, request: OpenHandsRequest) -> OpenHandsResult:
        if not self._runtime_available:
            self._status = OpenHandsStatus.UNAVAILABLE
            return OpenHandsResult(
                task_id=request.task_id,
                status=OpenHandsStatus.UNAVAILABLE,
                error="OpenHands runtime is not installed or not in PATH",
            )

        try:
            self._check_task_control()
        except (OpenHandsCancelledError, OpenHandsTimeoutError):
            self._status = OpenHandsStatus.CANCELLED
            return OpenHandsResult(
                task_id=request.task_id,
                status=OpenHandsStatus.CANCELLED,
                cancelled=True,
                error="Task was cancelled or timed out before execution",
            )

        working_dir = self._validate_working_directory(request.working_directory)
        cmd = self._build_command(request)

        self._status = OpenHandsStatus.RUNNING
        self._current_task_id = request.task_id

        process = subprocess.Popen(
            cmd,
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self._process_lock:
            self._process = process

        self._monitor_thread = threading.Thread(
            target=self._monitor_process,
            args=(process,),
            daemon=True,
        )
        self._monitor_thread.start()

        try:
            stdout_data, stderr_data = process.communicate(timeout=self._config.timeout)
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            self._terminate_process(process)
            try:
                stdout_data, stderr_data = process.communicate(timeout=5)
            except Exception:
                stdout_data, stderr_data = b"", b""
            exit_code = -1
            self._status = OpenHandsStatus.TIMED_OUT
            result = OpenHandsResult(
                task_id=request.task_id,
                status=OpenHandsStatus.TIMED_OUT,
                exit_code=exit_code,
                stdout=self._decode(stdout_data),
                stderr=self._decode(stderr_data),
                elapsed_time=self._config.timeout,
                timed_out=True,
                error="Task exceeded maximum duration",
            )
            self._cleanup_process(process)
            return result
        except Exception as e:
            self._terminate_process(process)
            try:
                stdout_data, stderr_data = process.communicate(timeout=5)
            except Exception:
                stdout_data, stderr_data = b"", b""
            exit_code = -1
            self._status = OpenHandsStatus.FAILED
            result = OpenHandsResult(
                task_id=request.task_id,
                status=OpenHandsStatus.FAILED,
                exit_code=exit_code,
                stdout=self._decode(stdout_data),
                stderr=self._decode(stderr_data),
                elapsed_time=0.0,
                error=str(e),
            )
            self._cleanup_process(process)
            return result

        try:
            self._check_task_control()
        except OpenHandsCancelledError:
            self._status = OpenHandsStatus.CANCELLED
            result = OpenHandsResult(
                task_id=request.task_id,
                status=OpenHandsStatus.CANCELLED,
                exit_code=exit_code,
                stdout=self._decode(stdout_data),
                stderr=self._decode(stderr_data),
                elapsed_time=0.0,
                cancelled=True,
                error="Task was cancelled during execution",
            )
            self._cleanup_process(process)
            return result
        except OpenHandsTimeoutError:
            self._status = OpenHandsStatus.TIMED_OUT
            result = OpenHandsResult(
                task_id=request.task_id,
                status=OpenHandsStatus.TIMED_OUT,
                exit_code=exit_code,
                stdout=self._decode(stdout_data),
                stderr=self._decode(stderr_data),
                elapsed_time=0.0,
                timed_out=True,
                error="Task timed out during execution",
            )
            self._cleanup_process(process)
            return result

        status = OpenHandsStatus.COMPLETED if exit_code == 0 else OpenHandsStatus.FAILED
        self._status = status
        result = OpenHandsResult(
            task_id=request.task_id,
            status=status,
            phase=OpenHandsPhase.COMPLETE if exit_code == 0 else None,
            exit_code=exit_code,
            stdout=self._decode(stdout_data),
            stderr=self._decode(stderr_data),
            elapsed_time=0.0,
            error=None if exit_code == 0 else "Runtime execution failed",
        )
        self._cleanup_process(process)
        return result

    def _monitor_process(self, process: subprocess.Popen) -> None:
        while True:
            if process.poll() is not None:
                break
            try:
                self._check_task_control()
            except (OpenHandsCancelledError, OpenHandsTimeoutError):
                self._terminate_process(process)
                break
            time.sleep(0.05)

    def _terminate_process(self, process: subprocess.Popen) -> None:
        try:
            process.kill()
        except Exception:
            pass

    def _cleanup_process(self, process: subprocess.Popen) -> None:
        with self._process_lock:
            if self._process is process:
                self._process = None
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=1.0)
            self._monitor_thread = None

    def cancel(self) -> Optional[OpenHandsResult]:
        with self._process_lock:
            if self._process is not None:
                self._terminate_process(self._process)
        if self._current_task_id is not None:
            self._status = OpenHandsStatus.CANCELLED
            return OpenHandsResult(
                task_id=self._current_task_id,
                status=OpenHandsStatus.CANCELLED,
                cancelled=True,
                error="Execution was cancelled by user",
            )
        return None

    def _validate_working_directory(self, working_directory: str) -> str:
        if working_directory == self._config.workspace_mount_path:
            return str(self._workspace.root_path)

        path = Path(working_directory)
        if path.is_absolute():
            resolved = path.resolve()
        else:
            resolved = (self._workspace.root_path / path).resolve()

        if resolved.exists() and resolved.is_file():
            resolved = resolved.parent
        elif not resolved.exists():
            candidate = resolved.parent
            if candidate.exists() and candidate.is_dir():
                resolved = candidate

        try:
            resolved.relative_to(self._workspace.root_path)
        except ValueError:
            raise OpenHandsWorkspaceError(
                f"Working directory escapes workspace: {working_directory}"
            )

        if ".." in Path(working_directory).parts:
            raise OpenHandsWorkspaceError(
                f"Path traversal detected in working directory: {working_directory}"
            )

        if not resolved.is_dir():
            raise OpenHandsWorkspaceError(
                f"Working directory does not exist: {resolved}"
            )

        return str(resolved)

    def _build_command(self, request: OpenHandsRequest) -> list:
        cmd = [self._runtime_command] + list(self._config.runtime_args)
        if request.command:
            cmd.append(request.command)
        cmd.extend(request.arguments)
        return cmd

    @staticmethod
    def _decode(data: bytes) -> str:
        if not data:
            return ""
        return data.decode("utf-8", errors="replace")
