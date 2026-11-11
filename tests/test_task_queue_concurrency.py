"""Concurrency regression tests for the shared task queue store.

``TaskQueue._save`` must keep the whole snapshot-write-swap sequence under the
queue lock. When the lock was released early, the worker thread and a GUI
action (Add, Clear finished, Retry) could both write ``task_queue.tmp`` and
both replace it, which surfaced on Windows as ``WinError 32`` (file in use) or
``WinError 2`` (temp file already moved).

These tests do not sleep: threads are released together with a barrier, and the
replace boundary is observed so "one writer at a time" is asserted directly.
"""
from __future__ import annotations

import json
import threading

import pytest

from giscode.task_control import queue as queue_module
from giscode.task_control.queue import QueueStatus, TaskQueue

VALID_STATUSES = {
    QueueStatus.WAITING,
    QueueStatus.RUNNING,
    QueueStatus.NEEDS_APPROVAL,
    QueueStatus.COMPLETED,
    QueueStatus.FAILED,
    QueueStatus.CANCELLED,
    QueueStatus.INTERRUPTED,
}


class ReplaceSpy:
    """Delegates to the real ``os`` module and records replace concurrency."""

    def __init__(self, real):
        self._real = real
        self.inside = 0
        self.peak = 0
        self.calls = 0
        self._guard = threading.Lock()

    def replace(self, src, dst, *args, **kwargs):
        with self._guard:
            self.inside += 1
            self.calls += 1
            self.peak = max(self.peak, self.inside)
        try:
            return self._real.replace(src, dst, *args, **kwargs)
        finally:
            with self._guard:
                self.inside -= 1

    def __getattr__(self, name):
        return getattr(self._real, name)


def _read_document(path):
    """Parse the store and assert it is structurally valid."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == queue_module.QUEUE_VERSION
    ids = [item["id"] for item in data["items"]]
    assert len(ids) == len(set(ids)), "the store repeated an item id"
    for item in data["items"]:
        assert item["status"] in VALID_STATUSES, item["status"]
        assert item["prompt"], "an item lost its request"
    return data


def test_simultaneous_saves_from_several_threads_stay_serialized(tmp_path, monkeypatch):
    """Repeated overlapping saves never share the temporary file."""
    path = tmp_path / "task_queue.json"
    queue = TaskQueue(path)
    queue.add("seed")
    spy = ReplaceSpy(queue_module.os)
    monkeypatch.setattr(queue_module, "os", spy)

    workers = 4
    rounds = 25
    barrier = threading.Barrier(workers, timeout=10)
    errors: list[BaseException] = []

    def save_repeatedly():
        try:
            barrier.wait()
            for _ in range(rounds):
                queue._save()
        except BaseException as exc:  # any escape fails the test
            errors.append(exc)

    threads = [threading.Thread(target=save_repeatedly, name=f"save-{i}") for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert errors == [], errors
    assert spy.calls == workers * rounds, spy.calls
    assert spy.peak == 1, f"{spy.peak} writers were inside one save"
    assert not (tmp_path / "task_queue.tmp").exists(), "a temporary file was left behind"

    document = _read_document(path)
    assert [item["prompt"] for item in document["items"]] == ["seed"]
    assert document["items"][0]["status"] == QueueStatus.WAITING


def test_gui_mutations_racing_worker_saves_keep_the_store_consistent(tmp_path, monkeypatch):
    """Add/Clear from the GUI thread while the worker saves never corrupts it."""
    path = tmp_path / "task_queue.json"
    spy = ReplaceSpy(queue_module.os)
    monkeypatch.setattr(queue_module, "os", spy)

    queue = TaskQueue(path)
    prompts: list[str] = []
    gui_errors: list[BaseException] = []

    for cycle in range(12):
        barrier = threading.Barrier(2, timeout=10)
        state = {"first": True}

        def executor(item, cancel_event, on_status, barrier=barrier, state=state):
            if state["first"]:
                state["first"] = False
                barrier.wait()  # released together with the GUI mutation
            return {"status": QueueStatus.COMPLETED}

        queue.set_executor(executor)
        prompts.append(queue.add(f"worker-{cycle}").prompt)
        queue.start()

        def gui_side(cycle=cycle):
            try:
                barrier.wait()
                for index in range(4):
                    prompts.append(queue.add(f"gui-{cycle}-{index}").prompt)
                queue.clear_finished()
            except BaseException as exc:  # any escape fails the test
                gui_errors.append(exc)

        gui = threading.Thread(target=gui_side, name=f"gui-{cycle}")
        gui.start()
        queue.wait(20)
        gui.join(20)

        assert gui_errors == [], gui_errors
        assert not gui.is_alive(), "the GUI mutation thread did not finish"

    # Drain whatever the worker had not picked up yet.
    for _ in range(6):
        if not queue.start():
            break
        queue.wait(20)

    assert gui_errors == [], gui_errors
    assert spy.peak == 1, f"{spy.peak} writers were inside one save"

    items = queue.items()
    surviving = [item.prompt for item in items]
    assert len(surviving) == len(set(surviving)), "an item was duplicated in memory"
    assert all(prompt in prompts for prompt in surviving)
    assert len(prompts) == len(set(prompts)), "add returned a duplicate request"
    assert all(item.status in VALID_STATUSES for item in items), [i.status for i in items]
    assert not any(item.status == QueueStatus.RUNNING for item in items), "a run was left open"

    document = _read_document(path)
    assert [item["prompt"] for item in document["items"]] == surviving
    assert [item["status"] for item in document["items"]] == [item.status for item in items]
    assert not (tmp_path / "task_queue.tmp").exists(), "a temporary file was left behind"
    assert not (tmp_path / "task_queue.corrupt.json").exists(), "the store was seen as corrupt"

    reloaded = TaskQueue(path)
    assert [item.prompt for item in reloaded.items()] == surviving
    assert [item.status for item in reloaded.items()] == [item.status for item in items]


def test_a_stale_temporary_file_is_ignored_and_replaced(tmp_path):
    """A leftover temp file is never read as data and is cleaned up by a save."""
    path = tmp_path / "task_queue.json"
    queue = TaskQueue(path)
    queue.add("kept")

    leftover = path.with_suffix(".tmp")
    leftover.write_text("{not json", encoding="utf-8")

    reopened = TaskQueue(path)
    assert [item.prompt for item in reopened.items()] == ["kept"]

    reopened.add("second")
    assert not leftover.exists(), "the stale temp file survived a save"
    document = _read_document(path)
    assert [item["prompt"] for item in document["items"]] == ["kept", "second"]
