import time
import tkinter as tk

import pytest

from giscode.gui.main_window import ACTIVITY_RUNNING_TESTS, MainWindow
from giscode.task_control.manager import TaskManager
from tests.conftest import _FakeGisAiEngine
from tests.conftest import requires_gisai


class _StatusInference:
    def __init__(self, reply="STATUS OK"):
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


class _StatusEngine(_FakeGisAiEngine):
    """Fake engine that exercises the real task states and on_status text."""

    def __init__(self, inference, outcome="completed", status_messages=()):
        super().__init__(inference)
        self.outcome = outcome
        self.status_messages = list(status_messages)

    def execute(self, task, on_status=None):
        from giscode.gisai.types import GisAiPhase, GisAiTaskStatus

        for msg in self.status_messages:
            if on_status is not None:
                on_status(msg)
        if self.outcome == "cancelled":
            task.status = GisAiTaskStatus.CANCELLED
            task.set_state(GisAiPhase.CANCELLED)
            return {"error": "Operation cancelled"}
        if self.outcome == "failed":
            task.status = GisAiTaskStatus.FAILED
            task.set_state(GisAiPhase.FAILED)
            return {"error": "boom: server exploded"}
        task.status = GisAiTaskStatus.COMPLETED
        task.set_state(GisAiPhase.COMPLETE)
        return {"response": "STATUS OK"}


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


def _make_window(tk_root, outcome="completed", status_messages=(), task_manager=None):
    manager = task_manager or TaskManager()
    inference = _StatusInference()
    engine = _StatusEngine(inference, outcome=outcome, status_messages=status_messages)
    model_manager = _FakeModelManager(inference)
    window = MainWindow(
        tk_root, task_manager=manager, model_manager=model_manager, gisai_engine=engine
    )
    window._task_timeout_s = 120
    return window, manager


def _send(window, text):
    window.task_entry.delete(0, tk.END)
    window.task_entry.insert(0, text)
    window._send_task()


def _wait_idle(window, timeout=5.0):
    deadline = time.time() + timeout
    while window._active_task and time.time() < deadline:
        window.root.update()
        time.sleep(0.005)
    assert not window._active_task


def _mk_gisai_task(status="running", state="understanding", task_id="task-1", failure_info=None):
    from giscode.gisai.types import GisAiPhase, GisAiTask, GisAiTaskStatus

    task = GisAiTask(
        task_id=task_id,
        project_id="test",
        project_path="",
        section="Assistant",
        raw_request="hi",
        state=GisAiPhase(state),
        status=GisAiTaskStatus(status),
    )
    if failure_info is not None:
        task.failure_info = failure_info
    return task


@requires_gisai
def test_activity_label_maps_authoritative_gisai_states(tk_root):
    window, _ = _make_window(tk_root)
    try:
        assert window._activity_label_for_gisai(_mk_gisai_task("running", "understanding")) == "Thinking"
        assert window._activity_label_for_gisai(_mk_gisai_task("running", "loading_capability")) == "Thinking"
        assert window._activity_label_for_gisai(_mk_gisai_task("running", "planning")) == "Planning"
        assert window._activity_label_for_gisai(_mk_gisai_task("running", "evaluating")) == "Verifying"
        assert window._activity_label_for_gisai(_mk_gisai_task("running", "executing")) == "Thinking"
        assert window._activity_label_for_gisai(_mk_gisai_task("pending", "waiting_input")) == "Waiting for input"
        assert window._activity_label_for_gisai(_mk_gisai_task("completed", "complete")) == "Completed"
        assert window._activity_label_for_gisai(_mk_gisai_task("cancelled", "cancelled")) == "Cancelled"
        assert window._activity_label_for_gisai(_mk_gisai_task("failed", "failed")) == "Error"
        assert window._activity_label_for_gisai(
            _mk_gisai_task("failed", "failed", failure_info={"reason": "execution_timeout"})
        ) == "Timeout"
        assert window._activity_label_for_gisai(_mk_gisai_task("timeout", "failed")) == "Timeout"
    finally:
        window._task_manager.cancel()


@requires_gisai
def test_live_status_text_overrides_executing_state(tk_root):
    window, _ = _make_window(tk_root)
    try:
        task = _mk_gisai_task("running", "executing")
        window._active_task = True
        window._active_gisai_task = task
        window._on_task_status("Running tests")
        assert window.phase_var.get() == "Running tests"
        assert window.vision_status_var.get() == ACTIVITY_RUNNING_TESTS
        assert window._live_activity_fresh() == "Running tests"
        window._poll_task_state()
        assert window.phase_var.get() == "Running tests"
        assert window.status_var.get() == "GisAi: running tests"
    finally:
        window._active_task = False
        window._clear_active_gisai_task()
        window._task_manager.cancel()


@requires_gisai
def test_poller_reflects_authoritative_gisai_task_when_active(tk_root):
    window, _ = _make_window(tk_root)
    try:
        window._active_task = True
        window._active_gisai_task = _mk_gisai_task("running", "understanding")
        window._poll_task_state()
        assert window.phase_var.get() == "Thinking"
        assert window.status_var.get() == "GisAi: thinking"
    finally:
        window._active_task = False
        window._clear_active_gisai_task()
        window._task_manager.cancel()


