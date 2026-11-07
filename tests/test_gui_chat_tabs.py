import tkinter as tk

from giscode.task_control.manager import TaskManager
from giscode.gui.main_window import MainWindow
from tests.conftest import requires_gisai


class _FakeInference:
    def get_health(self):
        return {"model_loaded": True}

    def cancel(self):
        pass

    def chat_generate(self, request):
        from giscode.inference.runtime import ChatResponse
        return ChatResponse(text="SYSTEM OK")

    def generate(self, request):
        from giscode.inference.runtime import GenerationResponse
        return GenerationResponse(text="SYSTEM OK")


class _FakeModelManager:
    def __init__(self):
        self._inference = _FakeInference()
        self._settings = {}

    def get_current_model(self):
        return None

    def auto_register_discovered(self):
        pass

    def get_registry(self):
        return _FakeRegistry()

    def get_health(self):
        return {"model_loaded": True}


class _FakeRegistry:
    def list_all(self):
        return []


def _make_window(tk_root):
    from tests.conftest import _FakeGisAiEngine
    manager = TaskManager()
    inference = _FakeInference()
    model_manager = _FakeModelManager()
    model_manager._inference = inference
    gisai_engine = _FakeGisAiEngine(inference)
    window = MainWindow(tk_root, task_manager=manager, model_manager=model_manager, gisai_engine=gisai_engine)
    return window


def test_initial_chat_exists(tk_root):
    window = _make_window(tk_root)
    try:
        assert len(window._chat_sessions) == 1
        assert window._current_chat_index == 0
    finally:
        window._task_manager.cancel()


def test_new_chat_creates_tab_and_selects_it(tk_root):
    window = _make_window(tk_root)
    try:
        window._new_chat()
        assert len(window._chat_sessions) == 2
        assert window._current_chat_index == 1
        assert window._chat_sessions[1]["title"] == "Chat 2"
    finally:
        window._task_manager.cancel()


def test_switch_chat_preserves_independent_histories(tk_root):
    window = _make_window(tk_root)
    try:
        window._append_response("chat one content\n")
        window._new_chat()
        assert len(window._chat_sessions) == 2
        assert window._chat_sessions[0]["text"].strip() == "chat one content"
        window._append_response("chat two content\n")
        assert window._chat_sessions[1]["text"].strip().endswith("chat two content")

        window._switch_chat(0)
        assert window._current_chat_index == 0
        text = window.response_text.get("1.0", tk.END)
        assert "chat one content" in text
        assert "chat two content" not in text

        window._switch_chat(1)
        assert window._current_chat_index == 1
        text = window.response_text.get("1.0", tk.END)
        assert "chat two content" in text
        assert "chat one content" not in text
    finally:
        window._task_manager.cancel()


def test_switch_chat_preserves_draft_input(tk_root):
    window = _make_window(tk_root)
    try:
        window.task_entry.delete(0, tk.END)
        window.task_entry.insert(0, "draft one")
        window._new_chat()
        assert window._chat_sessions[0]["draft"] == "draft one"
        assert window.task_entry.get() == ""
        window.task_entry.insert(0, "draft two")
        window._switch_chat(0)
        assert window.task_entry.get() == "draft one"
        window._switch_chat(1)
        assert window.task_entry.get() == "draft two"
    finally:
        window._task_manager.cancel()


def test_close_chat_keeps_at_least_one(tk_root):
    window = _make_window(tk_root)
    try:
        assert len(window._chat_sessions) == 1
        window._close_chat(0)
        assert len(window._chat_sessions) == 1

        window._new_chat()
        window._new_chat()
        assert len(window._chat_sessions) == 3
        window._close_chat(0)
        assert len(window._chat_sessions) == 2
        window._close_chat(0)
        assert len(window._chat_sessions) == 1
        assert 0 <= window._current_chat_index < len(window._chat_sessions)
    finally:
        window._task_manager.cancel()


def test_close_active_chat_switches_to_neighbor(tk_root):
    window = _make_window(tk_root)
    try:
        window._append_response("alpha\n")
        window._new_chat()
        window._append_response("beta\n")
        window._new_chat()
        window._append_response("gamma\n")
        assert window._current_chat_index == 2

        window._close_chat(2)
        assert len(window._chat_sessions) == 2
        assert 0 <= window._current_chat_index < 2
        text = window.response_text.get("1.0", tk.END)
        assert "beta" in text or "alpha" in text
    finally:
        window._task_manager.cancel()


def test_tab_bar_renders_buttons(tk_root):
    window = _make_window(tk_root)
    try:
        children = list(window._chat_tabs_frame.winfo_children())
        assert children
    finally:
        window._task_manager.cancel()


@requires_gisai
def test_multi_chat_send_routes_to_active_tab(tk_root):
    import time
    window = _make_window(tk_root)
    try:
        window.task_entry.delete(0, tk.END)
        window.task_entry.insert(0, "hello")
        window._send_task()
        deadline = 5.0
        start = time.time()
        while window._active_task and time.time() - start < deadline:
            window.root.update()
            time.sleep(0.005)
        assert not window._active_task
        assert "SYSTEM OK" in window._chat_sessions[0]["text"]

        window._new_chat()
        assert "SYSTEM OK" not in window._chat_sessions[1]["text"]
    finally:
        window._task_manager.cancel()