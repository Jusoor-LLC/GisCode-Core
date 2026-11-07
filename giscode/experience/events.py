"""Lightweight synchronous event bus for the Experience Layer.

The bus is intentionally tiny: subscribers are callables that accept
the event name and a payload dict. Failures in any subscriber are
swallowed so a single bad listener cannot break the caller.

This is purely a UI-facing signal channel — it never carries data
that affects the technical result of any operation.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Iterable


class EventName:
    MESSAGE_SENT = "message_sent"
    RESPONSE_READY = "response_ready"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    WARNING = "warning"
    ATTENTION_NEEDED = "attention_needed"
    PROJECT_STARTED = "project_started"
    PROJECT_COMPLETED = "project_completed"
    RESEARCH_STARTED = "research_started"
    RESEARCH_COMPLETED = "research_completed"
    MODEL_READY = "model_ready"

    ALL: tuple[str, ...] = (
        MESSAGE_SENT,
        RESPONSE_READY,
        TASK_STARTED,
        TASK_COMPLETED,
        TASK_FAILED,
        TASK_CANCELLED,
        WARNING,
        ATTENTION_NEEDED,
        PROJECT_STARTED,
        PROJECT_COMPLETED,
        RESEARCH_STARTED,
        RESEARCH_COMPLETED,
        MODEL_READY,
    )


Listener = Callable[[str, dict], None]


class EventBus:
    """Synchronous in-process event bus.

    Subscribers are called on the dispatching thread. Use
    :func:`safe_dispatch` to invoke the bus from performance-sensitive
    paths and discard the result.
    """

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_name: str, listener: Listener) -> Callable[[], None]:
        if event_name not in EventName.ALL:
            raise ValueError(f"unknown event: {event_name!r}")
        with self._lock:
            self._listeners.setdefault(event_name, []).append(listener)

        def _unsubscribe() -> None:
            with self._lock:
                lst = self._listeners.get(event_name, [])
                if listener in lst:
                    lst.remove(listener)

        return _unsubscribe

    def listeners(self, event_name: str) -> list[Listener]:
        with self._lock:
            return list(self._listeners.get(event_name, []))

    def clear(self, event_name: Optional[str] = None) -> None:
        with self._lock:
            if event_name is None:
                self._listeners.clear()
            else:
                self._listeners.pop(event_name, None)

    def dispatch(self, event_name: str, payload: Optional[dict] = None) -> int:
        if event_name not in EventName.ALL:
            return 0
        for listener in self.listeners(event_name):
            try:
                listener(event_name, dict(payload or {}))
            except Exception:
                pass
        return len(self.listeners(event_name))


def safe_dispatch(bus: EventBus, event_name: str, payload: Optional[dict] = None) -> None:
    """Dispatch without ever raising. Returns nothing useful — pure
    cosmetic plumbing."""
    try:
        bus.dispatch(event_name, payload or {})
    except Exception:
        pass
