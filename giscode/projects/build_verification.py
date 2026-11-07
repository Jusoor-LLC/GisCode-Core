"""Honest verification of a project whose coding-loop build just finished.

The build handoff runs the coding loop; its return value says the loop finished,
not that the project builds or that its tests pass. This module runs the
*existing* :class:`giscode.agentic.verifier.Verifier` against the project root
with the existing interpreter resolution and the shared
:class:`~giscode.agentic.verification_budget.VerificationBudget`, and reports
one explicit outcome instead of a claim.

Nothing here invents a pass. "No tests were run", "the build changed nothing",
"no interpreter was available" and "the tests failed" are all distinct, and only
``VERIFIED`` means the project's own checks really passed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Sequence

from giscode.agentic.test_runner import resolve_python
from giscode.agentic.verification_budget import VerificationBudget
from giscode.agentic.verifier import (
    TEST_STATUS_COULD_NOT_RUN,
    TEST_STATUS_FAILED,
    TEST_STATUS_NO_TESTS,
    TEST_STATUS_PASSED,
    TEST_STATUS_UNAVAILABLE,
    Verifier,
)


class BuildVerificationOutcome(str, Enum):
    """What the verification of a finished build actually established."""

    VERIFIED = "verified"
    TESTS_FAILED = "tests_failed"
    NO_TESTS = "no_tests_run"
    NO_CHANGES = "completed_no_changes"
    UNAVAILABLE = "unverified"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class BuildVerificationReport:
    """One verification attempt, with the evidence behind its verdict."""

    outcome: BuildVerificationOutcome
    message: str
    details: tuple = ()
    verification: dict = field(default_factory=dict)
    interpreter: Optional[str] = None
    duration_s: float = 0.0
    tests_run: bool = False

    @property
    def verified(self) -> bool:
        return self.outcome is BuildVerificationOutcome.VERIFIED

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "verified": self.verified,
            "message": self.message,
            "details": list(self.details),
            "verification": dict(self.verification),
            "interpreter": self.interpreter,
            "duration_s": self.duration_s,
            "tests_run": self.tests_run,
        }


def _as_dict(result) -> dict:
    """The verifier's own structured verdict, as a plain dict."""
    if isinstance(result, dict):
        return dict(result)
    try:
        from dataclasses import asdict

        return asdict(result)
    except Exception:  # noqa: BLE001 - a stub verdict still reports its fields
        return {
            key: getattr(result, key)
            for key in ("success", "errors", "details", "test_execution_status")
            if hasattr(result, key)
        }


def _timed_out(verification: dict) -> bool:
    """Whether the runner said this pass hit its time limit."""
    text = " ".join(
        str(item)
        for item in list(verification.get("errors") or ())
        + list(verification.get("details") or ())
    ).lower()
    return "timed out" in text


def _interpreter_for(root: Path) -> tuple[Optional[str], str]:
    """The interpreter the tests would use, and why, using the runner's rules."""
    try:
        resolution = resolve_python(root)
    except Exception as exc:  # noqa: BLE001 - reported as "unavailable"
        return None, f"interpreter resolution raised {type(exc).__name__}: {exc}"
    return getattr(resolution, "executable", None), getattr(resolution, "reason", "") or ""


def _default_verifier(root: Path) -> Verifier:
    from giscode.terminal.executor import CommandExecutor
    from giscode.workspace import Workspace

    return Verifier(root, terminal_executor=CommandExecutor(Workspace(root)))


