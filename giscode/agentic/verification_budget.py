"""One time budget shared by baseline, verification and repair verification.

Each of those test runs used to receive a fresh per-pass timeout, so a task with
three repair attempts could spend five independent 120 s project-test runs (600 s
of verification alone) while the only aggregate limit was the task deadline -
and that deadline is polled between steps, not inside a run. This module turns
the separate allowances into one pot::

    total = min(time actually left on the task, enough for every allowed run)

The pot is consumed by the measured duration of each run, and each run is still
clamped to what the task deadline allows. Nothing here extends a deadline: the
outer task budget is always the ceiling. A task without a deadline (unit tests
that call the executor directly) is bounded by the pass count instead.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

# A project-test run keeps its existing caps; the budget never raises them.
PYTHON_PASS_TIMEOUT_S = 120.0
GRADLE_PASS_TIMEOUT_S = 600.0

# The largest cap any single pass may use. A Gradle verification compiles the
# project before it runs tests, so the aggregate has to be able to fund passes
# of that size; a Python pass is still capped at its own smaller value.
LARGEST_PASS_TIMEOUT_S = GRADLE_PASS_TIMEOUT_S

# Production allows three repair attempts (the engine constructs the real-work
# executor with 3). Kept here so the budget and its callers cannot drift apart.
DEFAULT_MAX_REPAIR_ATTEMPTS = 3

# A run shorter than this cannot reach a verdict: it would start a test
# interpreter, be killed, and be reported as "timed out" without evidence. Five
# seconds is comfortably longer than starting an interpreter and short enough to
# still be useful on an almost exhausted budget. A run below it is refused
# rather than started - in particular because ``CommandExecutor._resolve_timeout``
# turns a non-positive timeout back into its 60 s default.
MIN_USEFUL_PASS_S = 5.0

# "No deadline" sentinel, matching ``GisAiEngine._task_remaining_s``.
_UNBOUNDED = 1e9


def test_runs_allowed(max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS) -> int:
    """Test runs a task may perform: baseline + initial verify + one per repair."""
    try:
        repairs = int(max_repair_attempts)
    except (TypeError, ValueError):
        repairs = DEFAULT_MAX_REPAIR_ATTEMPTS
    return 2 + max(0, repairs)


def verification_budget_s(
    task_remaining_s: float,
    *,
    per_pass_s: float = LARGEST_PASS_TIMEOUT_S,
    max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
) -> float:
    """Seconds available to all of this task's test runs, never above the task's own.

    ``task_remaining_s`` is what the task deadline still allows (``1e9`` when
    there is no deadline). The result is the smaller of that and what every
    allowed run would need at the largest pass cap, so the budget can never
    outlive the task and can never be larger than the work it funds.
    """
    try:
        remaining = float(task_remaining_s)
    except (TypeError, ValueError):
        remaining = 0.0
    if remaining != remaining or remaining < 0.0:  # NaN or negative
        remaining = 0.0
    try:
        per_pass = float(per_pass_s)
    except (TypeError, ValueError):
        per_pass = LARGEST_PASS_TIMEOUT_S
    if per_pass != per_pass or per_pass <= 0.0:  # NaN or non-positive
        per_pass = LARGEST_PASS_TIMEOUT_S
    return min(remaining, per_pass * test_runs_allowed(max_repair_attempts))


class VerificationBudget:
    """A countdown shared by every test run of one task.

    ``task_remaining_fn`` is the task deadline (``None`` when the host has no
    deadline); it is re-read on every query, so time the deadline excludes -
    an approval dialog, for instance - is not charged to the budget, and a
    deadline that is nearly exhausted clamps the next run even if the pot itself
    still has time. ``clock`` is injectable so tests need no real waiting.
    """

    def __init__(
        self,
        task_remaining_fn: Optional[Callable[[], float]] = None,
        *,
        per_pass_s: float = LARGEST_PASS_TIMEOUT_S,
        max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._task_remaining_fn = task_remaining_fn
        self._per_pass_s = per_pass_s
        self._max_repair_attempts = max_repair_attempts
        self._clock = clock or time.monotonic
        self._total = verification_budget_s(
            self._task_remaining(),
            per_pass_s=per_pass_s,
            max_repair_attempts=max_repair_attempts,
        )
        self._spent = 0.0
        self._passes = 0

    def _task_remaining(self) -> float:
        if self._task_remaining_fn is None:
            return _UNBOUNDED
        try:
            return float(self._task_remaining_fn())
        except Exception:  # noqa: BLE001 - an unreadable deadline is not a limit
            return _UNBOUNDED

    @property
    def total(self) -> float:
        """The pot this task started with."""
        return self._total

    @property
    def spent(self) -> float:
        """Measured duration of the test runs already performed."""
        return self._spent

    @property
    def passes(self) -> int:
        """How many test runs were started against this budget."""
        return self._passes

    def remaining(self) -> float:
        """Seconds left for the next run: the pot, clamped by the task deadline."""
        own = max(0.0, self._total - self._spent)
        return max(0.0, min(own, self._task_remaining()))

    def exhausted(self, minimum: float = MIN_USEFUL_PASS_S) -> bool:
        """True when a run cannot be started honestly with what is left."""
        return self.remaining() < float(minimum)

    def start_pass(self) -> float:
        """Mark a test run as started; returns the timestamp to pass to ``finish_pass``."""
        self._passes += 1
        return self._clock()

    def finish_pass(self, started_at: float) -> None:
        """Charge the run's measured duration to the pot."""
        try:
            self._spent += max(0.0, self._clock() - float(started_at))
        except (TypeError, ValueError):
            pass
