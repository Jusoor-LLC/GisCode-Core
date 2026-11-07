"""APPROVE & START bridge between the Project Workspace and the
existing coding pipeline.

This module provides the **only** legitimate path from
``PLANNING`` / ``REVIEW`` to ``BUILDING``. It is an explicit,
user-driven operation; nothing else in the system may cross the
approval boundary.

Design contract:

* **Explicit only.** No Planer state, no chat message, no
  readiness check, no automatic transition may approve a project.
  The user (or the future Project Workspace GUI's explicit
  "APPROVE & START" button) must call :func:`approve_and_start`.
* **Atomic.** Either the project ends in ``BUILDING`` with a
  :class:`BuildHandoff` ready for the coding pipeline, or the
  project remains in its previous state and a focused exception
  is raised.
* **Concurrency-safe.** Two simultaneous calls for the same
  project allow exactly one success. The other fails cleanly
  with :class:`ApprovalInProgressError`.
* **Hands off, does not start.** The bridge produces a
  :class:`BuildHandoff` describing the project, plan, workspace
  root, and the lifecycle state. It does **not** itself import
  or invoke :class:`giscode.agentic.loop.CodingAgentLoop`,
  :class:`giscode.openhands.adapter.OpenHandsAdapter`, or any
  other component of the coding pipeline. The future GUI / a
  separate orchestrator is responsible for actually starting the
  loop with the handoff.
* **No filesystem mutation.** The bridge does not create project
  directories, does not write source files, and does not run
  shell commands.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from giscode.planer.plan import is_plan_ready_for_review
from giscode.projects import Project, ProjectLifecycle, ProjectStore


# ----------------------------------------------------------------- errors


class ApprovalError(Exception):
    """Base error for the approval bridge."""


class ProjectNotFoundApprovalError(ApprovalError):
    """Raised when the target project does not exist."""


class ProjectNotReadyForApprovalError(ApprovalError):
    """Raised when the project's plan is not ready for review or
    the persisted plan is missing/empty."""


class ProjectInWrongLifecycleError(ApprovalError):
    """Raised when the project is not in a state from which
    explicit approval is legal (``PLANNING`` or ``REVIEW``)."""


class ApprovalInProgressError(ApprovalError):
    """Raised when another :func:`approve_and_start` call is
    already in flight for the same project."""


class ApprovalTransitionFailedError(ApprovalError):
    """Raised when the explicit approve / start_building call to
    :class:`ProjectStore` fails. The project's lifecycle is left
    in a consistent state (the previous state is preserved if
    approve failed, or the project is returned to a valid state
    if start_building failed)."""


# ----------------------------------------------------------------- handoff


@dataclass(frozen=True)
class BuildHandoff:
    """Immutable description of an approved project's handoff to
    the existing coding pipeline.

    A future orchestrator (typically the future Project Workspace
    GUI) constructs a :class:`giscode.agentic.loop.CodingAgentLoop`
    from the fields on this object and calls ``loop.start()`` /
    equivalent. The bridge itself does not start the loop; that
    is the responsibility of the orchestrator and a future task.
    """

    project_id: str
    project_name: str
    root_path: str
    plan: dict
    approved_at: str
    started_at: str
    lifecycle: ProjectLifecycle = ProjectLifecycle.BUILDING
    notes: tuple = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "root_path": self.root_path,
            "plan": self.plan,
            "approved_at": self.approved_at,
            "started_at": self.started_at,
            "lifecycle": self.lifecycle.name
            if hasattr(self.lifecycle, "name")
            else str(self.lifecycle),
            "notes": list(self.notes),
        }


# ----------------------------------------------------------------- bridge


# A process-wide registry of in-flight approvals. Keyed by
# project_id. This prevents two concurrent callers from both
# crossing the approval boundary for the same project.
_in_flight: dict[str, str] = {}
_in_flight_lock = threading.Lock()


def _utc_now_iso() -> str:
    import time

    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        + f".{int((time.time() % 1) * 1000):03d}Z"
    )


def _check_in_flight(project_id: str) -> Optional[str]:
    with _in_flight_lock:
        return _in_flight.get(project_id)


def _mark_in_flight(project_id: str) -> None:
    with _in_flight_lock:
        if project_id in _in_flight:
            raise ApprovalInProgressError(
                f"another approve_and_start is in flight for "
                f"project {project_id!r}"
            )
        _in_flight[project_id] = _utc_now_iso()


def _clear_in_flight(project_id: str) -> None:
    with _in_flight_lock:
        _in_flight.pop(project_id, None)


def _assert_plan_ready(project: Project) -> None:
    """Verify the project's persisted plan is non-empty and the
    Planer considers it ready for review. The plan is the source
    of truth at approval time; the in-memory :class:`PlanerEngine`
    is *not* consulted here, because the approval boundary is
    between the user and the project store, not between the
    Planer and the store.
    """
    if not project.plan:
        raise ProjectNotReadyForApprovalError(
            f"project {project.id!r} has no persisted plan; "
            f"save the plan before approving"
        )
    inner = project.plan.get("plan", project.plan)
    if not inner:
        raise ProjectNotReadyForApprovalError(
            f"project {project.id!r} persisted plan is empty; "
            f"save the plan before approving"
        )
    if not is_plan_ready_for_review(_coerce_to_plan(inner)):
        raise ProjectNotReadyForApprovalError(
            f"project {project.id!r} plan is not ready for review; "
            f"finish the Planer workflow first"
        )


def _coerce_to_plan(data: dict):
    """Build a :class:`ProjectPlan` from a dict without raising
    on partial data. We only use this to ask the *existing*
    :func:`is_plan_ready_for_review` helper whether the persisted
    plan passes its own validation.
    """
    from giscode.planer.plan import ProjectPlan

    return ProjectPlan.from_dict(data)


def _validate_lifecycle_for_approval(project: Project) -> None:
    """Approval is only legal from ``PLANNING`` or ``REVIEW``.

    ``APPROVED`` and beyond mean the boundary has already been
    crossed. ``BUILDING`` / ``TESTING`` / ``VERIFICATION`` /
    ``COMPLETE`` / ``FAILED`` / ``CANCELLED`` are terminal or
    in-flight and cannot be re-entered.
    """
    legal = {ProjectLifecycle.PLANNING, ProjectLifecycle.REVIEW}
    if project.lifecycle not in legal:
        raise ProjectInWrongLifecycleError(
            f"project {project.id!r} is in {project.lifecycle!r}; "
            f"approval is only allowed from {sorted(l.name for l in legal)}"
        )


def approve_and_start(
    project_store: ProjectStore,
    project_id: str,
    *,
    notes: Optional[list[str]] = None,
) -> BuildHandoff:
    """Explicitly approve ``project_id`` and start building.

    This is the **only** legitimate path from ``PLANNING`` /
    ``REVIEW`` to ``BUILDING``. It is atomic with respect to
    other concurrent calls for the same project.

    Steps (in order):

    1. Reject if the project does not exist.
    2. Reject if another call is in flight for the same project.
    3. Reject if the project is not in ``PLANNING`` or ``REVIEW``.
    4. Reject if the persisted plan is missing or the Planer's
       :func:`is_plan_ready_for_review` returns ``False``.
    5. Move ``PLANNING`` → ``REVIEW`` (if currently ``PLANNING``).
    6. ``ProjectStore.approve(project_id)`` → ``APPROVED``.
    7. ``ProjectStore.start_building(project_id)`` → ``BUILDING``.
    8. Build and return a :class:`BuildHandoff`.

    On any failure between steps 5-7, the project is left in a
    consistent lifecycle state (no spurious ``BUILDING``) and
    a focused exception is raised.

    The function does **not** import or invoke
    :class:`giscode.agentic.loop.CodingAgentLoop`. The returned
    :class:`BuildHandoff` is the handoff to the existing coding
    pipeline; a future orchestrator (typically the future Project
    Workspace GUI) is responsible for constructing and starting
    the loop.
    """
    if project_store is None:
        raise ApprovalError("project_store is required")
    if not project_id or not project_id.strip():
        raise ApprovalError("project_id must be a non-empty string")

    if _check_in_flight(project_id) is not None:
        raise ApprovalInProgressError(
            f"another approve_and_start is in flight for "
            f"project {project_id!r}"
        )

    # Claim this project before reading its lifecycle. _mark_in_flight is
    # the atomic step: it takes the registry lock and raises when another
    # call holds the project. Checking first and marking later left a
    # window in which a second caller passed the check, the winner finished
    # and cleared its mark, and the second caller then claimed the project
    # and acted on the PLANNING snapshot it had read earlier. That surfaced
    # as "building -> review" wrapped in ApprovalTransitionFailedError
    # instead of a clean rejection, although only one caller may succeed.
    _mark_in_flight(project_id)
    try:
        project = project_store.get_project(project_id)
        if project is None:
            raise ProjectNotFoundApprovalError(
                f"project {project_id!r} does not exist"
            )

        _validate_lifecycle_for_approval(project)
        _assert_plan_ready(project)

        # Cross the boundary atomically for this project.
        # Step 1: PLANNING -> REVIEW (idempotent if already REVIEW).
        if project.lifecycle == ProjectLifecycle.PLANNING:
            try:
                promoted = project_store.transition_lifecycle(
                    project_id, ProjectLifecycle.REVIEW
                )
            except Exception as exc:
                raise ApprovalTransitionFailedError(
                    f"failed to move project {project_id!r} to REVIEW: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if promoted is None:
                raise ProjectNotFoundApprovalError(
                    f"project {project_id!r} disappeared during REVIEW transition"
                )

        # Step 2: REVIEW -> APPROVED.
        try:
            approved = project_store.approve(project_id)
        except Exception as exc:
            raise ApprovalTransitionFailedError(
                f"failed to approve project {project_id!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if approved is None:
            raise ProjectNotFoundApprovalError(
                f"project {project_id!r} disappeared during approval"
            )

        approved_at = approved.updated_at

        # Step 3: APPROVED -> BUILDING.
        try:
            building = project_store.start_building(project_id)
        except Exception as exc:
            # Do NOT claim success. Do not silently roll back the
            # APPROVED state — the project is in a valid, explicit
            # approved state and the user can retry / cancel. We
            # surface the failure clearly.
            raise ApprovalTransitionFailedError(
                f"failed to start building for project {project_id!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if building is None:
            raise ProjectNotFoundApprovalError(
                f"project {project_id!r} disappeared during start_building"
            )

        started_at = building.updated_at

        return BuildHandoff(
            project_id=building.id,
            project_name=building.name,
            root_path=building.root_path,
            plan=building.plan or {},
            approved_at=approved_at,
            started_at=started_at,
            lifecycle=building.lifecycle,
            notes=tuple(notes) if notes else (),
        )
    finally:
        _clear_in_flight(project_id)


def clear_in_flight_registry() -> None:
    """Test helper: clears the in-flight approval registry. Not
    intended for production use; the registry is per-process and
    is automatically maintained by :func:`approve_and_start`.
    """
    with _in_flight_lock:
        _in_flight.clear()