def verify_completed_build(
    project_root,
    *,
    changed_files: Optional[Sequence[str]] = None,
    budget: Optional[VerificationBudget] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    verifier=None,
) -> BuildVerificationReport:
    """Run the project's own checks once and report what they proved.

    ``changed_files`` is the launcher's real answer: an empty list means the
    build touched nothing, so no test run is spent pretending it did. ``None``
    means unknown, which is not treated as an answer either way. A cancelled
    verification, an exhausted budget and a missing interpreter are all
    reported without running anything.
    """
    root = Path(project_root).resolve()
    started = time.monotonic()

    def _done(outcome: BuildVerificationOutcome, message: str, **kwargs):
        return BuildVerificationReport(
            outcome=outcome,
            message=message,
            duration_s=time.monotonic() - started,
            **kwargs,
        )

    def _cancelled() -> bool:
        if should_cancel is None:
            return False
        try:
            return bool(should_cancel())
        except Exception:  # noqa: BLE001 - an unreadable flag is not a stop
            return False

    if _cancelled():
        return _done(
            BuildVerificationOutcome.CANCELLED,
            "Project verification was cancelled before any test ran; no verdict "
            "was reached.",
        )

    if not root.is_dir():
        return _done(
            BuildVerificationOutcome.UNAVAILABLE,
            f"Project verification could not run: {root} is not a directory. "
            "Success is not claimed.",
        )

    if changed_files is not None and len(list(changed_files)) == 0:
        return _done(
            BuildVerificationOutcome.NO_CHANGES,
            "Build completed with no changes inside the project root. No tests "
            "were run, because an unchanged project proves nothing about this "
            "build; success is not claimed.",
        )

    executable, reason = _interpreter_for(root)
    if not executable:
        return _done(
            BuildVerificationOutcome.UNAVAILABLE,
            "Project verification could not run: " + reason
            + " Success is not claimed.",
            interpreter=None,
        )

    budget = budget or VerificationBudget(max_repair_attempts=0)
    if budget.exhausted():
        return _done(
            BuildVerificationOutcome.UNAVAILABLE,
            "Project verification did not start: the shared verification budget "
            "is exhausted. Success is not claimed.",
            interpreter=executable,
        )

    try:
        verifier = verifier or _default_verifier(root)
    except Exception as exc:  # noqa: BLE001 - reported, never guessed
        return _done(
            BuildVerificationOutcome.ERROR,
            "Project verification could not be set up: "
            f"{type(exc).__name__}: {exc}. Success is not claimed.",
            interpreter=executable,
        )

    pass_started = budget.start_pass()
    try:
        result = verifier.verify_project(budget_s=budget.remaining())
    except Exception as exc:  # noqa: BLE001 - a crash is not a pass
        return _done(
            BuildVerificationOutcome.ERROR,
            "Project verification raised "
            f"{type(exc).__name__}: {exc}. Success is not claimed.",
            interpreter=executable,
        )
    finally:
        budget.finish_pass(pass_started)

    verification = _as_dict(result)
    details = tuple(str(item) for item in (verification.get("details") or ()))
    errors = [str(item) for item in (verification.get("errors") or ())]
    status = str(verification.get("test_execution_status") or "")
    success = bool(verification.get("success"))
    first_error = errors[0] if errors else "the verifier reported no detail"

    if _cancelled():
        return _done(
            BuildVerificationOutcome.CANCELLED,
            "Project verification was cancelled; the run's result was discarded "
            "and no verdict is claimed.",
            details=details,
            verification=verification,
            interpreter=executable,
            tests_run=True,
        )

    if status == TEST_STATUS_PASSED and success:
        return _done(
            BuildVerificationOutcome.VERIFIED,
            "Project verification ran the project's own tests and they passed.",
            details=details,
            verification=verification,
            interpreter=executable,
            tests_run=True,
        )
    if status == TEST_STATUS_FAILED:
        return _done(
            BuildVerificationOutcome.TESTS_FAILED,
            "Project verification ran the project's own tests and they failed: "
            + first_error,
            details=details,
            verification=verification,
            interpreter=executable,
            tests_run=True,
        )
    if status == TEST_STATUS_NO_TESTS:
        return _done(
            BuildVerificationOutcome.NO_TESTS,
            "No project tests were found, so no test was executed: "
            + first_error,
            details=details,
            verification=verification,
            interpreter=executable,
            tests_run=False,
        )
    if _timed_out(verification):
        return _done(
            BuildVerificationOutcome.TIMEOUT,
            "Project verification timed out before reaching a verdict: "
            + first_error,
            details=details,
            verification=verification,
            interpreter=executable,
            tests_run=True,
        )
    if status in {TEST_STATUS_UNAVAILABLE, TEST_STATUS_COULD_NOT_RUN}:
        return _done(
            BuildVerificationOutcome.UNAVAILABLE,
            "Project verification could not establish success: " + first_error,
            details=details,
            verification=verification,
            interpreter=executable,
            tests_run=True,
        )
    return _done(
        BuildVerificationOutcome.UNAVAILABLE,
        "Project verification returned no usable verdict (" + (status or "no status")
        + "): " + first_error,
        details=details,
        verification=verification,
        interpreter=executable,
        tests_run=True,
    )
