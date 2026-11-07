import threading
import time

import pytest
import tkinter as tk

from giscode.task_control.manager import TaskManager
from giscode.task_control.types import TaskPhase, TaskState, TaskStatus
from giscode.gui.main_window import MainWindow
from tests.conftest import _FakeGisAiEngine


def test_task_can_be_started():
    manager = TaskManager()
    task = manager.start_task("task-001")
    assert task.task_id == "task-001"
    assert task.status == TaskStatus.RUNNING
    assert task.current_phase == TaskPhase.ANALYZING
    assert task.start_time is not None


def test_only_one_active_task_per_project():
    manager = TaskManager()
    manager.start_task("task-1")
    with pytest.raises(RuntimeError, match="Only one active task"):
        manager.start_task("task-2")


def test_task_phases_can_be_updated():
    manager = TaskManager()
    manager.start_task("task-001")
    manager.update_phase(TaskPhase.CODING)
    task = manager.get_active_task()
    assert task.current_phase == TaskPhase.CODING


def test_elapsed_time_is_tracked():
    manager = TaskManager()
    manager.start_task("task-001")
    time.sleep(0.1)
    elapsed = manager.get_elapsed()
    assert elapsed >= 0.1


def test_maximum_duration_can_be_assigned():
    manager = TaskManager()
    task = manager.start_task("task-001", max_duration=10.0)
    assert task.max_duration == 10.0


def test_timeout_is_enforced_by_application():
    manager = TaskManager()
    manager.start_task("task-001", max_duration=0.1)
    for _ in range(40):
        if manager.get_active_task() is None:
            break
        time.sleep(0.05)
    assert manager.get_active_task() is None


def test_timed_out_task_enters_timeout_state():
    manager = TaskManager()
    manager.start_task("task-001", max_duration=0.1)
    for _ in range(40):
        last = manager.get_last_task()
        if last is not None and last.status == TaskStatus.TIMEOUT:
            break
        time.sleep(0.05)
    last = manager.get_last_task()
    assert last is not None
    assert last.status == TaskStatus.TIMEOUT
    assert last.failure_info is not None
    assert last.failure_info["reason"] == "timeout"


def test_cancel_stops_active_task():
    manager = TaskManager()
    manager.start_task("task-001")
    result = manager.cancel()
    assert result is not None
    assert result.status == TaskStatus.CANCELLED
    assert manager.get_active_task() is None


def test_revoke_stops_task_and_restores_checkpoint():
    manager = TaskManager()
    manager.start_task("task-001")
    manager.checkpoint("before-revoke")
    result = manager.revoke()
    assert result is not None
    assert result.status == TaskStatus.CANCELLED
    assert result._restore_required is True
    assert len(result.checkpoints) == 1
    assert result.checkpoints[0]["label"] == "before-revoke"


def test_task_completion_requires_explicit_success():
    manager = TaskManager()
    manager.start_task("task-001", completion_criteria={"tests_pass": True})
    result = manager.complete(success=True)
    assert result.status == TaskStatus.COMPLETED
    assert result.completion_criteria is not None


def test_failure_produces_structured_information():
    manager = TaskManager()
    manager.start_task("task-001")
    failure = {"reason": "syntax_error", "details": "missing semicolon"}
    result = manager.complete(success=False, failure_info=failure)
    assert result.status == TaskStatus.FAILED
    assert result.failure_info == failure


def test_gui_displays_current_task_phase_and_status(tk_root):
    manager = TaskManager()
    class _LocalFakeInference:
        def get_health(self):
            return {"model_loaded": True}
        def cancel(self):
            pass
    window = MainWindow(tk_root, task_manager=manager, gisai_engine=_FakeGisAiEngine(_LocalFakeInference()))

    manager.start_task("gui-test")
    window._poll_task_state()
    # TaskManager phase/status appear in the status bar (_status_var)
    assert window._status_var.get().startswith("Analyzing: Running")
    assert window._status_var.get().endswith("s)")

    manager.update_phase(TaskPhase.VERIFYING)
    window._poll_task_state()
    assert window._status_var.get().startswith("Verifying")

    manager.cancel()
    window._poll_task_state()
    # After cancel, no active task → status bar shows Ready
    assert window._status_var.get() == "Ready"
