"""A command must never outlive its budget, and a timed-out run is not a pass.

Evidence this is built from: judge_9b state J1_a1/J3_a1/J6_a1, where the worker
thread sat in ``subprocess.communicate()`` for minutes while the command's
grandchild held the stdout/stderr pipes open, and the task never reached a
terminal state before the 15-minute limit.

Only dummy children started by these tests are ever killed, by PID.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from giscode.terminal.executor import CommandExecutor
from giscode.terminal.types import (
    CommandRequest,
    CommandStatus,
    ExecutorConfig,
)
from giscode.workspace.workspace import Workspace
from tests.conftest import requires_gisai

# A child that outlives its parent's budget and starts a grandchild which
# inherits the pipes: the exact shape that blocked the judge workers.
_CHILD_CODE = (
    "import os, pathlib, subprocess, sys, time\n"
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
    "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
    "pathlib.Path(sys.argv[2]).write_text(str(grandchild.pid))\n"
    "time.sleep(300)\n"
)


def _alive(pid: int) -> bool:
    """Whether a PID is still present, using only the standard library."""
    if os.name == "nt":
        try:
            listed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        except Exception:
            return True
        return str(pid) in listed
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until_dead(pid: int, timeout_s: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


def _tree_request(tmp_path: Path, timeout: float) -> tuple[CommandExecutor, CommandRequest]:
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)
    request = CommandRequest(
        command=sys.executable,
        arguments=[
            "-c",
            _CHILD_CODE,
            str(tmp_path / "child.pid"),
            str(tmp_path / "grandchild.pid"),
        ],
        working_directory=str(workspace.root_path),
        timeout=timeout,
    )
    return executor, request


def _pids(tmp_path: Path) -> tuple[int, int]:
    child = int((tmp_path / "child.pid").read_text(encoding="utf-8").strip())
    grandchild = int((tmp_path / "grandchild.pid").read_text(encoding="utf-8").strip())
    return child, grandchild


def test_a_timed_out_command_is_killed_with_its_grandchild(tmp_path):
    executor, request = _tree_request(tmp_path, timeout=5)

    result = executor.execute(request)

    assert result.status == CommandStatus.TIMED_OUT
    assert result.timed_out is True
    assert result.error == "command timed out after 5 s"
    child_pid, grandchild_pid = _pids(tmp_path)
    assert _wait_until_dead(child_pid), "the command's own process must not survive its timeout"
    assert _wait_until_dead(grandchild_pid), (
        "the grandchild that holds the pipes open must be killed too, or a "
        "bounded command still holds the worker"
    )


def test_cancel_kills_the_process_tree_and_reports_cancelled(tmp_path):
    executor, request = _tree_request(tmp_path, timeout=600)
    results: list = []

    worker = threading.Thread(target=lambda: results.append(executor.execute(request)))
    worker.start()
    # Wait for the child to exist before cancelling.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (tmp_path / "child.pid").exists():
        time.sleep(0.05)
    assert (tmp_path / "child.pid").exists(), "the dummy child never started"

    executor.cancel()
    worker.join(timeout=60)

    assert not worker.is_alive(), "cancel must release the waiting worker"
    assert results and results[0].status == CommandStatus.CANCELLED
    assert results[0].cancelled is True
    assert results[0].timed_out is False
    child_pid, grandchild_pid = _pids(tmp_path)
    assert _wait_until_dead(child_pid)
    assert _wait_until_dead(grandchild_pid)


def test_a_fast_command_is_unaffected(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)

    result = executor.execute(
        CommandRequest(
            command=sys.executable,
            arguments=["-c", "print('ok')"],
            working_directory=str(workspace.root_path),
        )
    )

    assert result.status == CommandStatus.COMPLETED
    assert result.exit_code == 0
    assert "ok" in result.stdout
    assert result.timed_out is False
    assert result.cancelled is False
    assert result.error is None


def test_a_model_supplied_timeout_is_clamped_to_the_ceiling(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace, ExecutorConfig(default_timeout=60.0, max_timeout=600.0))

    def resolve(raw):
        return executor._resolve_timeout(CommandRequest(command="x", timeout=raw))

    # The model or a plan step may ask for anything at all.
    assert resolve(99999) == 600.0
    assert resolve("99999") == 600.0
    assert resolve(600) == 600.0
    # Normal values pass through.
    assert resolve(30) == 30.0
    # Unusable values fall back to the configured default, never to no timeout.
    assert resolve(None) == 60.0
    assert resolve(0) == 60.0
    assert resolve(-5) == 60.0
    assert resolve("soon") == 60.0


def test_the_config_ceiling_bounds_a_request_that_omits_a_timeout(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace, ExecutorConfig(default_timeout=90.0, max_timeout=45.0))

    assert executor._resolve_timeout(CommandRequest(command="x")) == 45.0


# ---------------------------------------------------------------------------
# The verifier must never turn a timed-out run into a pass.
# ---------------------------------------------------------------------------


class _TimedOutExecutor:
    """A terminal executor whose run timed out, with an empty output."""

    def __init__(self) -> None:
        self.calls: list = []

    def execute(self, request):
        self.calls.append(request)
        return MagicMock(
            exit_code=-1,
            stdout="",
            stderr="",
            timed_out=True,
            cancelled=False,
            error="command timed out after 120 s",
        )


def _gradle_project(tmp_path: Path) -> Path:
    (tmp_path / "settings.gradle.kts").write_text(
        'rootProject.name = "app"\ninclude(":composeApp")\n', encoding="utf-8"
    )
    (tmp_path / "build.gradle.kts").write_text("plugins { }\n", encoding="utf-8")
    module = tmp_path / "composeApp"
    module.mkdir()
    (module / "build.gradle.kts").write_text(
        "plugins {\n    id(\"org.jetbrains.kotlin.multiplatform\")\n}\n"
        'kotlin { jvm("desktop") }\n',
        encoding="utf-8",
    )
    (tmp_path / "gradlew.bat").write_text("@echo off\n", encoding="utf-8")
    return module


def test_a_timed_out_gradle_run_is_reported_as_timed_out_not_passed(tmp_path):
    from giscode.agentic.verifier import Verifier

    _gradle_project(tmp_path)
    executor = _TimedOutExecutor()

    baseline = Verifier(workspace_root=tmp_path, terminal_executor=executor).baseline()

    assert baseline is not None
    assert executor.calls, "the project's own wrapper must still be the thing that ran"
    assert Path(executor.calls[0].command).name == "gradlew.bat"
    assert baseline.status != "passed"
    assert baseline.status == "could_not_run"
    assert "tests timed out" in baseline.reason


def test_a_timed_out_python_run_is_reported_as_timed_out_not_passed(tmp_path):
    from giscode.agentic.verifier import Verifier

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mathx.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    executor = _TimedOutExecutor()

    baseline = Verifier(workspace_root=tmp_path, terminal_executor=executor).baseline()

    assert baseline is not None
    assert executor.calls
    assert baseline.status == "could_not_run"
    assert "tests timed out" in baseline.reason


def test_the_gradle_budget_stays_within_the_executor_ceiling():
    """The verifier's own budget must be reachable, not silently clamped away."""
    from giscode.agentic.verifier import GRADLE_TIMEOUT_S

    ceiling = ExecutorConfig().max_timeout
    assert GRADLE_TIMEOUT_S <= ceiling


