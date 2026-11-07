"""The send worker must never call Tk, and its result must never be lost.

Reported defect (order-dependent GUI stall): ``_send_worker`` published its
outcome with ``self._parent.after(0, ...)`` *from the worker thread*. Tkinter
marshals that call to the main thread and blocks the worker until the event
loop services it, and it raises ``RuntimeError: main thread is not in main
loop`` whenever the loop is not running. The old code caught that error and
did nothing, so an answer that had already been computed was thrown away and
the pane stayed on "Planning..." for good.

The worker now puts its outcome on a queue and the main thread renders it.
These tests pin both halves of that contract:

* the worker makes no Tk call at all (so it can never block on the loop);
* a result produced while the loop is not running is still rendered when the
  loop next gets a turn — nothing is dropped.
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from giscode.gui.project_workspace import ProjectWorkspaceView

try:
    from tests.conftest import (
        _create_tk_root,
        _safe_destroy,
        tk_available,
        tk_skip_reason,
    )
except Exception:  # pragma: no cover - environment guard
    _create_tk_root = None
    _safe_destroy = None
    tk_available = None

    def tk_skip_reason() -> str:
        return "Tk helpers unavailable"


# The shared retrying probe decides availability; a missing helper module skips
# too, so this file still collects in a tree without the conftest helpers.
TK_OK = _create_tk_root is not None and tk_available()
pytestmark = pytest.mark.skipif(not TK_OK, reason=tk_skip_reason())

ANSWER = "SPY-ANSWER: the model really answered"


class _Controller:
    """The minimum the view calls, with a controllable outcome."""

    has_active_session = False
    current_project_id = None

    def __init__(self, delay: float = 0.0, error: Exception | None = None) -> None:
        self.delay = delay
        self.error = error
        self.calls: list[str] = []

    def send_planer_message(self, message: str):
        self.calls.append(message)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(success=True, assistant_message=ANSWER)

    def list_projects(self):
        return []

    def load_chat_history(self):
        return []

    def get_readiness(self):
        return {"plan_readiness": "NO_PLAN", "is_ready_for_review": False}


@pytest.fixture
def view_factory():
    created: list[tk.Tk] = []

    def make(controller) -> tuple[tk.Tk, ProjectWorkspaceView]:
        root = _create_tk_root()
        root.withdraw()
        created.append(root)
        return root, ProjectWorkspaceView(root, controller)

    yield make
    for root in created:
        _safe_destroy(root)


def _pump(root: tk.Tk, ms: int = 250) -> None:
    """Run the real event loop for a bounded time."""
    root.after(ms, root.quit)
    root.mainloop()


def _pump_until(root: tk.Tk, predicate, timeout: float = 3.0) -> None:
    """Pump the loop in slices until *predicate* holds or the budget is gone.

    The assertions stay strict; only the waiting is tolerant of a loaded
    machine, which a single fixed-length pump is not.
    """
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        _pump(root, 50)


def _send(view: ProjectWorkspaceView, text: str = "do the thing") -> None:
    view._input_var.set(text)
    view._on_send_message()


# ------------------------------------------------------- nothing is dropped


def test_result_produced_while_the_loop_is_down_is_still_rendered(view_factory):
    """The old bug: the worker's post raised, the answer vanished forever."""
    controller = _Controller()
    root, view = view_factory(controller)

    _send(view)

    # Wait for the worker without running the event loop at all: this is the
    # exact state in which ``after()`` from the worker used to raise.
    thread = view._send_thread
    assert thread is not None
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    # Nothing rendered yet (the loop has not run), and the answer is not lost.
    assert view._status_var.get() == "Planning..."

    _pump_until(root, lambda: ANSWER in view._plan_reply_var.get())

    assert ANSWER in view._plan_reply_var.get()
    assert view._status_var.get() == "Planer responded"
    assert view._send_thread is None


