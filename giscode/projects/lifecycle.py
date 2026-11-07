"""Project lifecycle enumeration and transition rules.

The lifecycle is the single source of truth used by :class:`ProjectStore`
and (later) the Planer / Project Workspace GUI. It is intentionally
declared once and re-exported so that the same enum drives every
layer of the system.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet


class ProjectLifecycle(str, Enum):
    PLANNING = "planning"
    REVIEW = "review"
    APPROVED = "approved"
    BUILDING = "building"
    TESTING = "testing"
    VERIFICATION = "verification"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LifecycleError(ValueError):
    """Raised when an invalid lifecycle transition is requested."""


# Forward-progress transitions along the happy path.
# Backward transitions are explicit user actions (e.g. Edit Plan).
HAPPY_PATH: tuple = (
    ProjectLifecycle.PLANNING,
    ProjectLifecycle.REVIEW,
    ProjectLifecycle.APPROVED,
    ProjectLifecycle.BUILDING,
    ProjectLifecycle.TESTING,
    ProjectLifecycle.VERIFICATION,
    ProjectLifecycle.COMPLETE,
)

# Cancellation is allowed from any non-terminal state.
# Failure is allowed from any active state (PLANNING..VERIFICATION).
# Going back from REVIEW to PLANNING is the "Edit Plan" flow.
# APPROVED → BUILDING is *only* the entry into execution; it is
# deliberately not automatic.
ALLOWED_TRANSITIONS: Dict[ProjectLifecycle, FrozenSet[ProjectLifecycle]] = {
    ProjectLifecycle.PLANNING: frozenset({
        ProjectLifecycle.REVIEW,
        ProjectLifecycle.FAILED,
        ProjectLifecycle.CANCELLED,
    }),
    ProjectLifecycle.REVIEW: frozenset({
        ProjectLifecycle.PLANNING,  # "Edit Plan"
        ProjectLifecycle.APPROVED,
        ProjectLifecycle.FAILED,
        ProjectLifecycle.CANCELLED,
    }),
    ProjectLifecycle.APPROVED: frozenset({
        ProjectLifecycle.BUILDING,
        # Explicit retry only: an approved build that never actually started
        # (start_building failed, or the launch was refused) goes back to REVIEW
        # so the user can approve it again. Nothing moves APPROVED -> REVIEW on
        # its own.
        ProjectLifecycle.REVIEW,
        ProjectLifecycle.CANCELLED,
        ProjectLifecycle.FAILED,
    }),
    ProjectLifecycle.BUILDING: frozenset({
        ProjectLifecycle.TESTING,
        ProjectLifecycle.FAILED,
        ProjectLifecycle.CANCELLED,
    }),
    ProjectLifecycle.TESTING: frozenset({
        ProjectLifecycle.VERIFICATION,
        ProjectLifecycle.FAILED,
        ProjectLifecycle.CANCELLED,
    }),
    ProjectLifecycle.VERIFICATION: frozenset({
        ProjectLifecycle.COMPLETE,
        ProjectLifecycle.FAILED,
        ProjectLifecycle.CANCELLED,
    }),
    # FINISHED states — no automatic transitions out.
    #
    # FAILED keeps exactly one explicit exit: back to REVIEW for a retry. It is
    # deliberately not part of TERMINAL_STATES, because "terminal" means "no
    # action can reopen it", and a failed build must be retryable. COMPLETE and
    # CANCELLED really are terminal: neither is ever reopened.
    ProjectLifecycle.COMPLETE: frozenset(),
    ProjectLifecycle.FAILED: frozenset({
        ProjectLifecycle.REVIEW,  # explicit retry (ProjectStore.retry)
    }),
    ProjectLifecycle.CANCELLED: frozenset(),
}


TERMINAL_STATES: FrozenSet[ProjectLifecycle] = frozenset({
    ProjectLifecycle.COMPLETE,
    ProjectLifecycle.CANCELLED,
})

# States an explicit retry may reopen. REVIEW is where approval starts again, so
# a retry never bypasses the approval boundary.
RETRYABLE_STATES: FrozenSet[ProjectLifecycle] = frozenset({
    ProjectLifecycle.FAILED,
    ProjectLifecycle.APPROVED,
})


def can_transition(current: ProjectLifecycle, target: ProjectLifecycle) -> bool:
    if not isinstance(current, ProjectLifecycle):
        raise TypeError(f"current must be a ProjectLifecycle, got {type(current).__name__}")
    if not isinstance(target, ProjectLifecycle):
        raise TypeError(f"target must be a ProjectLifecycle, got {type(target).__name__}")
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def assert_transition(current: ProjectLifecycle, target: ProjectLifecycle) -> None:
    if not can_transition(current, target):
        allowed = ", ".join(s.value for s in sorted(ALLOWED_TRANSITIONS.get(current, frozenset()), key=lambda x: x.value))
        raise LifecycleError(
            f"Invalid lifecycle transition: {current.value} -> {target.value}. "
            f"Allowed from {current.value}: [{allowed or 'none — terminal state'}]"
        )
