"""The single authority for automatic edit approval.

``GISCODE_AUTO_APPROVE_EDITS`` used to be read at four call sites in the GisAI
package, each with its own copy of the same truthy test, and it was honoured
whatever the process was. An inherited shell variable, a stray ``.env`` or a CI
profile was therefore enough to turn every "Apply proposed edit" dialog off in a
running build -- the audit's P0 finding for the approval path.

This module is now the only place the variable is read, and the question it
answers is narrow: *may this process approve an edit without a human?* Four
checks, each of which fails closed:

* the request has to be an exact truthy token, so a typo or a half-written
  value stops the mechanism instead of enabling it;
* a frozen build (the shipped EXE) never auto-approves, whatever the
  environment says, because there is no test to be running inside one;
* the process has to be in explicit test mode (``GISCODE_TEST_MODE``, the
  switch ``giscode.config.paths`` already owns and documents);
* test mode only counts while a test harness is actually running, so a
  developer's ordinary source run stays interactive even with test mode left
  in a shell profile.

Nothing here weakens the approval contract itself: ``GuardedWriter``,
``ProjectFileGuard`` and ``adapters._is_approved`` keep their existing rules.
Only the source of the flag changed, and a denied request simply leaves the
human dialog in place.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from giscode.config.paths import GISCODE_TEST_MODE_ENV, test_mode

#: The variable that asks for automatic approval of edits to existing files.
ENV = "GISCODE_AUTO_APPROVE_EDITS"
#: The variable a harness script sets to declare itself one (pytest sets its
#: own ``PYTEST_CURRENT_TEST`` marker, so a pytest run needs nothing extra).
HARNESS_ENV = "GISCODE_TEST_HARNESS"
#: pytest's own per-test marker: present only while a test is executing.
PYTEST_ENV = "PYTEST_CURRENT_TEST"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"", "0", "false", "no", "off"})


@dataclass(frozen=True)
class Decision:
    """Whether auto-approval is allowed, and the reason either way."""

    enabled: bool
    reason: str

    def __bool__(self) -> bool:  # lets ``if decision:`` read naturally
        return self.enabled


def _requested() -> str:
    return os.environ.get(ENV, "").strip()


def _frozen() -> bool:
    """True for a packaged build (PyInstaller sets both markers)."""
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def _harness_running() -> bool:
    if os.environ.get(HARNESS_ENV, "").strip().lower() in _TRUTHY:
        return True
    return PYTEST_ENV in os.environ or "pytest" in sys.modules


def decision() -> Decision:
    """The approval policy for this process, read on every call."""
    raw = _requested()
    lowered = raw.lower()
    if lowered in _FALSY:
        if not raw:
            return Decision(False, f"{ENV} is not set")
        return Decision(False, f"{ENV}={raw} turns auto-approval off")
    if lowered not in _TRUTHY:
        return Decision(
            False, f"{ENV}={raw} is not an accepted value (use 1/true/yes/on)"
        )
    if _frozen():
        return Decision(False, "a packaged build never auto-approves edits")
    if not test_mode():
        return Decision(
            False,
            f"{ENV} is honoured only in explicit test mode ({GISCODE_TEST_MODE_ENV})",
        )
    if not _harness_running():
        return Decision(
            False,
            f"{ENV} is honoured only while a test harness runs "
            f"({PYTEST_ENV} or {HARNESS_ENV})",
        )
    return Decision(True, "auto-approval enabled for this test process")


def enabled() -> bool:
    """True only for a test process that explicitly asked for it."""
    return decision().enabled