@requires_gisai
def test_coding_agent_poller_does_not_clobber_engine_driven_status(tk_root):
    from unittest.mock import MagicMock

    from giscode.agentic.loop import CodingAgentLoop
    from giscode.agentic.types import ExecutionPhase, LoopState

    window, _ = _make_window(tk_root)
    window._coding_agent = MagicMock(spec=CodingAgentLoop)
    window._coding_agent.get_current_state.return_value = LoopState(
        task_id="task-abc",
        phase=ExecutionPhase.IMPLEMENTATION,
        status="running",
        model_name="Model-X",
    )
    try:
        task = _mk_gisai_task("running", "executing")
        window._active_task = True
        window._active_gisai_task = task
        window._live_activity = "Step 1/4: Analyze the project"
        window._live_activity_at = time.time()
        window._poll_coding_agent_state()
        assert window.phase_var.get() == "Step 1/4: Analyze the project"
        assert "Implementation" not in window.phase_var.get()
        assert "task-abc" not in window.status_var.get()
        assert window.agent_phase_var.get() == "Implementation"
        assert window.agent_status_var.get() == "Running"
    finally:
        window._active_task = False
        window._clear_active_gisai_task()
        window._task_manager.cancel()


@requires_gisai
def test_active_to_completed_shows_completed_then_resets(tk_root):
    window, manager = _make_window(tk_root, outcome="completed")
    try:
        _send(window, "do the thing")
        _wait_idle(window)
        assert manager.get_active_task() is None
        assert not window._active_task
        # The "Completed" flash is held for one second of wall clock
        # (main_window.py: _terminal_state_until = now + 1.0). Under load
        # the wait above can outlast that hold and the poll loop has already
        # reset the phase, so pin the hold open to assert the flash itself;
        # the reset is still exercised below by expiring the deadline.
        window._terminal_state_until = time.time() + 60.0
        window._poll_task_state()
        assert window.phase_var.get() == "Completed"
        assert window.send_task_btn.instate(["!disabled"])
        assert window.revoke_btn.instate(["disabled"])
        assert "STATUS OK" in window.response_text.get("1.0", tk.END)

        window._terminal_state_until = time.time() - 1.0
        window._poll_task_state()
        assert window.phase_var.get() == "Idle"
        assert window.status_var.get() == "No active task"
    finally:
        manager.cancel()


@requires_gisai
def test_active_to_cancelled_shows_cancelled_and_recovers(tk_root):
    window, manager = _make_window(tk_root, outcome="cancelled")
    try:
        _send(window, "do the thing")
        _wait_idle(window)
        assert manager.get_active_task() is None
        assert window._ui_state == "STOPPED"
        assert window.phase_var.get() == "Cancelled"
        assert window.send_task_btn.instate(["!disabled"])
        assert window.revoke_btn.instate(["disabled"])
    finally:
        manager.cancel()


@requires_gisai
def test_revoke_task_cancels_and_allows_next_tasks(tk_root):
    from giscode.gisai.types import GisAiPhase, GisAiTaskStatus

    class _HangEngine(_StatusEngine):
        def __init__(self, inference):
            super().__init__(inference)
            self.hang = True
            self.cancel_requested = False

        def cancel(self):
            self.cancel_requested = True

        def execute(self, task, on_status=None):
            if self.hang:
                if on_status is not None:
                    on_status("Thinking")
                time.sleep(30)
                task.status = GisAiTaskStatus.CANCELLED
                task.set_state(GisAiPhase.CANCELLED)
                return {"error": "Operation cancelled"}
            task.status = GisAiTaskStatus.COMPLETED
            task.set_state(GisAiPhase.COMPLETE)
            return {"response": "SECOND OK"}

    window, manager = _make_window(tk_root, task_manager=TaskManager())
    inference = _StatusInference()
    engine = _HangEngine(inference)
    window._gisai_engine = engine
    try:
        _send(window, "first")
        assert window._active_task is True
        window._revoke_task()
        assert engine.cancel_requested is True
        assert window._ui_state == "STOPPED"
        assert window.phase_var.get() == "Cancelled"
        assert window.send_task_btn.instate(["!disabled"])
        assert window.revoke_btn.instate(["disabled"])
        assert window._active_gisai_task is None

        engine.hang = False
        _send(window, "second")
        _wait_idle(window)
        assert "SECOND OK" in window.response_text.get("1.0", tk.END)
        assert window.phase_var.get() == "Completed"
    finally:
        manager.cancel()


@requires_gisai
def test_active_to_error_shows_error_button_cycle(tk_root):
    window, manager = _make_window(tk_root, outcome="failed")
    try:
        _send(window, "do the thing")
        _wait_idle(window)
        assert manager.get_active_task() is None
        assert window._ui_state == "ERROR"
        assert window.phase_var.get() == "Error"
        assert window.send_task_btn.instate(["!disabled"])
        assert window.revoke_btn.instate(["disabled"])
        assert "boom: server exploded" in window.response_text.get("1.0", tk.END)
    finally:
        manager.cancel()


@requires_gisai
def test_subsequent_request_resets_state_machine(tk_root):
    window, manager = _make_window(tk_root, outcome="completed")
    try:
        _send(window, "first")
        _wait_idle(window)
        assert window.phase_var.get() == "Completed"

        _send(window, "second")
        assert window._active_task is True
        assert window.phase_var.get() == "Working"
        assert window.send_task_btn.instate(["disabled"])
        assert window.revoke_btn.instate(["!disabled"])
        _wait_idle(window)
        assert window.response_text.get("1.0", tk.END).count("STATUS OK") >= 2
        assert window.phase_var.get() == "Completed"
    finally:
        manager.cancel()


def test_waiting_for_input_result_is_shown_not_completed(tk_root):
    window, manager = _make_window(tk_root)
    try:
        window._last_outcome = "completed"
        window._dispatch_result({"status": "waiting_for_input"}, 0.1, window._task_epoch)
        assert window._last_outcome == "waiting"
        assert window.phase_var.get() == "Waiting for input"
        window._reset_task_ui(window._task_epoch)
        assert window.phase_var.get() == "Waiting for input"
    finally:
        manager.cancel()