"""Acceptance tests for the shared task queue (Core and Pro use this engine)."""
from __future__ import annotations

import json
import threading
import time

import pytest

from giscode.task_control.queue import QueueStatus, TaskQueue


class Recorder:
    """Deterministic executor: records order and concurrency, obeys cancel."""

    def __init__(self, outcomes=None, delay=0.0):
        self.outcomes = outcomes or {}
        self.delay = delay
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def __call__(self, item, cancel_event, on_status):
        with self.lock:
            self.calls.append(item.prompt)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            on_status("running")
            deadline = time.time() + self.delay
            while time.time() < deadline:
                if cancel_event.wait(0.01):
                    return {"status": QueueStatus.CANCELLED}
            outcome = self.outcomes.get(item.prompt, {"status": QueueStatus.COMPLETED})
            if cancel_event.is_set():
                return {"status": QueueStatus.CANCELLED}
            return dict(outcome)
        finally:
            with self.lock:
                self.active -= 1


def make_queue(tmp_path, executor, name="queue.json"):
    return TaskQueue(tmp_path / name, executor=executor)


def drain(queue, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if queue.wait(0.2):
            return
    raise AssertionError("queue worker did not finish")


def test_three_tasks_run_in_order_and_only_one_at_a_time(tmp_path):
    executor = Recorder(delay=0.05)
    queue = make_queue(tmp_path, executor)
    for text in ("one", "two", "three"):
        queue.add(text)
    assert queue.start() is True
    drain(queue)
    assert executor.calls == ["one", "two", "three"]
    assert executor.max_active == 1
    assert all(item.status == QueueStatus.COMPLETED for item in queue.items())


def test_second_start_does_not_spawn_a_parallel_worker(tmp_path):
    executor = Recorder(delay=0.2)
    queue = make_queue(tmp_path, executor)
    queue.add("only")
    assert queue.start() is True
    assert queue.start() is False
    drain(queue)
    assert executor.calls == ["only"]


def test_failure_is_recorded_as_failed_and_queue_continues(tmp_path):
    executor = Recorder(outcomes={"bad": {"status": QueueStatus.FAILED, "error": "boom"}})
    queue = make_queue(tmp_path, executor)
    for text in ("bad", "good"):
        queue.add(text)
    queue.start()
    drain(queue)
    items = queue.items()
    assert items[0].status == QueueStatus.FAILED and items[0].error == "boom"
    assert items[1].status == QueueStatus.COMPLETED
    assert "boom" not in items[1].result


def test_cancelled_task_is_not_completed_and_blocks_later_items(tmp_path):
    executor = Recorder(delay=5)
    queue = make_queue(tmp_path, executor)
    queue.add("long")
    queue.add("after")
    queue.start()
    deadline = time.time() + 5
    while time.time() < deadline and queue.current() is None:
        time.sleep(0.02)
    assert queue.cancel_current() is True
    drain(queue)
    items = queue.items()
    assert items[0].status == QueueStatus.CANCELLED
    assert items[1].status == QueueStatus.WAITING  # no unintended continuation


def test_pause_preserves_waiting_items_and_resume_continues(tmp_path):
    executor = Recorder(delay=0.05)
    queue = make_queue(tmp_path, executor)
    for text in ("first", "second"):
        queue.add(text)
    queue.start()
    drain(queue)
    queue.add("third")
    queue.pause()
    assert queue.start() is False
    assert queue.items()[-1].status == QueueStatus.WAITING
    queue.resume()
    drain(queue)
    assert executor.calls == ["first", "second", "third"]


def test_reordering_and_removal_change_execution_order(tmp_path):
    executor = Recorder()
    queue = make_queue(tmp_path, executor)
    first, second, third = (queue.add(t) for t in ("a", "b", "c"))
    assert queue.move(third.id, -2) is True
    assert queue.remove(second.id) is True
    assert [item.prompt for item in queue.items()] == ["c", "a"]
    queue.start()
    drain(queue)
    assert executor.calls == ["c", "a"]


def test_restart_loads_queue_and_history(tmp_path):
    executor = Recorder()
    queue = make_queue(tmp_path, executor)
    queue.add("kept")
    queue.start()
    drain(queue)
    reopened = TaskQueue(tmp_path / "queue.json")
    items = reopened.items()
    assert [item.prompt for item in items] == ["kept"]
    assert items[0].status == QueueStatus.COMPLETED


def test_running_item_recovers_as_interrupted_without_duplicate_execution(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"version": 1, "items": [
        {"id": "a", "prompt": "was running", "status": QueueStatus.RUNNING},
        {"id": "b", "prompt": "still waiting", "status": QueueStatus.WAITING},
    ]}), encoding="utf-8")
    executor = Recorder()
    queue = TaskQueue(path, executor=executor)
    items = queue.items()
    assert items[0].status == QueueStatus.INTERRUPTED
    assert items[1].status == QueueStatus.WAITING
    queue.start()
    drain(queue)
    assert executor.calls == ["still waiting"]


def test_retry_runs_failed_item_once_more(tmp_path):
    executor = Recorder(outcomes={"flaky": {"status": QueueStatus.FAILED, "error": "no"}})
    queue = make_queue(tmp_path, executor)
    item = queue.add("flaky")
    queue.start()
    drain(queue)
    assert queue.items()[0].status == QueueStatus.FAILED
    assert queue.retry(item.id) is True
    executor.outcomes["flaky"] = {"status": QueueStatus.COMPLETED}
    queue.start()
    drain(queue)
    assert executor.calls == ["flaky", "flaky"]
    assert queue.items()[0].status == QueueStatus.COMPLETED


def test_approval_is_required_before_execution(tmp_path):
    executor = Recorder(outcomes={"needs approval": {"status": QueueStatus.NEEDS_APPROVAL}})
    queue = make_queue(tmp_path, executor)
    item = queue.add("needs approval")
    queue.add("after approval")
    queue.start()
    drain(queue)
    assert queue.items()[0].status == QueueStatus.NEEDS_APPROVAL
    assert queue.items()[1].status == QueueStatus.WAITING  # blocked, not bypassed
    assert queue.approve(item.id) is True
    executor.outcomes["needs approval"] = {"status": QueueStatus.COMPLETED}
    queue.start()
    drain(queue)
    assert queue.items()[0].status == QueueStatus.COMPLETED
    assert queue.items()[1].status == QueueStatus.COMPLETED


def test_cancel_all_marks_only_pending_items(tmp_path):
    executor = Recorder()
    queue = make_queue(tmp_path, executor)
    queue.add("done")
    queue.start()
    drain(queue)
    queue.add("pending one")
    queue.add("pending two")
    queue.cancel_all()
    statuses = [item.status for item in queue.items()]
    assert statuses == [QueueStatus.COMPLETED, QueueStatus.CANCELLED, QueueStatus.CANCELLED]


def test_snapshot_reports_position_and_progress(tmp_path):
    executor = Recorder()
    queue = make_queue(tmp_path, executor)
    queue.add("x")
    queue.add("y")
    snapshot = queue.snapshot()
    assert snapshot["count"] == 2 and snapshot["waiting"] == 2
    assert snapshot["running_index"] is None
    assert snapshot["path"].endswith("queue.json")


def test_empty_prompt_is_rejected(tmp_path):
    queue = TaskQueue(tmp_path / "queue.json")
    with pytest.raises(ValueError):
        queue.add("   ")


def test_corrupt_store_is_preserved_and_queue_starts_empty(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text("{not json", encoding="utf-8")
    queue = TaskQueue(path)
    assert queue.items() == []
    assert (tmp_path / "queue.corrupt.json").is_file()