@requires_gisai
def test_the_project_root_facade_delegates_cancel(tmp_path):
    from giscode.gisai.tools.adapters import ProjectRootCommandExecutor

    workspace = Workspace(tmp_path / "ws")
    inner = CommandExecutor(workspace)
    facade = ProjectRootCommandExecutor(inner)

    facade.cancel()

    assert inner._cancel_requested is True, (
        "cancelling through the facade must reach the executor that owns the PID"
    )


@requires_gisai
def test_cancelling_a_gisai_task_kills_the_running_command(tmp_path):
    """The engine's cancel path must reach the executor, not just the task."""
    from giscode.gisai.engine import GisAiEngine
    from giscode.gisai.types import GisAiTask, GisAiTaskStatus

    class _Holder:
        def __init__(self) -> None:
            self.cancelled = 0

        def cancel(self) -> None:
            self.cancelled += 1

    holder = _Holder()
    engine = GisAiEngine()
    engine._project_command_executor = holder
    task = GisAiTask(
        task_id="task-bounding",
        project_id="proj-bounding",
        project_path=str(tmp_path),
        section="Coding",
        raw_request="run the tests",
        intent="debugging",
        required_capabilities=["coding"],
        status=GisAiTaskStatus.RUNNING,
    )
    engine._cancel_event.set()

    assert engine._is_cancelled(task) is True
    assert holder.cancelled == 1, "a cancelled task must not leave its command running"


@pytest.mark.parametrize("value", ["", None])
def test_the_cancel_request_is_cleared_between_commands(tmp_path, value):
    workspace = Workspace(tmp_path / "ws")
    executor = CommandExecutor(workspace)

    executor.cancel()
    result = executor.execute(
        CommandRequest(
            command=sys.executable,
            arguments=["-c", "print('ok')"],
            working_directory=str(workspace.root_path),
            timeout=None,
        )
    )

    assert result.status == CommandStatus.COMPLETED
    assert result.cancelled is False
