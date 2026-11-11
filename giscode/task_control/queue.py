"""A persistent, sequential task queue shared by GisCode Core and Pro.

Design constraints that come from the two editions:

* **One engine, edition adapters.** The queue knows nothing about GisAI, Pro,
  models or GUI toolkits. An edition supplies ``executor(item, cancel_event,
  on_status)`` and the engine owns ordering, persistence, single-flight
  execution, pause/resume, retry and recovery.
* **One task at a time.** A single worker loop is the only caller of the
  executor; the engine refuses to start a second worker and persists which item
  is running, so a crash can never leave two "running" items.
* **No optimistic results.** Statuses are recorded from what the executor
  reports. ``COMPLETED`` is written only for an explicit success result; an
  exception, cancellation, timeout or interruption is recorded as such.
* **Recovery without duplicate execution.** On load, an item that was RUNNING
  becomes INTERRUPTED and is never auto-started. Only WAITING items may run on
  an explicit resume/start.
* **Approval is a state, not a bypass.** An executor may report
  ``needs_approval``; the item then waits for ``approve`` and is never executed
  again without it.

The store is a single JSON document written atomically (temp + replace), so a
kill during a write leaves the previous document intact.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

QUEUE_VERSION = 1


class QueueStatus:
    WAITING = "waiting"
    RUNNING = "running"
    NEEDS_APPROVAL = "needs_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED, INTERRUPTED})


@dataclass
class QueueItem:
    id: str
    prompt: str
    status: str = QueueStatus.WAITING
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: str = ""
    error: str = ""
    approved: bool = False

    @property
    def terminal(self) -> bool:
        return self.status in QueueStatus.TERMINAL

    def to_dict(self) -> dict:
        data = asdict(self)
        data["terminal"] = self.terminal
        return data


class TaskQueue:
    """Ordered, persistent, single-flight task queue.

    ``executor(item, cancel_event, on_status)`` must return a dict with at
    least ``{"status": one of the queue statuses}``; ``result`` and ``error``
    are optional. ``on_status(text)`` is called with progress text and never
    changes task status by itself.
    """

    def __init__(
        self,
        path,
        executor: Optional[Callable] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self._executor = executor
        self._clock = clock
        self._items: list[QueueItem] = []
        self._lock = threading.RLock()
        self._worker: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._paused = False
        self._closed = False
        self._load()

    # ------------------------------------------------------------------ store
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw = data.get("items", [])
        except Exception:
            # A corrupt queue file must not stop the application; keep a copy
            # for inspection and start empty rather than guessing.
            try:
                self.path.replace(self.path.with_suffix(".corrupt.json"))
            except Exception:
                pass
            return
        items = []
        for entry in raw:
            try:
                item = QueueItem(
                    id=str(entry["id"]),
                    prompt=str(entry["prompt"]),
                    status=str(entry.get("status", QueueStatus.WAITING)),
                    attempts=int(entry.get("attempts", 0)),
                    created_at=float(entry.get("created_at", 0.0)),
                    updated_at=float(entry.get("updated_at", 0.0)),
                    started_at=entry.get("started_at"),
                    finished_at=entry.get("finished_at"),
                    result=str(entry.get("result", "")),
                    error=str(entry.get("error", "")),
                    approved=bool(entry.get("approved", False)),
                )
            except Exception:
                continue
            if item.status == QueueStatus.RUNNING:
                item.status = QueueStatus.INTERRUPTED
                item.error = item.error or "The application stopped while this task was running."
                item.finished_at = item.finished_at or self._clock()
                item.updated_at = item.updated_at or self._clock()
            items.append(item)
        with self._lock:
            self._items = items
        self._save()

    def _save(self) -> None:
        # The snapshot and the swap happen under one lock: the worker and the
        # GUI both persist (adding or clearing while a task runs), and two
        # writers sharing a single temp path would race each other's replace.
        with self._lock:
            payload = {
                "version": QUEUE_VERSION,
                "saved_at": self._clock(),
                "items": [item.to_dict() for item in self._items],
            }
            text = json.dumps(payload, indent=2)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, self.path)

    # ------------------------------------------------------------------- edits
    def add(self, prompt: str) -> QueueItem:
        text = (prompt or "").strip()
        if not text:
            raise ValueError("A queued task needs a request.")
        with self._lock:
            item = QueueItem(id=uuid.uuid4().hex, prompt=text, created_at=self._clock())
            item.updated_at = item.created_at
            self._items.append(item)
        self._save()
        return item

    def remove(self, item_id: str) -> bool:
        with self._lock:
            for index, item in enumerate(self._items):
                if item.id == item_id:
                    if item.status == QueueStatus.RUNNING:
                        raise RuntimeError("Stop the running task before removing it.")
                    del self._items[index]
                    break
            else:
                return False
        self._save()
        return True

    def move(self, item_id: str, delta: int) -> bool:
        with self._lock:
            index = next((i for i, item in enumerate(self._items) if item.id == item_id), None)
            if index is None:
                return False
            target = max(0, min(len(self._items) - 1, index + int(delta)))
            if target == index:
                return False
            item = self._items.pop(index)
            self._items.insert(target, item)
        self._save()
        return True

    def retry(self, item_id: str) -> bool:
        with self._lock:
            item = self._get(item_id)
            if item is None or item.status not in (
                QueueStatus.FAILED, QueueStatus.CANCELLED, QueueStatus.INTERRUPTED
            ):
                return False
            item.status = QueueStatus.WAITING
            item.error = ""
            item.result = ""
            item.started_at = None
            item.finished_at = None
            item.updated_at = self._clock()
        self._save()
        return True

    def approve(self, item_id: str) -> bool:
        with self._lock:
            item = self._get(item_id)
            if item is None or item.status != QueueStatus.NEEDS_APPROVAL:
                return False
            item.approved = True
            item.status = QueueStatus.WAITING
            item.updated_at = self._clock()
            # Approval is the user's decision to continue; it clears the pause
            # the approval gate imposed, but execution still needs start().
            self._paused = False
        self._save()
        return True

    def clear_finished(self) -> int:
        with self._lock:
            before = len(self._items)
            self._items = [item for item in self._items if not item.terminal]
            removed = before - len(self._items)
        if removed:
            self._save()
        return removed

    # ---------------------------------------------------------------- controls
    def set_executor(self, executor: Callable) -> None:
        self._executor = executor

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self.start()

    def cancel_current(self) -> bool:
        with self._lock:
            running = self._running_item()
        if running is None:
            return False
        self._cancel_event.set()
        return True

    def cancel_all(self) -> None:
        self._cancel_event.set()
        self._paused = True
        with self._lock:
            now = self._clock()
            for item in self._items:
                if item.status in (QueueStatus.WAITING, QueueStatus.NEEDS_APPROVAL):
                    item.status = QueueStatus.CANCELLED
                    item.finished_at = now
                    item.updated_at = now
                    item.error = item.error or "Cancelled with the remaining queue."
        self._save()

    # ------------------------------------------------------------------ worker
    def start(self) -> bool:
        """Start the single worker if it is not already running."""
        with self._lock:
            if self._executor is None:
                raise RuntimeError("The queue has no executor for this edition.")
            if self._worker is not None and self._worker.is_alive():
                return False
            if not any(item.status == QueueStatus.WAITING for item in self._items):
                return False
            if self._paused:
                # Pause is a user decision; only resume()/approve() clear it.
                return False
            self._closed = False
            self._cancel_event = threading.Event()
            self._worker = threading.Thread(
                target=self._run, name="giscode-task-queue", daemon=True
            )
            self._worker.start()
            return True

    def stop_worker(self) -> None:
        """Stop the loop without changing item statuses (application exit)."""
        self._closed = True
        self._cancel_event.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)

    def wait(self, timeout: Optional[float] = None) -> bool:
        worker = self._worker
        if worker is None:
            return True
        worker.join(timeout=timeout)
        return not worker.is_alive()

    def _run(self) -> None:
        while not self._closed:
            if self._paused:
                return
            with self._lock:
                item = next(
                    (entry for entry in self._items
                     if entry.status == QueueStatus.WAITING and (
                         not entry.approved or entry.status == QueueStatus.WAITING
                     )),
                    None,
                )
                if item is None:
                    return
                item.status = QueueStatus.RUNNING
                item.started_at = self._clock()
                item.updated_at = item.started_at
                item.attempts += 1
                item.error = ""
            self._save()
            cancel_event = self._cancel_event
            outcome = None
            try:
                outcome = self._executor(item, cancel_event, self._progress)
            except Exception as exc:  # a crashing adapter is a failed task
                outcome = {"status": QueueStatus.FAILED, "error": repr(exc)}
            with self._lock:
                if cancel_event.is_set() and (not outcome or outcome.get("status") not in QueueStatus.TERMINAL):
                    outcome = {"status": QueueStatus.CANCELLED}
                status = (outcome or {}).get("status", QueueStatus.FAILED)
                if status not in (
                    QueueStatus.COMPLETED, QueueStatus.FAILED, QueueStatus.CANCELLED,
                    QueueStatus.INTERRUPTED, QueueStatus.NEEDS_APPROVAL,
                ):
                    status = QueueStatus.FAILED
                item.status = status
                item.result = str((outcome or {}).get("result", "") or "")
                item.error = str((outcome or {}).get("error", "") or "")
                item.updated_at = self._clock()
                if status != QueueStatus.NEEDS_APPROVAL:
                    item.finished_at = item.updated_at
            self._save()
            if status in (QueueStatus.NEEDS_APPROVAL, QueueStatus.CANCELLED):
                # A cancelled or approval-gated task stops the run: never let the
                # queue silently continue past a stop or an unmet approval.
                self._paused = True
                return

    def _progress(self, _text: str) -> None:
        # Progress is advisory: it must never promote a task to a terminal
        # status. Editions surface it through their own UI.
        return

    # ----------------------------------------------------------------- helpers
    def _get(self, item_id: str) -> Optional[QueueItem]:
        return next((item for item in self._items if item.id == item_id), None)

    def _running_item(self) -> Optional[QueueItem]:
        return next(
            (item for item in self._items if item.status == QueueStatus.RUNNING), None
        )

    def snapshot(self) -> dict:
        with self._lock:
            items = [item.to_dict() for item in self._items]
            running = next(
                (i for i, item in enumerate(self._items)
                 if item.status == QueueStatus.RUNNING), None
            )
        waiting = [i for i, item in enumerate(items) if items[i]["status"] == QueueStatus.WAITING]
        return {
            "items": items,
            "count": len(items),
            "running_index": running,
            "waiting": len(waiting),
            "paused": self._paused,
            "worker_active": bool(self._worker and self._worker.is_alive()),
            "path": str(self.path),
        }

    def current(self) -> Optional[QueueItem]:
        with self._lock:
            return self._running_item()

    def next_waiting(self) -> Optional[QueueItem]:
        with self._lock:
            return next((item for item in self._items if item.status == QueueStatus.WAITING), None)

    def items(self) -> list[QueueItem]:
        with self._lock:
            return list(self._items)
