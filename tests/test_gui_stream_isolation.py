"""GUI isolation for streamed provider answers.

These use the real MainWindow widgets but drive the stream lifecycle directly,
so Stop/New Chat and queued completion callbacks are deterministic and do not
depend on model timing.
"""

from __future__ import annotations

import threading
import time

from tests.conftest import _FakeGisAiEngine
from giscode.gui.main_window import (
    MainWindow,
    _RequestChannel,
    _StreamEvent,
    _TaskEvent,
)
from giscode.task_control.manager import TaskManager


class _FakeInference:
    def __init__(self):
        self.cancelled = False

    def get_health(self):
        return {"model_loaded": True}

    def cancel(self):
        self.cancelled = True


class _FakeRegistry:
    def list_all(self):
        return []


class _FakeModelManager:
    def __init__(self, inference):
        self._inference = inference
        self._settings = {}

    def get_current_model(self):
        return None

    def auto_register_discovered(self):
        pass

    def get_registry(self):
        return _FakeRegistry()

    def get_health(self):
        return {"model_loaded": True}


def _make_window(tk_root):
    inference = _FakeInference()
    window = MainWindow(
        tk_root,
        task_manager=TaskManager(),
        model_manager=_FakeModelManager(inference),
        gisai_engine=_FakeGisAiEngine(inference),
    )
    return window, inference


def _seed_stream(window, text="Half an answer", *, request_id="req-old"):
    session = window._current_chat_session()
    entry = {"role": "assistant", "content": text}
    session.setdefault("history", []).append(entry)
    window._task_chat_session = session
    window._task_session_id = window._session_id_for(session)
    window._task_request_id = request_id
    window._stream_entry = entry
    window._streamed_any = True
    window._active_task = True
    window._task_epoch = 1
    window._current_task_epoch = 1
    window._task_start_time = time.time()
    channel = _RequestChannel(request_id, window._task_session_id, 1)
    window._active_stream = channel
    window._stream_queue = channel.events
    window._send_cancel = threading.Event()
    return session, entry


def test_timeout_preserves_partial_text_and_marks_it_interrupted(tk_root):
    window, _inference = _make_window(tk_root)
    try:
        session, entry = _seed_stream(window, "TIMEOUT-PARTIAL")

        window._force_task_timeout()

        content = entry["content"]
        assert "TIMEOUT-PARTIAL" in content
        assert "time limit" in content
        assert "[Timed out" in content
        assert content.count("TIMEOUT-PARTIAL") == 1
        assert window.phase_var.get() == "Timeout"
        assert window._active_task is False
        stored = window._chat_store.load_history(session["chat_id"])
        assert any("TIMEOUT-PARTIAL" in item.content for item in stored)
    finally:
        window._task_manager.cancel()


def test_stop_preserves_partial_text_and_marks_it_interrupted(tk_root):
    window, inference = _make_window(tk_root)
    try:
        assert 0 < window._stream_queue.maxsize
        session, _entry = _seed_stream(window)
        window._stream_queue.put(" plus queued")

        window._revoke_task()

        content = session["history"][0]["content"]
        assert content.startswith("Half an answer plus queued")
        assert "[Stopped" in content
        assert "not completed" in content
        assert window.phase_var.get() == "Cancelled"
        assert window._active_task is False
        assert window._send_cancel.is_set()
        assert inference.cancelled is True
        stored = window._chat_store.load_history(session["chat_id"])
        assert any("[Stopped" in item.content for item in stored)
    finally:
        window._task_manager.cancel()


def test_new_chat_stops_the_old_request_into_the_old_conversation(tk_root):
    window, _inference = _make_window(tk_root)
    try:
        old, _entry = _seed_stream(window, "old partial")

        window._new_chat()

        fresh = window._current_chat_session()
        assert fresh is not old
        assert window._current_chat_index == 1
        assert "old partial" in old["history"][0]["content"]
        assert "[Stopped" in old["history"][0]["content"]
        assert fresh.get("history", []) == []
        assert "old partial" not in window.response_text.get("1.0", "end-1c")
    finally:
        window._task_manager.cancel()


def test_switching_chats_stops_the_stream_into_its_original_chat(tk_root):
    window, _inference = _make_window(tk_root)
    try:
        window._new_chat()
        window._switch_chat(0)
        old, _entry = _seed_stream(window, "stream in chat one")

        window._switch_chat(1)

        fresh = window._current_chat_session()
        assert fresh is not old
        assert "stream in chat one" in old["history"][0]["content"]
        assert "[Stopped" in old["history"][0]["content"]
        assert fresh.get("history", []) == []
        assert "stream in chat one" not in window.response_text.get("1.0", "end-1c")
    finally:
        window._task_manager.cancel()


