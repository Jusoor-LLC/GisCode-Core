import time

import pytest
import tkinter as tk

from giscode.inference.runtime import ChatResponse, GenerationResponse
from giscode.task_control.manager import TaskManager
from giscode.gui.main_window import MainWindow
from tests.conftest import requires_gisai


class _FakeInference:
    def __init__(self, reply="SYSTEM OK", error=None, hang=False):
        self.reply = reply
        self.error = error
        self.hang = hang
        self.cancelled = False
        self.chat_calls = 0
        self.last_messages = None

    def get_health(self):
        return {"model_loaded": True}

    def cancel(self):
        self.cancelled = True

    def _respond(self):
        if self.hang:
            while not self.cancelled:
                time.sleep(0.002)
            raise RuntimeError("cancelled")
        if self.error:
            raise RuntimeError(self.error)
        return self.reply

    def chat_generate(self, request):
        self.chat_calls += 1
        self.last_messages = list(request.messages)
        return ChatResponse(text=self._respond())

    def generate(self, request):
        self.chat_calls += 1
        return GenerationResponse(text=self._respond())


class _FakeRegistry:
    def list_all(self):
        return []


class _FakeModelManager:
    def __init__(self, inference):
        self._inference = inference
        self._settings = {}

    def get_current_model(self):
        return None

    def auto_register_discovered(self):
        pass

    def get_registry(self):
        return _FakeRegistry()

    def get_health(self):
        return {"model_loaded": True}


def _make_window(root, inference=None, task_manager=None, ctx_store=None):
    from tests.conftest import _FakeGisAiEngine
    task_manager = task_manager or TaskManager()
    inference = inference or _FakeInference()
    model_manager = _FakeModelManager(inference)
    gisai_engine = _FakeGisAiEngine(inference)
    window = MainWindow(root, task_manager=task_manager, model_manager=model_manager, gisai_engine=gisai_engine)
    if ctx_store is not None:
        window._ctx_store = ctx_store
    return window


def _send(window: MainWindow, text: str) -> None:
    window.task_entry.delete(0, tk.END)
    window.task_entry.insert(0, text)
    window._send_task()


def _wait_idle(window: MainWindow, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while window._active_task and time.time() < deadline:
        window.root.update()
        time.sleep(0.005)


def test_ui_state_button_rules(tk_root):
    window = _make_window(tk_root)
    try:
        window._set_ui_state("WORKING")
        assert window._ui_state == "WORKING"
        assert window.send_task_btn.instate(["disabled"])
        assert window.revoke_btn.instate(["!disabled"])
        assert window.phase_var.get() == "Working"

        window._set_ui_state("STOPPING")
        assert window.send_task_btn.instate(["disabled"])
        assert window.revoke_btn.instate(["disabled"])
        assert window.phase_var.get() == "Stopping"

        window._set_ui_state("STOPPED")
        assert window.send_task_btn.instate(["!disabled"])
        assert window.revoke_btn.instate(["disabled"])
        assert window.phase_var.get() == "Cancelled"

        window._set_ui_state("IDLE")
        assert window.send_task_btn.instate(["!disabled"])
        assert window.revoke_btn.instate(["disabled"])
        assert window.phase_var.get() == "Idle"
    finally:
        window._task_manager.cancel()


def test_stop_while_working_restores_send_and_shows_cancelled(tk_root):
    manager = TaskManager()
    inference = _FakeInference(hang=True)
    window = _make_window(tk_root, inference=inference, task_manager=manager)
    try:
        _send(window, "Hello GisCode")
        assert window._active_task is True
        assert window.send_task_btn.instate(["disabled"])

        window._revoke_task()

        assert inference.cancelled is True
        assert window._active_task is False
        assert window.send_task_btn.instate(["!disabled"])
        assert window.revoke_btn.instate(["disabled"])
        assert window.phase_var.get() == "Cancelled"
        assert "Stop requested" in window.response_text.get("1.0", tk.END)
        assert window._ui_state == "STOPPED"
    finally:
        inference.cancel()
        manager.cancel()


def test_cancel_before_switch_releases_busy(tk_root):
    manager = TaskManager()
    inference = _FakeInference(hang=True)
    window = _make_window(tk_root, inference=inference, task_manager=manager)
    try:
        _send(window, "Hello GisCode")
        assert manager.get_active_task() is not None

        window._cancel_before_switch()

        assert inference.cancelled is True
        assert window._active_task is False
        assert "cancelled before switching" in window.response_text.get("1.0", tk.END)
        assert window.phase_var.get() == "Switching project"
    finally:
        inference.cancel()
        manager.cancel()


def test_project_switch_selects_context_and_datapoints(tk_root, tmp_path, monkeypatch):
    from giscode.projects.context import ProjectContextStore

    manager = TaskManager()
    inference = _FakeInference()
    window = _make_window(tk_root, inference=inference, task_manager=manager)
    ctx_store = ProjectContextStore(tmp_path / "session")
    window._ctx_store = ctx_store
    project_dir = tmp_path / "WebApp"
    project_dir.mkdir()
    try:
        window._select_project_path(str(project_dir))

        assert window._active_project_context is not None
        assert window._active_project_context.root_path == str(project_dir.resolve())
        assert window.current_path_var.get() == str(project_dir.resolve())
        assert window.current_name_var.get() == "WebApp"
        assert "WebApp" in window.project_path_var.get()
        assert text_file_is_expected(window, "Project opened")

        window._enter_active_section("Backend")
        assert window._active_project_context.active_section == "Backend"
        window._back_section()
        assert window._active_project_context.active_section == "Overview"

        saved = ctx_store.load_all()
        assert window._active_project_context.project_id in saved
    finally:
        manager.cancel()


def text_file_is_expected(window, marker):
    return marker in window.response_text.get("1.0", "end")


@requires_gisai
def test_system_prompt_includes_project_context_not_gis(tk_root, tmp_path):
    from giscode.projects.context import ProjectContextStore

    inference = _FakeInference()
    window = _make_window(tk_root, inference=inference)
    window._ctx_store = ProjectContextStore(tmp_path / "session")
    project_dir = tmp_path / "WebApp"
    project_dir.mkdir()
    try:
        window._select_project_path(str(project_dir))
        if hasattr(window, "_gisai_engine") and window._gisai_engine is not None:
            window._gisai_engine._project_context = window._active_project_context
        _send(window, "inspect the code")
        _wait_idle(window)

        assert inference.chat_calls == 1
        system_msg = next(
            m for m in inference.last_messages if m.role == "system"
        )
        text = system_msg.content
        assert str(project_dir.resolve()) in text
        assert "SOFTWARE DEVELOPMENT PROJECT" in text
        assert "NOT as GIS/geospatial data" in text
        assert "WebApp" in text
    finally:
        window._task_manager.cancel()


@requires_gisai
def test_send_works_again_after_stopped(tk_root):
    manager = TaskManager()
    inference = _FakeInference(hang=True)
    window = _make_window(tk_root, inference=inference, task_manager=manager)
    try:
        _send(window, "First")
        window._revoke_task()
        assert window._ui_state == "STOPPED"

        inference.hang = False
        _send(window, "Second")
        assert inference.chat_calls == 2
        _wait_idle(window)
        assert window._active_task is False
        assert window.send_task_btn.instate(["!disabled"])
        assert window.phase_var.get() == "Completed"
    finally:
        inference.cancel()
        manager.cancel()