def test_result_is_rendered_while_the_loop_is_running(view_factory):
    """The normal case: a slow send is picked up as soon as it completes."""
    controller = _Controller(delay=0.15)
    root, view = view_factory(controller)

    _send(view)
    _pump_until(root, lambda: ANSWER in view._plan_reply_var.get())

    assert ANSWER in view._plan_reply_var.get()
    assert controller.calls == ["do the thing"]


# ------------------------------------------- the queue is built by the Tk thread


def test_outcome_queue_exists_before_the_worker_starts(view_factory):
    """The worker only posts; the queue must already exist when it is started.

    ``_result_queue()`` builds the queue lazily, so without an explicit call on
    the Tk thread the first allocation happens on the worker. The reported
    failure was that allocation collecting a tkinter Variable on the worker
    thread, whose ``__del__`` then called Tcl off the main thread and parked
    the worker, so a computed answer was never delivered.

    Honest limit: that wedge depends on allocator and GC state (a Variable
    being finalised at that exact moment), so it cannot be forced
    deterministically in a test. What is asserted here is the ordering the fix
    guarantees — the queue exists, built by the main thread, before the worker
    thread runs — not the wedge itself.
    """
    controller = _Controller()
    root, view = view_factory(controller)

    worker_started = threading.Event()
    release = threading.Event()
    constructed_on: list[str] = []
    real_queue = queue.Queue
    real_worker = view._send_worker

    class RecordingQueue(real_queue):
        def __init__(self, *args, **kwargs):
            constructed_on.append(threading.current_thread().name)
            super().__init__(*args, **kwargs)

    def blocked_worker(message, seq=0):
        worker_started.set()
        release.wait(timeout=5)
        real_worker(message, seq)

    view._send_worker = blocked_worker
    with patch.object(queue, "Queue", RecordingQueue):
        try:
            _send(view)
            assert worker_started.wait(timeout=5), "the worker never started"
            # The worker is parked on its first line and cannot have built it.
            assert getattr(view, "_send_results", None) is not None, (
                "the outcome queue does not exist yet: it is still created "
                "lazily, first on the worker thread"
            )
        finally:
            release.set()

    assert constructed_on, "no outcome queue was created at all"
    assert all(name == "MainThread" for name in constructed_on), (
        f"a worker thread built a queue: {constructed_on}"
    )
    thread = view._send_thread
    if thread is not None:
        thread.join(timeout=5)
    _pump(root, 100)


# ------------------------------------------------- the worker never uses Tk


def test_worker_makes_no_tk_call_from_its_own_thread(view_factory):
    """A worker-side Tk call is the deadlock; it must not exist."""
    controller = _Controller(delay=0.15)
    root, view = view_factory(controller)

    calls: list[str] = []
    real_after = view._parent.after

    def recording_after(ms, callback=None, *args):
        calls.append(threading.current_thread().name)
        return real_after(ms, callback, *args)

    view._parent.after = recording_after
    try:
        _send(view)
        _pump_until(
            root,
            lambda: view._send_thread is None or not view._send_thread.is_alive(),
        )
        _pump(root, 100)          # let the queued result be rendered
    finally:
        view._parent.after = real_after

    assert calls, "the result dispatch must still go through after()"
    assert set(calls) == {"MainThread"}, (
        f"a worker thread called Tk: {sorted(set(calls))}"
    )


# --------------------------------------------------- honest failure reporting


def test_failed_send_is_recorded_and_a_dialog_cannot_block_a_hidden_view(view_factory):
    """The reason must reach the reply line; a hidden window shows no modal."""
    controller = _Controller(error=RuntimeError("model exploded"))
    root, view = view_factory(controller)

    raised: list[tuple] = []

    def record_dialog(*args, **kwargs):
        raised.append((args, kwargs))

    with patch("giscode.gui.project_workspace.messagebox.showerror", record_dialog):
        with patch.object(view, "_controller_has_session", return_value=False):
            _send(view)
            _pump_until(
                root, lambda: "model exploded" in view._plan_reply_var.get()
            )

    display = view._plan_reply_var.get()
    assert "model exploded" in display, "the failure must be visible in the reply line"
    assert "model exploded" in view._status_var.get()
    assert raised == [], "a withdrawn view must not raise a modal dialog"
    assert view._send_thread is None