def test_queued_completion_after_new_chat_stays_in_original_chat(tk_root):
    window, _inference = _make_window(tk_root)
    try:
        old = window._current_chat_session()
        window._task_chat_session = old
        window._task_epoch = 7
        window._current_task_epoch = 7
        window._task_start_time = time.time()
        window._worker_results[7] = ({"response": "OLD ANSWER", "work_result": {}}, None, 7, None)

        window._new_chat()
        window._drain_worker_result(7)

        fresh = window._current_chat_session()
        assert old["history"][-1]["content"] == "OLD ANSWER"
        assert fresh.get("history", []) == []
        assert "OLD ANSWER" not in window.response_text.get("1.0", "end-1c")
    finally:
        window._task_manager.cancel()


def test_late_drain_after_finish_cannot_append_a_second_answer(tk_root):
    window, _inference = _make_window(tk_root)
    try:
        session = window._current_chat_session()
        window._task_chat_session = session
        window._task_epoch = 3
        window._active_task = False
        window._stream_entry = None
        channel = _RequestChannel("old", window._session_id_for(session), 3)
        window._active_stream = channel
        window._stream_queue = channel.events
        window._stream_queue.put("late text")

        window._drain_stream(3)

        assert session.get("history", []) == []
    finally:
        window._task_manager.cancel()


def test_delta_queued_before_new_chat_stays_in_old_conversation(tk_root):
    window, _inference = _make_window(tk_root)
    try:
        old, _entry = _seed_stream(window, "old partial")
        old_channel = window._active_stream
        old_channel.events.put(
            _StreamEvent(
                "delta",
                old_channel.request_id,
                old_channel.session_id,
                old_channel.epoch,
                "LEAKED-OLD-DELTA",
            )
        )

        window._new_chat()

        fresh = window._current_chat_session()
        assert fresh is not old
        assert fresh.get("history", []) == []
        assert "LEAKED-OLD-DELTA" not in window.response_text.get("1.0", "end-1c")
        assert "LEAKED-OLD-DELTA" in old["history"][0]["content"]
        assert "[Stopped" in old["history"][0]["content"]
    finally:
        window._task_manager.cancel()


def test_late_delta_from_an_old_channel_is_rejected_by_a_new_request(tk_root):
    window, _inference = _make_window(tk_root)
    try:
        _old, _entry = _seed_stream(window, "old partial")
        old_channel = window._active_stream
        window._new_chat()
        fresh = window._current_chat_session()
        window._task_chat_session = fresh
        window._task_session_id = window._session_id_for(fresh)
        window._task_request_id = "req-new"
        window._task_epoch += 1
        window._active_task = True
        new_channel = _RequestChannel("req-new", window._task_session_id, window._task_epoch)
        window._active_stream = new_channel
        window._stream_queue = new_channel.events

        # The old worker may still call with its captured channel.
        window._stream_push("LEAKED-OLD-DELTA", channel=old_channel)
        window._drain_stream(window._task_epoch)

        assert fresh.get("history", []) == []
        assert "LEAKED-OLD-DELTA" not in window.response_text.get("1.0", "end-1c")
    finally:
        window._task_manager.cancel()


def test_closing_active_chat_cancels_and_late_output_is_rejected(tk_root):
    window, inference = _make_window(tk_root)
    try:
        window._new_chat()
        old, _entry = _seed_stream(window, "closed partial", request_id="req-closed")
        channel = window._active_stream

        window._close_chat(window._current_chat_index)

        assert inference.cancelled is True
        assert window._active_task is False
        assert channel.closed is True
        assert old not in window._chat_sessions
        assert "closed partial" in old["history"][0]["content"]
        channel.events.put(
            _StreamEvent("delta", channel.request_id, channel.session_id, channel.epoch, "late closed")
        )
        window._drain_stream(channel.epoch)
        assert "late closed" not in old["history"][0]["content"]
        assert all("late closed" not in item.get("content", "") for item in window._chat_sessions[0]["history"])
    finally:
        window._task_manager.cancel()


def test_closing_an_inactive_chat_does_not_cancel_the_active_one(tk_root):
    window, _inference = _make_window(tk_root)
    try:
        window._new_chat()
        window._switch_chat(0)
        _session, _entry = _seed_stream(window, "still active")
        active_epoch = window._task_epoch
        active_event = window._send_cancel

        window._close_chat(1)

        assert window._active_task is True
        assert window._task_epoch == active_epoch
        assert active_event.is_set() is False
    finally:
        window._task_manager.cancel()


def test_completion_event_with_old_request_id_is_ignored(tk_root):
    window, _inference = _make_window(tk_root)
    try:
        _old, _entry = _seed_stream(window, "old")
        window._task_epoch += 1
        window._task_request_id = "req-new"
        window._task_session_id = window._session_id_for(window._current_chat_session())
        window._worker_results[window._task_epoch] = _TaskEvent(
            "completed", "req-old", window._task_session_id, window._task_epoch, result={"response": "STALE"}
        )

        window._drain_worker_result(window._task_epoch)

        assert "STALE" not in window.response_text.get("1.0", "end-1c")
    finally:
        window._task_manager.cancel()