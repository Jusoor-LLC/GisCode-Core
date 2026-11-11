"""Queue host behaviour shared by the Core and Pro main windows.

Each edition keeps its own single-task execution path. This mixin owns the
queue mechanics and delegates execution to ``_queue_execute_item``, which the
edition implements. Nothing here imports GisAI, Pro or a model.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from tkinter import messagebox

from giscode.gui import task_queue_panel
from giscode.task_control.queue import QueueStatus, TaskQueue


def default_queue_path() -> Path:
    home = os.environ.get("GISCODE_HOME")
    base = Path(home) if home else Path.home() / ".giscode"
    return base / "task_queue.json"


class QueueHost:
    """Mixin: call ``_init_task_queue()`` after the window exists."""

    def _init_task_queue(self) -> None:
        queue_path = default_queue_path()
        self._task_queue = TaskQueue(queue_path)
        self._task_queue.set_executor(self._queue_execute_item)

    # ------------------------------------------------------------------ panel
    def _build_task_queue_panel(self, parent) -> None:
        if getattr(self, "_task_queue", None) is None:
            self._init_task_queue()
        task_queue_panel.build_queue_panel(self, parent)

    def _queue_refresh(self) -> None:
        task_queue_panel.refresh(self)

    def _queue_tick(self) -> None:
        if getattr(self, "_queue_closing", False):
            return
        self._queue_refresh()
        try:
            self.root.after(700, self._queue_tick)
        except Exception:
            pass

    def _queue_selected(self):
        return task_queue_panel.selected_item(self)

    # --------------------------------------------------------------- actions
    def _queue_add_from_entry(self) -> None:
        entry = getattr(self, "task_entry", None)
        if entry is None:
            return
        text = entry.get("1.0", "end-1c").strip()
        if not text:
            self._status_var.set("Type a request first, then add it to the queue.")
            return
        try:
            self._task_queue.add(text)
        except ValueError as exc:
            self._status_var.set(str(exc))
            return
        entry.delete("1.0", "end")
        self._status_var.set("Added to the task queue.")
        self._queue_refresh()

    def _queue_start(self) -> None:
        if getattr(self, "_task_queue", None) is None:
            return
        if not self._task_queue.start():
            self._status_var.set(
                "Queue not started: it may be paused, empty, or already running."
            )
        self._queue_refresh()

    def _queue_pause(self) -> None:
        if getattr(self, "_task_queue", None) is None:
            return
        self._task_queue.pause()
        self._status_var.set("Queue paused. The running task is allowed to finish.")
        self._queue_refresh()

    def _queue_resume(self) -> None:
        if getattr(self, "_task_queue", None) is None:
            return
        if getattr(self, "_task_queue", None) is not None:
            self._task_queue.resume()
        self._queue_refresh()

    def _queue_cancel_current(self) -> None:
        running = getattr(self, "_task_queue", None)
        if running is None or not running.cancel_current():
            self._status_var.set("No queued task is currently running.")
            return
        self._status_var.set("Stopping the running queued task…")
        self._queue_refresh()

    def _queue_cancel_all(self) -> None:
        queue = getattr(self, "_task_queue", None)
        if queue is None:
            return
        pending = sum(1 for item in queue.items() if not item.terminal and item.status != QueueStatus.RUNNING)
        if pending and not messagebox.askyesno(
            "Cancel remaining queue?",
            f"Cancel {pending} pending task(s)? The running task keeps running until stopped.",
            parent=self.root,
        ):
            return
        queue.cancel_all()
        self._queue_refresh()

    def _queue_remove_selected(self) -> None:
        item = self._queue_selected()
        if item is None:
            self._status_var.set("Select a queued task first.")
            return
        if not messagebox.askyesno(
            "Remove queued task?",
            f"Remove this request from the queue?\n\n{item.prompt[:200]}",
            parent=self.root,
        ):
            return
        try:
            self._task_queue.remove(item.id)
        except RuntimeError as exc:
            self._status_var.set(str(exc))
        self._queue_refresh()

    def _queue_move_selected(self, delta: int) -> None:
        item = self._queue_selected()
        if item is None:
            self._status_var.set("Select a queued task first.")
            return
        self._task_queue.move(item.id, delta)
        self._queue_refresh()

    def _queue_retry_selected(self) -> None:
        item = self._queue_selected()
        if item is None or not self._task_queue.retry(item.id):
            self._status_var.set("Only failed, cancelled or interrupted tasks can be retried.")
            return
        self._status_var.set("Task reset to waiting. Press Start to run it.")
        self._queue_refresh()

    def _queue_clear_finished(self) -> None:
        if getattr(self, "_task_queue", None) is None:
            return
        removed = self._task_queue.clear_finished()
        self._status_var.set(f"Cleared {removed} finished task(s) from the queue view.")
        self._queue_refresh()

    def _queue_shutdown(self) -> None:
        self._queue_closing = True
        queue = getattr(self, "_task_queue", None)
        if queue is not None:
            queue.stop_worker()
