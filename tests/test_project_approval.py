"""Tests for the APPROVE & START bridge (Task 9).

These tests use a temporary ``ProjectStore`` SQLite file and never
touch the user's real ``~/.giscode/projects.db``.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterator

import pytest

from giscode.planer import plan_to_project_dict
from giscode.planer.engine import PlanerEngine
from giscode.projects import Project, ProjectLifecycle, ProjectStore
from giscode.projects.approval import (
    ApprovalError,
    ApprovalInProgressError,
    ApprovalTransitionFailedError,
    BuildHandoff,
    ProjectInWrongLifecycleError,
    ProjectNotFoundApprovalError,
    ProjectNotReadyForApprovalError,
    approve_and_start,
    clear_in_flight_registry,
)


# ----------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _reset_in_flight():
    """Ensure the in-flight approval registry is empty between
    tests so concurrency tests are isolated.
    """
    clear_in_flight_registry()
    yield
    clear_in_flight_registry()


@pytest.fixture
def project_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "projects.db")


@pytest.fixture
def project_store(project_db_path: str) -> Iterator[ProjectStore]:
    s = ProjectStore(db_path=project_db_path)
    yield s
    s.close()


@pytest.fixture
def project(project_store: ProjectStore) -> Project:
    return project_store.create_project(
        Project.create(name="Demo", root_path="C:/projects/demo")
    )


def _make_ready_plan(project_id: str) -> dict:
    """Build a Planer engine, drive it to a ready state, and
    return its serialised plan dict.
    """
    e = PlanerEngine(project_id=project_id)
    e.start_with_idea("Build a small todo app with care")
    for _ in range(15):
        qs = [q for q in e.plan.open_questions if not q.answered]
        if not qs:
            break
        for q in qs:
            e.answer_question(q.id, "a-" + q.id)
    e.set_application_type("web app")
    for n in "abc":
        e.add_feature(n)
    for n in "xy":
        e.add_component(n)
    for n in "s1s2s3":
        e.add_screen(n)
    for n in "m1m2m3":
        e.add_roadmap_item(n)
    e.set_branding("br")
    assert e.is_ready_for_review()
    return plan_to_project_dict(e.build_plan())


# ----------------------------------------------------------------- 1. nonexistent


def test_cannot_approve_nonexistent_project(project_store):
    with pytest.raises(ProjectNotFoundApprovalError):
        approve_and_start(project_store, "proj-does-not-exist")


# ----------------------------------------------------------------- 2. not ready


def test_cannot_approve_project_without_ready_plan(project_store, project):
    with pytest.raises(ProjectNotReadyForApprovalError):
        approve_and_start(project_store, project.id)


def test_cannot_approve_project_with_empty_plan_dict(project_store, project):
    project_store.set_plan(project.id, {"plan": {}})
    with pytest.raises(ProjectNotReadyForApprovalError):
        approve_and_start(project_store, project.id)


# ----------------------------------------------------------------- 3 / 4. explicit success


def test_explicit_approval_succeeds(project_store, project):
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    handoff = approve_and_start(project_store, project.id)
    assert isinstance(handoff, BuildHandoff)
    assert handoff.project_id == project.id
    assert handoff.lifecycle == ProjectLifecycle.BUILDING
    assert handoff.plan
    assert handoff.approved_at
    assert handoff.started_at


def test_approval_changes_planning_to_building(project_store, project):
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    approve_and_start(project_store, project.id)
    final = project_store.get_project(project.id)
    assert final.lifecycle == ProjectLifecycle.BUILDING


def test_approval_from_review_state_works(project_store, project):
    """A project already in REVIEW can be approved; the bridge
    must NOT demote it to PLANNING first.
    """
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    project_store.transition_lifecycle(project.id, ProjectLifecycle.REVIEW)
    assert project_store.get_project(project.id).lifecycle == ProjectLifecycle.REVIEW
    approve_and_start(project_store, project.id)
    final = project_store.get_project(project.id)
    assert final.lifecycle == ProjectLifecycle.BUILDING


# ----------------------------------------------------------------- 5. start_building requires APPROVED


def test_start_building_directly_requires_approved(project_store, project):
    """Sanity check that the existing ProjectStore still enforces
    its own rules: BUILDING is unreachable from PLANNING directly.
    """
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    with pytest.raises(Exception):
        project_store.start_building(project.id)
    # And the project did not move.
    assert project_store.get_project(project.id).lifecycle == ProjectLifecycle.PLANNING


def test_approved_to_building_succeeds(project_store, project):
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    approve_and_start(project_store, project.id)
    assert project_store.get_project(project.id).lifecycle == ProjectLifecycle.BUILDING


# ----------------------------------------------------------------- 6. readiness does not auto-approve


def test_readiness_check_does_not_auto_approve(project_store, project):
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    # Even with a fully ready plan, nothing in the Planer side or
    # the readiness check should move the project.
    assert project_store.get_project(project.id).lifecycle == ProjectLifecycle.PLANNING
    # And the bridge's own readiness probe (via is_plan_ready_for_review
    # which it uses internally) is a *read*, not a state change.
    from giscode.planer.plan import is_plan_ready_for_review
    from giscode.planer import plan_from_project_dict
    assert is_plan_ready_for_review(
        plan_from_project_dict(project_store.get_project(project.id).plan)
    ) is True
    assert project_store.get_project(project.id).lifecycle == ProjectLifecycle.PLANNING


def test_planer_chat_cannot_approve(project_store, project, tmp_path):
    """A Planer chat interaction (which is the user driving the
    Planer) must not be able to cross the approval boundary.
    Driving the Planer to a ready state and saving the plan is
    still not approval.
    """
    from giscode.storage import ChatStore
    from giscode.projects.workspace import ProjectWorkspaceSession

    chat_db = str(tmp_path / "chats.db")
    chat_store = ChatStore(db_path=chat_db)
    try:
        with ProjectWorkspaceSession.open(
            project_store, chat_store, project.id
        ) as ws:
            ws.engine.start_with_idea("Build a small todo app with care")
            for _ in range(15):
                qs = [q for q in ws.engine.plan.open_questions if not q.answered]
                if not qs:
                    break
                for q in qs:
                    ws.engine.answer_question(q.id, "a-" + q.id)
            ws.engine.set_application_type("web app")
            for n in "abc":
                ws.engine.add_feature(n)
            for n in "xy":
                ws.engine.add_component(n)
            for n in "s1s2s3":
                ws.engine.add_screen(n)
            for n in "m1m2m3":
                ws.engine.add_roadmap_item(n)
            ws.engine.set_branding("br")
            ws.chat_with_planer("ping the planer")
            ws.save_plan()
            assert ws.is_ready_for_review()
            # After all of that, the project is still PLANNING.
            assert project_store.get_project(project.id).lifecycle == ProjectLifecycle.PLANNING
    finally:
        chat_store.close()
    # And the bridge still requires an explicit call.
    handoff = approve_and_start(project_store, project.id)
    assert handoff.lifecycle == ProjectLifecycle.BUILDING


# ----------------------------------------------------------------- 7. concurrency


def test_concurrent_approve_and_start_allows_only_one_success(
    project_store, project
):
    project_store.set_plan(project.id, _make_ready_plan(project.id))

    successes: list[BuildHandoff] = []
    errors: list[Exception] = []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            handoff = approve_and_start(project_store, project.id)
            with lock:
                successes.append(handoff)
        except ApprovalInProgressError as exc:
            with lock:
                errors.append(exc)
        except Exception as exc:  # pragma: no cover - unexpected
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one success, the rest were rejected cleanly.
    assert len(successes) == 1
    # The rest are either ApprovalInProgressError (rejected during
    # in-flight) or, if they ran after the winner cleared, they
    # must have hit ProjectInWrongLifecycleError (project already
    # BUILDING).
    assert len(errors) == 4
    for e in errors:
        assert isinstance(e, (ApprovalInProgressError, ProjectInWrongLifecycleError))
    final = project_store.get_project(project.id)
    assert final.lifecycle == ProjectLifecycle.BUILDING


def test_a_caller_holding_a_stale_snapshot_is_rejected_cleanly(
    project_store, project, monkeypatch
):
    """Two overlapping approvals: the loser is rejected at the claim.

    The interleaving is forced rather than left to luck. The second caller
    reads the PLANNING project and is then held until the first caller has
    finished approving. If the project is only claimed after that read, the
    second caller claims a project that is already free again and re-runs the
    PLANNING -> REVIEW transition from a snapshot that no longer describes it,
    which surfaces as ApprovalTransitionFailedError instead of a clean
    rejection.
    """
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    first_read = threading.Event()
    winner_done = threading.Event()
    real_get_project = project_store.get_project

    def blocking_get_project(project_id: str):
        # Capture what this caller believes, then let the other caller win.
        snapshot = real_get_project(project_id)
        if threading.current_thread().name == "late-caller":
            first_read.set()
            winner_done.wait(timeout=5)
        return snapshot

    monkeypatch.setattr(project_store, "get_project", blocking_get_project)

    outcome: dict[str, object] = {}

    def late_caller() -> None:
        try:
            outcome["handoff"] = approve_and_start(project_store, project.id)
        except BaseException as exc:  # noqa: BLE001 - the assertion names it
            outcome["error"] = exc

    late = threading.Thread(target=late_caller, name="late-caller", daemon=True)
    late.start()
    assert first_read.wait(timeout=5), "the late caller never read the project"
    try:
        outcome["first_handoff"] = approve_and_start(project_store, project.id)
    except BaseException as exc:  # noqa: BLE001 - the assertion names it
        outcome["first_error"] = exc
    finally:
        winner_done.set()
    late.join(timeout=5)
    assert not late.is_alive(), "the late caller never finished"

    successes = [value for key, value in outcome.items() if key.endswith("handoff")]
    rejections = [
        value
        for key, value in outcome.items()
        if key.endswith("error") and value is not None
    ]
    assert len(successes) == 1, f"expected exactly one success, got {outcome}"
    assert len(rejections) == 1, f"expected exactly one rejection, got {outcome}"
    assert isinstance(rejections[0], ApprovalInProgressError), (
        "the loser raised "
        f"{type(rejections[0]).__name__}, not a clean ApprovalInProgressError"
    )
    assert project_store.get_project(project.id).lifecycle == ProjectLifecycle.BUILDING


# ----------------------------------------------------------------- 8. failure handling


def test_failure_during_approval_is_handled(project_store, project, monkeypatch):
    project_store.set_plan(project.id, _make_ready_plan(project.id))

    real_transition = project_store.transition_lifecycle

    def boom(pid, target):
        if target == ProjectLifecycle.APPROVED:
            raise RuntimeError("simulated approve failure")
        return real_transition(pid, target)

    monkeypatch.setattr(project_store, "transition_lifecycle", boom)

    with pytest.raises(ApprovalTransitionFailedError):
        approve_and_start(project_store, project.id)
    # Project must not be in BUILDING.
    final = project_store.get_project(project.id)
    assert final.lifecycle != ProjectLifecycle.BUILDING


def test_failure_during_start_building_is_handled(
    project_store, project, monkeypatch
):
    project_store.set_plan(project.id, _make_ready_plan(project.id))

    real_transition = project_store.transition_lifecycle

    def boom(pid, target):
        if target == ProjectLifecycle.BUILDING:
            raise RuntimeError("simulated start_building failure")
        return real_transition(pid, target)

    monkeypatch.setattr(project_store, "transition_lifecycle", boom)

    with pytest.raises(ApprovalTransitionFailedError):
        approve_and_start(project_store, project.id)
    # The project is left in APPROVED (the explicit user
    # decision is preserved). It is NOT moved on to BUILDING.
    final = project_store.get_project(project.id)
    assert final.lifecycle == ProjectLifecycle.APPROVED
    # And the in-flight registry was cleared, so a fresh attempt
    # does not see a stale in-flight entry. A fresh attempt will
    # instead see the project in APPROVED, which is also not
    # legal for approval (we only approve from PLANNING/REVIEW),
    # so it must raise ProjectInWrongLifecycleError.
    with pytest.raises(ProjectInWrongLifecycleError):
        approve_and_start(project_store, project.id)


# ----------------------------------------------------------------- 9. no duplicate build handoff


def test_no_duplicate_build_handoff(project_store, project):
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    h1 = approve_and_start(project_store, project.id)
    # A second attempt must be rejected because the project is
    # now in BUILDING.
    with pytest.raises(ProjectInWrongLifecycleError):
        approve_and_start(project_store, project.id)
    # Exactly one handoff was produced.
    assert isinstance(h1, BuildHandoff)


# ----------------------------------------------------------------- 10. lifecycle rules intact


def test_existing_lifecycle_rules_remain_intact(project_store, project):
    # The bridge did not weaken any existing transition.
    # PLANNING -> BUILDING is still illegal via the store.
    with pytest.raises(Exception):
        project_store.transition_lifecycle(project.id, ProjectLifecycle.BUILDING)
    # REVIEW -> BUILDING is still illegal via the store.
    project_store.transition_lifecycle(project.id, ProjectLifecycle.REVIEW)
    with pytest.raises(Exception):
        project_store.transition_lifecycle(project.id, ProjectLifecycle.BUILDING)


# ----------------------------------------------------------------- 11. existing pipeline not rewritten


def test_existing_coding_pipeline_is_not_rewritten():
    """The bridge must not import the coding pipeline. The future
    orchestrator builds the loop from the handoff fields.
    """
    import giscode.projects.approval as approval_mod

    forbidden = {
        "CodingAgentLoop",
        "OpenHandsAdapter",
        "BuildTestOrchestrator",
        "CodingAgent",
        "execute",
    }
    exposed = set(dir(approval_mod))
    for name in forbidden:
        assert name not in exposed, f"approval module exposes {name}"


def test_handoff_is_a_pure_descriptor(project_store, project):
    """The handoff has only data, not references to live agents.
    """
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    h = approve_and_start(project_store, project.id)
    d = h.to_dict()
    assert d["project_id"] == project.id
    assert d["lifecycle"] == "BUILDING"
    assert "plan" in d and d["plan"]
    # No runtime objects in the descriptor.
    for v in d.values():
        assert not callable(v)


# ----------------------------------------------------------------- 12. notes are optional


def test_notes_are_optional(project_store, project):
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    h1 = approve_and_start(project_store, project.id, notes=["user said go"])
    assert "user said go" in h1.notes


# ----------------------------------------------------------------- 13. already approved cannot be re-approved


def test_cannot_approve_already_building_project(project_store, project):
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    approve_and_start(project_store, project.id)
    with pytest.raises(ProjectInWrongLifecycleError):
        approve_and_start(project_store, project.id)


def test_cannot_approve_failed_project(project_store, project):
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    project_store.transition_lifecycle(project.id, ProjectLifecycle.FAILED)
    with pytest.raises(ProjectInWrongLifecycleError):
        approve_and_start(project_store, project.id)


def test_cannot_approve_cancelled_project(project_store, project):
    project_store.set_plan(project.id, _make_ready_plan(project.id))
    project_store.transition_lifecycle(project.id, ProjectLifecycle.CANCELLED)
    with pytest.raises(ProjectInWrongLifecycleError):
        approve_and_start(project_store, project.id)


# ----------------------------------------------------------------- 14. validation


def test_rejects_empty_project_id(project_store):
    with pytest.raises(ApprovalError):
        approve_and_start(project_store, "")


def test_rejects_none_project_store():
    with pytest.raises(ApprovalError):
        approve_and_start(None, "proj-x")
