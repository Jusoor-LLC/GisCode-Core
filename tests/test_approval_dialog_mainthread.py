import threading
import time
import tkinter as tk
from types import SimpleNamespace
from unittest import mock

from giscode.gui.main_window import MainWindow


class _FakeModelManager:
    def __init__(self):
        self.loaded = None
        self._settings = {}

    def auto_register_discovered(self):
        pass

    def get_registry(self):
        return SimpleNamespace(list_all=lambda: [SimpleNamespace(name="Model-A")])

    def get_current_model(self):
        return self.loaded

    def load_model(self, name):
        self.loaded = SimpleNamespace(name=name)
        return None

    def unload_model(self, name):
        self.loaded = None
        return None


def _make_window(tk_root):
    return MainWindow(tk_root, model_manager=_FakeModelManager(), gisai_engine=None)


def _pump(root, stop_evt, max_s=5.0):
    # Pump the Tk main loop (fires the approval poller) until stop_evt or timeout.
    deadline = time.time() + max_s
    while not stop_evt.is_set() and time.time() < deadline:
        root.update()
        time.sleep(0.005)


def test_apply_returns_true(tk_root):
    mw = _make_window(tk_root)
    finish = threading.Event()
    results = {}

    def worker():
        results["approved"] = mw._approve_edit("calc.py", "--- a\n+++ b\n")
        finish.set()

    t = threading.Thread(target=worker, daemon=True)
    with mock.patch("giscode.gui.main_window.messagebox.askyesno", return_value=True):
        t.start()
        _pump(tk_root, finish)
    t.join(2)
    assert results["approved"] is True


def test_reject_returns_false(tk_root):
    mw = _make_window(tk_root)
    finish = threading.Event()
    results = {}

    def worker():
        results["approved"] = mw._approve_edit("calc.py", "--- a\n+++ b\n")
        finish.set()

    t = threading.Thread(target=worker, daemon=True)
    with mock.patch("giscode.gui.main_window.messagebox.askyesno", return_value=False):
        t.start()
        _pump(tk_root, finish)
    t.join(2)
    assert results["approved"] is False


def test_timeout_returns_false(tk_root):
    mw = _make_window(tk_root)
    mw._APPROVAL_TIMEOUT_S = 0.2
    finish = threading.Event()
    results = {}

    def worker():
        results["approved"] = mw._approve_edit("calc.py", "--- a\n+++ b\n")
        finish.set()

    t = threading.Thread(target=worker, daemon=True)
    # Dialog never fires -> worker must time out to Reject.
    with mock.patch("giscode.gui.main_window.messagebox.askyesno", side_effect=AssertionError("no dialog")):
        t.start()
        _pump(tk_root, finish)
    t.join(2)
    assert results["approved"] is False


def test_two_sequential_requests_do_not_interleave(tk_root):
    mw = _make_window(tk_root)
    order = []
    r1, r2 = {}, {}
    f1, f2 = threading.Event(), threading.Event()

    def worker1():
        r1["approved"] = mw._approve_edit("a.py", "--- a\n+++ b\n")
        order.append(1)
        f1.set()

    def worker2():
        r2["approved"] = mw._approve_edit("b.py", "--- a\n+++ b\n")
        order.append(2)
        f2.set()

    active = {"n": 0}

    def fake_askyesno(*a, **k):
        active["n"] += 1
        assert active["n"] == 1, "interleaved dialogs"
        time.sleep(0.02)
        active["n"] -= 1
        return True

    with mock.patch("giscode.gui.main_window.messagebox.askyesno", side_effect=fake_askyesno):
        t1 = threading.Thread(target=worker1, daemon=True)
        t1.start()
        _pump(tk_root, f1)
        t1.join(2)
        t2 = threading.Thread(target=worker2, daemon=True)
        t2.start()
        _pump(tk_root, f2)
        t2.join(2)
    assert r1["approved"] is True
    assert r2["approved"] is True
    assert order == [1, 2]


def test_window_close_rejects_pending(tk_root):
    mw = _make_window(tk_root)
    mw._APPROVAL_TIMEOUT_S = 2.0
    finish = threading.Event()
    results = {}

    def worker():
        results["approved"] = mw._approve_edit("x.py", "--- a\n+++ b\n")
        finish.set()

    def fake_askyesno(*a, **k):
        # Simulate the user closing the window mid-dialog.
        mw._on_window_close()
        return True

    t = threading.Thread(target=worker, daemon=True)
    with mock.patch("giscode.gui.main_window.messagebox.askyesno", side_effect=fake_askyesno):
        t.start()
        _pump(tk_root, finish)
    t.join(2)
    assert results["approved"] is False


def test_close_during_dialog_voids_answer(tk_root):
    """A close while the dialog is open rejects the edit, without a race.

    Drives the drainer itself, so no thread timing is involved: the dialog
    closes the window and still reports True. Before the fix the drainer
    stored that True, and whether the waiting worker saw it or saw _closing
    first depended on its 50 ms poll phase, which is why
    test_window_close_rejects_pending failed only on a loaded machine.
    """
    mw = _make_window(tk_root)
    item = {"msg": "Proposed edit for x.py", "result": False,
            "done": threading.Event(), "shown": False}
    mw._pending_approvals.append(item)

    def fake_askyesno(*a, **k):
        mw._on_window_close()
        return True

    with mock.patch("giscode.gui.main_window.messagebox.askyesno", side_effect=fake_askyesno):
        mw._drain_pending_approvals()

    assert item["shown"] is True
    assert item["done"].is_set()
    assert item["result"] is False


class _RecordingEngine:
    """Minimal engine stand-in: records what the GUI reports about dialogs."""

    def __init__(self):
        self.decisions = []
        self.callback = None

    def set_edit_approval_callback(self, callback):
        self.callback = callback

    def get_active_task(self):
        return None

    def note_edit_dialog(self, path, *, shown, accepted):
        self.decisions.append((str(path), shown, accepted))


def test_approve_edit_reports_a_dialog_it_really_showed(tk_root):
    engine = _RecordingEngine()
    mw = MainWindow(tk_root, model_manager=_FakeModelManager(), gisai_engine=engine)
    finish = threading.Event()
    results = {}

    def worker():
        results["approved"] = mw._approve_edit("calc.py", "--- a\n+++ b\n")
        finish.set()

    t = threading.Thread(target=worker, daemon=True)
    with mock.patch("giscode.gui.main_window.messagebox.askyesno", return_value=False):
        t.start()
        _pump(tk_root, finish)
    t.join(2)
    assert results["approved"] is False
    assert engine.decisions == [("calc.py", True, False)]


def test_approve_edit_does_not_report_a_decision_nobody_saw(tk_root):
    engine = _RecordingEngine()
    mw = MainWindow(tk_root, model_manager=_FakeModelManager(), gisai_engine=engine)
    mw._APPROVAL_TIMEOUT_S = 0.2
    results = {}

    def worker():
        results["approved"] = mw._approve_edit("calc.py", "--- a\n+++ b\n")

    t = threading.Thread(target=worker, daemon=True)
    # No Tk pump: the dialog is never drained, so it never reaches a person.
    t.start()
    t.join(3)
    assert results["approved"] is False
    assert engine.decisions == [("calc.py", False, False)]
