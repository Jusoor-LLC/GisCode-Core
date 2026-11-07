"""A plan request must never fail silently.

The original defect these tests pin down lived in the hidden Planer chat box:
its ``tk.Text`` mirrored into ``_input_var`` only on ``<KeyRelease>``, so a
paste performed with the mouse (context menu, middle click, drag and drop)
inserted text with no key event and ``_on_send_message`` returned without
sending, without a reply and without an error.

Stage 2B removed that panel. The Projects tab now authors plans through a
single-line request field, and ``_on_plan_request`` reads the field directly,
so there is no mirror to go stale. These tests keep the user-visible
invariants on the surviving path: whatever is in the box is what gets sent,
an empty box says why, a failure is shown, and a busy box keeps the unsent
text.
"""
import threading

import pytest
import tkinter as tk

from giscode.gui import project_workspace as pw
from tests.conftest import requires_gisai


class _FakeController:
    """Records what the view asked to send and returns a canned answer."""

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.sent = []
        self.lock = threading.Lock()

    def send_planer_message(self, message):
        with self.lock:
            self.sent.append(message)
        if self._error is not None:
            raise self._error
        return self._result

    def has_session(self):
        return False

    def get_readiness(self):
        return {"plan_readiness": "NO_PLAN", "is_ready_for_review": False}


class _FakeStatusVar:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value

    def get(self):
        return self.value


def _view(tk_root, controller):
    """A ProjectWorkspaceView with only the attributes the plan path uses."""
    view = object.__new__(pw.ProjectWorkspaceView)
    view._parent = tk_root
    view._controller = controller
    view._input_var = tk.StringVar()
    view._status_var = _FakeStatusVar()
    view._lifecycle_var = _FakeStatusVar()
    view._plan_reply_var = tk.StringVar()
    view._plan_entry = ttk_entry(tk_root)
    view._send_thread = None
    view._send_cancel = threading.Event()
    view._plan_request_pending = False
    return view


def ttk_entry(parent):
    from tkinter import ttk

    entry = ttk.Entry(parent)
    entry.pack()
    return entry


def _type_into_plan_request(view, text):
    """Insert text the way a mouse paste does: no key event is generated."""
    view._plan_entry.delete(0, tk.END)
    view._plan_entry.insert(0, text)


def _drain(view):
    """Let the worker thread finish and run the queued after() callback."""
    if view._send_thread is not None:
        view._send_thread.join(timeout=5)
    tk_root = view._parent
    for _ in range(5):
        tk_root.update()


def test_mouse_paste_is_sent_even_without_a_key_event(tk_root):
    """The reported bug: text pasted with the mouse was never sent."""
    controller = _FakeController()
    view = _view(tk_root, controller)
    message = "Here is the task to type into the plan box: Fix the routing bug"
    _type_into_plan_request(view, message)

    # No key event was generated, so nothing could have mirrored the text.
    assert view._input_var.get() == ""

    view._on_plan_request()
    _drain(view)

    assert controller.sent == [message], "the visible text must be sent"
    assert view._plan_entry.get() == "", "the box must be cleared"


def test_send_reports_a_failure_visibly(tk_root):
    """A backend error must surface next to the plan actions, not vanish."""
    controller = _FakeController(error=RuntimeError("model exploded"))
    view = _view(tk_root, controller)
    _type_into_plan_request(view, "do the thing")

    view._on_plan_request()
    _drain(view)

    assert view._status_var.get(), "an error must be shown, not silence"
    assert "model exploded" in view._plan_reply_var.get()


def test_three_dash_line_still_produces_a_visible_outcome(tk_root):
    """A sentence followed by a line of dashes must not break the request."""
    controller = _FakeController()
    view = _view(tk_root, controller)
    message = (
        "Here is the task to type into the plan box:\n"
        "---------------------------------------------\n"
        "Fix the failing test in tests/test_routing.py"
    )
    _type_into_plan_request(view, message)

    view._on_plan_request()
    _drain(view)

    assert controller.sent == [message]
    assert view._status_var.get(), "a visible status or reply is required"


@requires_gisai
def test_three_dash_line_survives_intent_detection():
    """The dashes must not trip intent detection or command parsing."""
    from giscode.gisai.engine import GisAiEngine

    engine = GisAiEngine()
    dashed = (
        "Here is the task to type into the chat box:\n"
        "---------------------------------------------\n"
        "Fix the failing test in tests/test_routing.py"
    )
    plain = "Fix the failing test in tests/test_routing.py"

    dashed_intent = engine._detect_intent(dashed)
    plain_intent = engine._detect_intent(plain)
    assert dashed_intent["capabilities"] == plain_intent["capabilities"]

    task = engine.receive(dashed)
    assert getattr(task, "raw_request", None) is not None
    assert task.state is not None


def test_empty_box_reports_instead_of_silently_returning(tk_root):
    controller = _FakeController()
    view = _view(tk_root, controller)

    view._on_plan_request()
    _drain(view)

    assert controller.sent == []
    assert "empty" in view._status_var.get().lower()
    assert "empty" in view._plan_reply_var.get().lower()


def test_second_send_while_one_is_running_keeps_the_text(tk_root):
    """A busy request must not silently eat the typed message."""
    controller = _FakeController()
    view = _view(tk_root, controller)
    gate = threading.Event()

    def blocking(message):
        gate.wait(timeout=5)
        return None

    controller.send_planer_message = blocking
    _type_into_plan_request(view, "first message")
    view._on_plan_request()

    _type_into_plan_request(view, "second message")
    view._on_plan_request()

    assert "already" in view._status_var.get().lower()
    assert view._plan_entry.get() == "second message", (
        "the unsent message must stay in the box"
    )
    gate.set()
    _drain(view)


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "   \n  ",
        "Here is the task:\n---\nBuild a new app",
        "---\n---\n---",
    ],
)
def test_plan_request_never_silently_drops_visible_text(tk_root, text):
    controller = _FakeController()
    view = _view(tk_root, controller)
    _type_into_plan_request(view, text)

    view._on_plan_request()
    _drain(view)

    if text.strip():
        assert controller.sent == [text.strip()]
    else:
        assert view._plan_reply_var.get(), "whitespace-only input must still report"
