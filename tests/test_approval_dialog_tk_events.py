"""The Tk main thread must keep processing events while an approval waits.

The approval callback enqueues a request and blocks the worker; a main-thread
poller shows the dialog. This proves a root.after timer keeps firing during
the wait (the UI is not wedged), and that Apply, Reject, and window close
all resolve the pending approval.
"""

import threading
import time
import unittest

import giscode.gui.main_window as mw_mod
from giscode.gui.main_window import MainWindow


class TestApprovalKeepsTkAlive(unittest.TestCase):
    def setUp(self):
        import tkinter as tk

        # Use the suite's retrying root creator: creating Tcl interpreters
        # repeatedly in one process intermittently fails with "can't find a
        # usable tk.tcl" (see tests/conftest.py), and a bare tk.Tk() here
        # turned that documented environment flake into a test failure.
        from tests.conftest import _create_tk_root

        self.root = _create_tk_root()
        self.root.withdraw()
        self.mw = MainWindow.__new__(MainWindow)
        self.mw.root = self.root
        self.mw._pending_approvals = []
        self.mw._closing = False
        self.mw._APPROVAL_TIMEOUT_S = 600
        self.mw._approval_poll_loop()
        self._timers = []

    def tearDown(self):
        self.mw._closing = True
        for tid in self._timers:
            try:
                self.root.after_cancel(tid)
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _askyesno(self, result):
        calls = []

        def fake(title, msg, parent=None):
            calls.append(msg)
            return result

        return fake, calls

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.01)

    def test_timer_fires_while_approval_pending(self):
        ticks = []

        def ticker():
            ticks.append(time.time())
            if len(ticks) < 20:
                self._timers.append(self.root.after(50, ticker))

        self._timers.append(self.root.after(50, ticker))
        fake, calls = self._askyesno(True)
        orig = mw_mod.messagebox.askyesno
        mw_mod.messagebox.askyesno = fake
        try:
            result = {}

            def worker():
                result["ok"] = self.mw._approve_edit("p.py", "diff")

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            # Keep the Tk event loop alive while the worker waits.
            end = time.time() + 1.5
            while time.time() < end and not calls:
                self.root.update()
                time.sleep(0.01)
            t.join(timeout=3.0)
            self._pump(0.2)
        finally:
            mw_mod.messagebox.askyesno = orig
        self.assertTrue(calls, "dialog never shown")
        self.assertTrue(result.get("ok"))
        self.assertGreaterEqual(len(ticks), 2, "Tk timer did not fire during approval wait")

    def test_apply_returns_true(self):
        fake, calls = self._askyesno(True)
        orig = mw_mod.messagebox.askyesno
        mw_mod.messagebox.askyesno = fake
        try:
            result = {}

            def worker():
                result["ok"] = self.mw._approve_edit("p.py", "d")

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            end = time.time() + 1.5
            while time.time() < end and not calls:
                self.root.update()
                time.sleep(0.01)
            t.join(timeout=3.0)
        finally:
            mw_mod.messagebox.askyesno = orig
        self.assertTrue(result.get("ok"))

    def test_reject_returns_false(self):
        fake, calls = self._askyesno(False)
        orig = mw_mod.messagebox.askyesno
        mw_mod.messagebox.askyesno = fake
        try:
            result = {}

            def worker():
                result["ok"] = self.mw._approve_edit("p.py", "d")

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            end = time.time() + 1.5
            while time.time() < end and not calls:
                self.root.update()
                time.sleep(0.01)
            t.join(timeout=3.0)
        finally:
            mw_mod.messagebox.askyesno = orig
        self.assertFalse(result.get("ok"))

    def test_window_close_resolves_pending_reject(self):
        result = {}

        def worker():
            result["ok"] = self.mw._approve_edit("p.py", "d")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        end = time.time() + 0.5
        while time.time() < end and not self.mw._pending_approvals:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(self.mw._pending_approvals, "approval never queued")
        # Simulate close without destroying root (destroy breaks later tests).
        self.mw._closing = True
        while self.mw._pending_approvals:
            item = self.mw._pending_approvals.pop(0)
            item["result"] = False
            item["done"].set()
        t.join(timeout=2.0)
        self.assertFalse(result.get("ok"))

    def test_stop_rejects_via_closing_flag(self):
        result = {}

        def worker():
            result["ok"] = self.mw._approve_edit("p.py", "d")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        end = time.time() + 0.5
        while time.time() < end and not self.mw._pending_approvals:
            self.root.update()
            time.sleep(0.01)
        self.mw._closing = True
        t.join(timeout=2.0)
        self.assertFalse(result.get("ok"))


if __name__ == "__main__":
    unittest.main()
