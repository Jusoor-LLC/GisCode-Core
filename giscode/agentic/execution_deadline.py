import threading
import time
from typing import Callable, Optional


class ExecutionDeadline:
    """A monotonic execution deadline that can be paused and resumed.

    Time spent paused (an edit approval waiting for the user) does not count
    against the budget. ``expired`` polls the clock, so a check can never be
    "skipped": once paused time is excluded, any moment past the budget
    reports expired. ``wait`` blocks on a real condition variable, so it also
    cannot spin through a pause or return early.
    """

    def __init__(self, seconds: float, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self._budget = float(seconds)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._running_since = self._clock()
        self._accumulated = 0.0
        self._paused = False

    @property
    def budget(self) -> float:
        return self._budget

    @property
    def elapsed(self) -> float:
        """Budget consumed so far; paused time is excluded."""
        with self._lock:
            return self._consumed_locked()

    @property
    def remaining(self) -> float:
        return max(0.0, self._budget - self.elapsed)

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def pause(self) -> None:
        """Start excluding time. Idempotent: a second pause is a no-op."""
        with self._lock:
            if self._paused:
                return
            self._accumulated += self._consumed_locked()
            self._paused = True

    def resume(self) -> None:
        """Stop excluding time and wake anything waiting on the deadline."""
        with self._lock:
            if not self._paused:
                return
            self._running_since = self._clock()
            self._paused = False
            self._condition.notify_all()

    def expired(self) -> bool:
        return self.elapsed >= self._budget

    def wait(self, poll: float = 0.05, abort: Optional[threading.Event] = None) -> bool:
        """Wait until expired (True) or aborted (False).

        Blocks on a real condition variable, so a paused deadline never
        reports expired and a caller cannot spin past a pause. While paused
        the lock is released between polls so ``resume`` can acquire it and
        notify. Once resumed, or once the budget is actually consumed,
        returns True; a paused deadline never returns.
        """
        while True:
            if abort is not None and abort.is_set():
                return False
            with self._condition:
                if abort is not None and abort.is_set():
                    return False
                if not self._paused:
                    return True
                self._condition.wait(timeout=poll)
            if abort is not None and abort.is_set():
                return False
            if self._paused:
                time.sleep(poll)

    def _consumed_locked(self) -> float:
        if self._paused:
            return self._accumulated
        return self._accumulated + (self._clock() - self._running_since)
