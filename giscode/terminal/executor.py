import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import List, Optional

from giscode.terminal.types import CommandResult, CommandStatus, CommandRequest, ExecutorConfig
from giscode.workspace.workspace import Workspace


class TerminalError(Exception):
    pass


class WorkspaceRestrictionError(TerminalError):
    pass


class CommandExecutor:
    def __init__(self, workspace: Workspace, config: Optional[ExecutorConfig] = None):
        self._workspace = workspace
        self._config = config or ExecutorConfig()
        self._active_process: Optional[subprocess.Popen] = None
        self._active_lock = threading.Lock()
        self._cancel_requested = False

    def execute(self, request: CommandRequest) -> CommandResult:
        working_dir = self._resolve_working_directory(request.working_directory)
        cmd = self._build_command(request)
        timeout = self._resolve_timeout(request)
        with self._active_lock:
            self._cancel_requested = False

        result = CommandResult(
            command=request.command,
            arguments=request.arguments,
            working_directory=working_dir,
            status=CommandStatus.RUNNING,
        )

        start_time = __import__("time").time()
        process = None
        timed_out = False

        try:
            env = self._build_environment(request.environment)
            process = subprocess.Popen(
                cmd,
                cwd=working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                text=False,
                # POSIX: own process group, so a timeout can kill the whole
                # tree without signalling this process.
                start_new_session=(os.name != "nt"),
            )
            with self._active_lock:
                self._active_process = process

            try:
                stdout_data, stderr_data = process.communicate(timeout=timeout)
                elapsed = __import__("time").time() - start_time
                result.stdout = self._cap_output(stdout_data, self._config.max_stdout_bytes)
                result.stderr = self._cap_output(stderr_data, self._config.max_stderr_bytes)
                result.exit_code = process.returncode
                result.elapsed_time = elapsed
                if self._consume_cancel_request():
                    # The process was killed by cancel(); the exit code is the
                    # kill's, not the command's own verdict.
                    result.status = CommandStatus.CANCELLED
                    result.cancelled = True
                    result.error = "command cancelled"
                else:
                    result.status = CommandStatus.COMPLETED if process.returncode == 0 else CommandStatus.FAILED
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_process_tree(process)
                try:
                    stdout_data, stderr_data = process.communicate(timeout=2)
                    result.stdout = self._cap_output(stdout_data or b"", self._config.max_stdout_bytes)
                    result.stderr = self._cap_output(stderr_data or b"", self._config.max_stderr_bytes)
                except Exception:
                    result.stdout = ""
                    result.stderr = ""
                result.exit_code = -1
                result.elapsed_time = __import__("time").time() - start_time
                result.timed_out = True
                result.error = f"command timed out after {int(timeout)} s"
                result.status = CommandStatus.TIMED_OUT
        except Exception as e:
            result.error = str(e)
            result.status = CommandStatus.FAILED
            result.elapsed_time = __import__("time").time() - start_time
        finally:
            with self._active_lock:
                if self._active_process is process:
                    self._active_process = None

        return result

    def _resolve_timeout(self, request: CommandRequest) -> float:
        """Bound one command's wait.

        A plan step, the model or a caller may ask for any timeout, or for
        none at all. Whatever arrives is clamped to positive seconds no larger
        than ``ExecutorConfig.max_timeout``, so no single command can wait
        longer than its budget (an unclamped request blocked judge workers for
        minutes: judge_9b state J1_a1).
        """
        raw = request.timeout if request.timeout is not None else self._config.default_timeout
        try:
            timeout = float(raw)
        except (TypeError, ValueError):
            timeout = float(self._config.default_timeout)
        if timeout <= 0:
            timeout = float(self._config.default_timeout)
        ceiling = float(self._config.max_timeout or 0)
        if ceiling > 0 and timeout > ceiling:
            timeout = ceiling
        return timeout

    def _resolve_working_directory(self, working_directory: Optional[str]) -> str:
        if working_directory is None:
            return str(self._workspace.root_path)

        path = Path(working_directory)
        if path.is_absolute():
            resolved = path.resolve()
        else:
            resolved = (self._workspace.root_path / path).resolve()

        try:
            resolved.relative_to(self._workspace.root_path)
        except ValueError:
            raise WorkspaceRestrictionError(
                f"Working directory escapes workspace: {working_directory}"
            )

        if ".." in Path(working_directory).parts:
            raise WorkspaceRestrictionError(
                f"Path traversal detected in working directory: {working_directory}"
            )

        if not resolved.is_dir():
            raise WorkspaceRestrictionError(
                f"Working directory does not exist: {resolved}"
            )

        return str(resolved)

    def _build_command(self, request: CommandRequest) -> List[str]:
        return [request.command] + list(request.arguments)

    def _build_environment(self, extra: Optional[dict]) -> Optional[dict]:
        if extra is None:
            return None
        env = os.environ.copy()
        env.update(extra)
        return env

    def _cap_output(self, data: bytes, limit: int) -> str:
        if len(data) > limit:
            return data[:limit].decode("utf-8", errors="replace") + "\n[OUTPUT TRUNCATED]"
        return data.decode("utf-8", errors="replace")

    def _terminate_process(self, process: subprocess.Popen) -> None:
        self._kill_process_tree(process)

    def _kill_process_tree(self, process: subprocess.Popen) -> None:
        """Kill a command and every process it started.

        ``Popen.kill()`` only signals the direct child. On Windows a shell
        exits while the grandchild it launched keeps running and keeps the
        stdout/stderr pipes open, so the reader threads never see EOF; that is
        how a bounded command still held a worker for minutes. Kill the tree
        by its own PID, then the direct child as a fallback.
        """
        pid = getattr(process, "pid", None)
        if pid:
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                except Exception:
                    pass
            else:
                try:
                    group = os.getpgid(pid)
                    if group != os.getpgrp():
                        os.killpg(group, signal.SIGKILL)
                except Exception:
                    pass
        try:
            process.kill()
        except Exception:
            pass

    def _consume_cancel_request(self) -> bool:
        with self._active_lock:
            requested = self._cancel_requested
            self._cancel_requested = False
        return requested

    def cancel(self) -> None:
        """Stop the running command and its whole process tree."""
        with self._active_lock:
            self._cancel_requested = True
            process = self._active_process
        if process is not None:
            self._kill_process_tree(process)