def test_view_accepts_another_send_after_a_failure(view_factory):
    """A failure must not wedge the send path."""
    controller = _Controller(error=RuntimeError("first attempt failed"))
    root, view = view_factory(controller)

    with patch.object(view, "_controller_has_session", return_value=False):
        _send(view, "first")
        _pump_until(root, lambda: view._send_thread is None)

        controller.error = None
        _send(view, "second")
        _pump_until(root, lambda: ANSWER in view._plan_reply_var.get())

    assert controller.calls == ["first", "second"]
    assert ANSWER in view._plan_reply_var.get()
    assert view._send_thread is None


# ------------------------------------- one send's answer cannot answer another


class _EchoController(_Controller):
    """Answers with a token that names the message it was given."""

    def send_planer_message(self, message: str):
        self.calls.append(message)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(success=True, assistant_message=f"ANSWER<{message}>")


def test_an_abandoned_answer_never_answers_a_later_message(view_factory):
    """A queued answer that was superseded must not be shown as the next one's.

    The old poller took whatever sat at the head of the queue and cleared
    ``_send_thread`` unconditionally, so an earlier answer could be rendered
    against a newer message and the newer answer was then never polled.
    """
    controller = _EchoController()
    root, view = view_factory(controller)

    _send(view, "first")
    first = view._send_thread
    assert first is not None
    first.join(timeout=5.0)          # answer is queued; the loop has not run
    assert not first.is_alive()

    _send(view, "second")            # allowed: the first worker has finished
    view._send_thread.join(timeout=5.0)

    _pump_until(root, lambda: "ANSWER<second>" in view._plan_reply_var.get())

    display = view._plan_reply_var.get()
    assert "ANSWER<second>" in display
    assert "ANSWER<first>" not in display, "the abandoned answer was shown"
    assert display.count("ANSWER<second>") == 1, "delivered more than once"
    assert view._send_thread is None


def test_outcome_queued_between_the_read_and_the_liveness_check_is_not_lost(
    view_factory,
):
    """The worker puts its outcome just before its thread ends.

    Reading the queue and then checking liveness leaves a window in which the
    outcome lands in between; the poller concluded "empty and finished" and
    dropped the answer for good. This forces exactly that interleaving by
    making the first read see an empty queue while the real item stays queued.
    """
    controller = _Controller()
    root, view = view_factory(controller)

    _send(view)
    view._send_thread.join(timeout=5.0)   # the outcome is now queued

    results = view._result_queue()
    real_get = results.get_nowait
    reads = {"count": 0}

    def get_nowait():
        reads["count"] += 1
        if reads["count"] == 1:
            # The read that happened just before the worker's put.
            raise queue.Empty
        return real_get()

    results.get_nowait = get_nowait

    _pump_until(root, lambda: ANSWER in view._plan_reply_var.get())

    assert reads["count"] >= 2, "the poller did not re-read after the worker ended"
    assert ANSWER in view._plan_reply_var.get()
    assert view._status_var.get() == "Planer responded"
    assert view._send_thread is None


def test_cancel_join_does_not_need_the_event_loop(view_factory):
    """Stop used to risk the documented join deadlock; now the join is safe."""
    controller = _Controller(delay=1.0)
    root, view = view_factory(controller)

    _send(view)
    started = time.monotonic()
    view._cancel_send()          # joins the worker without pumping the loop
    elapsed = time.monotonic() - started

    thread = view._send_thread
    assert thread is None or not thread.is_alive(), "worker survived Stop"
    assert elapsed < 2.5, f"Stop took {elapsed:.1f}s"
