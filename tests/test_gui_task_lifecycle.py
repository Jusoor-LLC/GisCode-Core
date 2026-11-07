import time
import tkinter as tk

from giscode.gui.main_window import MainWindow
from giscode.task_control.manager import TaskManager
from giscode.task_control.types import TaskStatus
from tests.conftest import _FakeGisAiEngine
from tests.conftest import requires_gisai


class _LifecycleInference:
    def __init__(self, reply="SYSTEM OK"):
        self.reply = reply
        self.cancelled = False
        self.chat_calls = 0

    def get_health(self):
        return {"model_loaded": True}

    def cancel(self):
        self.cancelled = True

    def chat_generate(self, request):
        self.chat_calls += 1
        return type("R", (), {"text": self.reply})()


class _LifecycleGisAiEngine(_FakeGisAiEngine):
    """Drives a specific gisai-task terminal state (completed/failed/
    cancelled) so the GUI dispatch lifecycle can be exercised end to end."""

    def __init__(self, inference, outcome="completed"):
        super().__init__(inference)
        self.outcome = outcome

    def execute(self, task, on_status=None):
        from giscode.gisai.types import GisAiPhase, GisAiTaskStatus
        if self.outcome == "cancelled":
            task.status = GisAiTaskStatus.CANCELLED
            task.set_state(GisAiPhase.CANCELLED)
            return {"error": "Operation cancelled"}
        if self.outcome == "failed":
            task.status = GisAiTaskStatus.FAILED
            task.set_state(GisAiPhase.FAILED)
            return {"error": "boom: server exploded"}
        if self.outcome == "raise_cancelled":
            from giscode.inference.errors import RequestCancelledError
            raise RequestCancelledError("cancelled by user")
        task.status = GisAiTaskStatus.COMPLETED
        task.set_state(GisAiPhase.COMPLETE)
        return {"response": "SYSTEM OK"}


class _FakeRegistry:
    def list_all(self):
        return []


class _FakeModelManager:
    def __init__(self, inference):
        self._inference = inference

    def get_current_model(self):
        return None

    def auto_register_discovered(self):
        pass

    def get_registry(self):
        return _FakeRegistry()

    def get_health(self):
        return {"model_loaded": True}


def _make_window(root, outcome="completed", task_manager=None):
    manager = task_manager or TaskManager()
    inference = _LifecycleInference()
    engine = _LifecycleGisAiEngine(inference, outcome=outcome)
    model_manager = _FakeModelManager(inference)
    return MainWindow(root, task_manager=manager, model_manager=model_manager, gisai_engine=engine), manager


def _send(window: MainWindow, text: str) -> None:
    window.task_entry.delete(0, tk.END)
    window.task_entry.insert(0, text)
    window._send_task()


def _wait_idle(window: MainWindow, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while window._active_task and time.time() < deadline:
        window.root.update()
        time.sleep(0.005)
    assert not window._active_task


def _rendered(window: MainWindow) -> str:
    return window.response_text.get("1.0", tk.END)


@requires_gisai
def test_successful_task_reaches_terminal_completed(tk_root):
    window, manager = _make_window(tk_root, outcome="completed")
    try:
        _send(window, "do the thing")
        _wait_idle(window)
        last = manager.get_last_task()
        assert last is not None
        assert last.status == TaskStatus.COMPLETED
        assert manager.get_active_task() is None
        assert window._ui_state == "IDLE"
        assert window.phase_var.get() == "Completed"
        assert "SYSTEM OK" in _rendered(window)
    finally:
        manager.cancel()


@requires_gisai
def test_failed_task_reaches_terminal_failed(tk_root):
    window, manager = _make_window(tk_root, outcome="failed")
    try:
        _send(window, "do the thing")
        _wait_idle(window)
        last = manager.get_last_task()
        assert last is not None
        assert last.status == TaskStatus.FAILED
        assert manager.get_active_task() is None
        assert window._ui_state == "ERROR"
        assert "boom: server exploded" in _rendered(window)
    finally:
        manager.cancel()


@requires_gisai
def test_cancelled_task_reaches_terminal_cancelled_not_error(tk_root):
    window, manager = _make_window(tk_root, outcome="cancelled")
    try:
        _send(window, "do the thing")
        _wait_idle(window)
        last = manager.get_last_task()
        assert last is not None
        assert last.status == TaskStatus.CANCELLED
        assert manager.get_active_task() is None
        assert window._ui_state == "STOPPED"
        assert window.phase_var.get() == "Cancelled"
        rendered = _rendered(window)
        assert "Operation cancelled" in rendered
        assert "<error>" not in rendered
    finally:
        manager.cancel()


@requires_gisai
def test_request_cancel_exception_reaches_terminal_cancelled(tk_root):
    window, manager = _make_window(tk_root, outcome="raise_cancelled")
    try:
        _send(window, "do the thing")
        _wait_idle(window)
        last = manager.get_last_task()
        assert last is not None
        assert last.status == TaskStatus.CANCELLED
        assert manager.get_active_task() is None
        assert window._ui_state == "STOPPED"
        rendered = _rendered(window)
        assert "Operation cancelled" in rendered
        assert "<error>" not in rendered
    finally:
        manager.cancel()


@requires_gisai
def test_consecutive_tasks_do_not_rely_on_watchdog(tk_root):
    window, manager = _make_window(tk_root, outcome="completed")
    try:
        _send(window, "first")
        _wait_idle(window)
        assert manager.get_active_task() is None
        _send(window, "second")
        _wait_idle(window)
        assert _rendered(window).count("SYSTEM OK") >= 2
        assert manager.get_active_task() is None
        last = manager.get_last_task()
        assert last.status == TaskStatus.COMPLETED
    finally:
        manager.cancel()


def test_force_timeout_cancels_not_completes(tk_root):
    window, manager = _make_window(tk_root, outcome="completed", task_manager=TaskManager())
    manager.start_task("gui-timeout", max_duration=window._task_timeout_s)
    try:
        window._force_task_timeout()
        last = manager.get_last_task()
        assert last is not None
        assert last.status == TaskStatus.CANCELLED
        assert manager.get_active_task() is None
    finally:
        manager.cancel()