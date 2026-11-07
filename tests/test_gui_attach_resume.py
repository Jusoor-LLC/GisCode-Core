import os
import threading
import time
import tkinter as tk
from types import SimpleNamespace

from giscode.gui.main_window import MainWindow
from giscode.task_control.manager import TaskManager


class _RecordingEngine:
    def __init__(self):
        self.received = []
        self.executed = 0
        self._active_task = None

    def receive(self, text, attachments=None, project_context=None):
        self.received.append((text, list(attachments or [])))
        task = SimpleNamespace(
            task_id=f"gisai-{len(self.received)}",
            attachments=list(attachments or []),
            status=None,
            state=None,
        )
        self._active_task = task
        return task

    def get_active_task(self):
        # MainWindow._poll_gisai_state() calls this during __init__ and on a
        # recurring after() loop, so the double must model the real engine's
        # contract: return the current task or None.
        return self._active_task

    def execute(self, task, on_status=None):
        self.executed += 1
        paths = [att.path for att in getattr(task, "attachments", []) or []]
        if paths:
            return {"vision_result": f"analysis-of-{os.path.basename(paths[-1])}", "model_used": "test"}
        return {"vision_result": "none"}


def _make_window(root, engine):
    return MainWindow(root, task_manager=TaskManager(), gisai_engine=engine)


def _wait_idle(window, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while window._active_task and time.time() < deadline:
        window.root.update()
        time.sleep(0.005)
    assert not window._active_task


def _create_waiting_task(window, text="analyze this screenshot"):
    window.task_entry.delete(0, tk.END)
    window.task_entry.insert(0, text)
    return text


def test_image_attach_resumes_waiting_task(tk_root, tmp_path, monkeypatch):
    from giscode.gui import main_window as mw_module

    image = tmp_path / "screen.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(mw_module.filedialog, "askopenfilename", lambda **kw: str(image))

    engine = _RecordingEngine()
    reached_execute = threading.Event()
    release_execute = threading.Event()
    real_execute = engine.execute

    def gated_execute(task, on_status=None):
        reached_execute.set()
        assert release_execute.wait(timeout=5.0), "test did not release the worker thread"
        return real_execute(task, on_status=on_status)

    engine.execute = gated_execute
    window = _make_window(tk_root, engine)
    try:
        window._current_chat_index = 0
        text = _create_waiting_task(window)
        window._attach_image()

        assert window.task_entry.get() == text
        assert str(image) in window._chat_sessions[window._current_chat_index]["images"]

        window._model_manager = SimpleNamespace(get_health=lambda: {"model_loaded": True})
        window._send_task()

        assert reached_execute.wait(timeout=5.0)
        assert len(engine.received) == 1
        assert window._current_chat_index in range(len(window._chat_sessions))
        assert str(image) in window._chat_sessions[window._current_chat_index]["images"]

        release_execute.set()
        _wait_idle(window)

        assert engine.executed == 1
        assert len(engine.received) == 1
        received_text, attachments = engine.received[0]
        assert received_text == text
        assert len(attachments) == 1
        assert attachments[0].path == str(image)
        assert "analysis-of-screen.png" in window.response_text.get("1.0", tk.END)
    finally:
        release_execute.set()
        window._task_manager.cancel()


def test_image_attach_keeps_running_ui_state(tk_root, tmp_path, monkeypatch):
    from giscode.gui import main_window as mw_module

    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpeg")
    monkeypatch.setattr(mw_module.filedialog, "askopenfilename", lambda **kw: str(image))

    engine = _RecordingEngine()
    window = _make_window(tk_root, engine)
    try:
        window._current_chat_index = 0
        window._current_task_id = None
        window._attach_image()

        assert engine.executed == 0
        assert engine.received == []
        assert str(image) in window._chat_sessions[0]["images"]
        assert "[Attached image: photo.jpg]" in window.response_text.get("1.0", tk.END)
    finally:
        window._task_manager.cancel()