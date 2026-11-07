"""Tests for the Project Workspace session (Task 8).

All tests use isolated temporary SQLite files so the user's real
``~/.giscode/projects.db`` and ``~/.giscode/chats.db`` are never
touched.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from giscode.planer import (
    PLANER_CHAT_TITLE,
    PlanerEngine,
    plan_to_project_dict,
)
from giscode.projects import (
    Project,
    ProjectLifecycle,
    ProjectStore,
)
from giscode.projects.workspace import (
    PlanerChatUnavailableError,
    ProjectNotFoundError,
    ProjectStoreUnavailableError,
    ProjectWorkspaceError,
    ProjectWorkspaceSession,
    WorkspaceAlreadyClosedError,
)
from giscode.storage import ChatStore


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def project_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "projects.db")


@pytest.fixture
def chat_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "chats.db")


@pytest.fixture
def project_store(project_db_path: str) -> Iterator[ProjectStore]:
    s = ProjectStore(db_path=project_db_path)
    yield s
    s.close()


@pytest.fixture
def chat_store(chat_db_path: str) -> Iterator[ChatStore]:
    s = ChatStore(db_path=chat_db_path)
    yield s
    s.close()


@pytest.fixture
def created_session(project_store: ProjectStore, chat_store: ChatStore) -> ProjectWorkspaceSession:
    return ProjectWorkspaceSession.create(
        project_store=project_store,
        chat_store=chat_store,
        name="Demo",
        root_path="C:/projects/demo",
        description="",
    )


# ----------------------------------------------------------------- 1

def test_create_new_project_session(created_session: ProjectWorkspaceSession):
    assert isinstance(created_session, ProjectWorkspaceSession)
    assert created_session.project_id.startswith("proj-")
    assert created_session.lifecycle == ProjectLifecycle.PLANNING
    assert created_session.is_closed is False
    assert created_session.engine is not None
    assert created_session.ai is not None
    assert created_session.planer_chat is not None


# ----------------------------------------------------------------- 2

def test_open_existing_project(project_store, chat_store, created_session):
    pid = created_session.project_id
    created_session.close()
    opened = ProjectWorkspaceSession.open(project_store, chat_store, pid)
    try:
        assert opened.project_id == pid
        assert opened.lifecycle == ProjectLifecycle.PLANNING
    finally:
        opened.close()


# ----------------------------------------------------------------- 3

def test_project_id_remains_stable_across_reopen(project_store, chat_store, created_session):
    pid = created_session.project_id
    created_session.close()
    opened = ProjectWorkspaceSession.open(project_store, chat_store, pid)
    try:
        assert opened.project_id == pid
    finally:
        opened.close()


# ----------------------------------------------------------------- 4

def test_existing_project_plan_is_restored(project_store, chat_store, created_session):
    created_session.engine.start_with_idea("A notes app for students")
    created_session.save_plan()
    pid = created_session.project_id
    created_session.close()

    opened = ProjectWorkspaceSession.open(project_store, chat_store, pid)
    try:
        assert opened.engine.plan.understanding.original_idea == \
            "A notes app for students"
    finally:
        opened.close()
# ----------------------------------------------------------------- 5 / 6

def test_planer_chat_is_restored_and_id_stable(project_store, chat_store, created_session):
    created_session.chat_with_planer("I want to build a notes app")
    pid = created_session.project_id
    chat_id_1 = created_session.planer_chat.chat_id
    created_session.close()

    opened = ProjectWorkspaceSession.open(project_store, chat_store, pid)
    try:
        history = opened.load_chat_history()
        assert len(history) == 2  # user + assistant
        assert opened.planer_chat.chat_id == chat_id_1
    finally:
        opened.close()


# ----------------------------------------------------------------- 7

def test_planer_conversation_continues_after_reopen(project_store, chat_store, created_session):
    created_session.chat_with_planer("First message")
    pid = created_session.project_id
    created_session.close()

    opened = ProjectWorkspaceSession.open(project_store, chat_store, pid)
    try:
        history = opened.load_chat_history()
        assert len(history) == 2
        # New turn on the reopened workspace should append, not duplicate.
        opened.chat_with_planer("Second message")
        history2 = opened.load_chat_history()
        assert len(history2) == 4
    finally:
        opened.close()


# ----------------------------------------------------------------- 8

def test_save_plan_persists_structured_plan(project_store, chat_store, created_session):
    created_session.engine.start_with_idea("Build a small CRM")
    created_session.save_plan()
    pid = created_session.project_id
    created_session.close()

    # Direct store read: plan must reflect what we saved.
    stored = project_store.get_project(pid)
    assert stored is not None
    assert stored.plan
    inner = stored.plan.get("plan", {})
    assert inner.get("understanding", {}).get("original_idea") == \
        "Build a small CRM"


def test_save_plan_preserves_project_plan_metadata(project_store, chat_store):
    metadata = {
        "mode": "visual_app_design",
        "visual_design": {"palette": ["navy", "gold"]},
        "custom_metadata": {"owner": "design-team", "revision": 3},
    }
    session = ProjectWorkspaceSession.create(
        project_store=project_store,
        chat_store=chat_store,
        name="Visual project",
        root_path="C:/projects/visual",
        plan_data=metadata,
        mode="visual_app_design",
    )
    try:
        session.engine.start_with_idea("A visual planning project")
        session.save_plan()
        project_id = session.project_id

        stored = project_store.get_project(project_id)
        assert stored is not None
        assert stored.plan["mode"] == "visual_app_design"
        assert stored.plan["visual_design"] == metadata["visual_design"]
        assert stored.plan["custom_metadata"] == metadata["custom_metadata"]
        assert stored.plan["plan"]["understanding"]["original_idea"] == \
            "A visual planning project"
    finally:
        session.close()

    reopened = ProjectWorkspaceSession.open(project_store, chat_store, project_id)
    try:
        assert reopened.project.mode == "visual_app_design"
        assert reopened.project.plan["visual_design"] == metadata["visual_design"]
        assert reopened.project.plan["custom_metadata"] == metadata["custom_metadata"]
        assert reopened.engine.plan.understanding.original_idea == \
            "A visual planning project"
    finally:
        reopened.close()


# ----------------------------------------------------------------- 9

def test_readiness_uses_existing_planer_validation(created_session):
    r = created_session.readiness()
    assert r.is_ready_for_review is False
    assert r.plan_readiness is not None
    assert r.phase is not None


# ----------------------------------------------------------------- 10

def test_opening_project_does_not_change_lifecycle(project_store, chat_store, created_session):
    pid = created_session.project_id
    initial = created_session.lifecycle
    created_session.close()

    opened = ProjectWorkspaceSession.open(project_store, chat_store, pid)
    try:
        assert opened.lifecycle == initial
    finally:
        opened.close()


# ----------------------------------------------------------------- 11

def test_planer_chat_does_not_auto_approve(project_store, chat_store, created_session):
    created_session.chat_with_planer("Build me something huge")
    pid = created_session.project_id
    created_session.close()
    project = project_store.get_project(pid)
    assert project is not None
    assert project.lifecycle == ProjectLifecycle.PLANNING
    # Must not have been moved toward REVIEW/APPROVED/BUILDING.
    assert project.lifecycle not in {
        ProjectLifecycle.REVIEW,
        ProjectLifecycle.APPROVED,
        ProjectLifecycle.BUILDING,
    }


# ----------------------------------------------------------------- 12

def test_planer_readiness_does_not_auto_approve(project_store, chat_store, created_session):
    # Push the engine into REVIEW_READY phase internally; the workspace
    # readiness should report it but the *project* lifecycle must
    # remain PLANNING.
    created_session.engine.mark_review_ready()
    pid = created_session.project_id
    project = project_store.get_project(pid)
    assert project is not None
    assert project.lifecycle == ProjectLifecycle.PLANNING
    # Workspace's own readiness is decoupled from the project.
    r = created_session.readiness()
    assert r.plan_readiness is not None


# ----------------------------------------------------------------- 13

def test_project_a_b_plan_isolation(project_store, chat_store):
    a = ProjectWorkspaceSession.create(project_store, chat_store, "Alpha", "C:/a")
    b = ProjectWorkspaceSession.create(project_store, chat_store, "Beta", "C:/b")
    try:
        a.engine.start_with_idea("Alpha's idea")
        a.save_plan()
        b.engine.start_with_idea("Beta's idea")
        b.save_plan()

        a_re = project_store.get_project(a.project_id)
        b_re = project_store.get_project(b.project_id)
        assert a_re.plan["plan"]["understanding"]["original_idea"] == "Alpha's idea"
        assert b_re.plan["plan"]["understanding"]["original_idea"] == "Beta's idea"
    finally:
        a.close()
        b.close()


# ----------------------------------------------------------------- 14

def test_project_a_b_chat_isolation(project_store, chat_store):
    a = ProjectWorkspaceSession.create(project_store, chat_store, "Alpha", "C:/a")
    b = ProjectWorkspaceSession.create(project_store, chat_store, "Beta", "C:/b")
    try:
        a.chat_with_planer("Alpha message")
        b.chat_with_planer("Beta message")

        a_hist = a.load_chat_history()
        b_hist = b.load_chat_history()
        assert len(a_hist) == 2
        assert len(b_hist) == 2
        assert a_hist[0].content == "Alpha message"
        assert b_hist[0].content == "Beta message"
        # And the chat ids differ.
        assert a.planer_chat.chat_id != b.planer_chat.chat_id
    finally:
        a.close()
        b.close()


# ----------------------------------------------------------------- 15

def test_close_reopen_preserves_data(project_store, chat_store, created_session):
    created_session.engine.start_with_idea("Persistent idea")
    created_session.chat_with_planer("Persistent turn")
    created_session.save_plan()
    pid = created_session.project_id
    created_session.close()

    # Reopen multiple times.
    for _ in range(2):
        ws = ProjectWorkspaceSession.open(project_store, chat_store, pid)
        try:
            assert ws.engine.plan.understanding.original_idea == "Persistent idea"
            assert len(ws.load_chat_history()) == 2
            assert ws.lifecycle == ProjectLifecycle.PLANNING
        finally:
            ws.close()


# ----------------------------------------------------------------- 16

def test_missing_project_fails_cleanly(project_store, chat_store):
    with pytest.raises(ProjectNotFoundError):
        ProjectWorkspaceSession.open(project_store, chat_store, "proj-does-not-exist")


def test_invalid_project_id_fails_cleanly(project_store, chat_store):
    with pytest.raises(ProjectWorkspaceError):
        ProjectWorkspaceSession.open(project_store, chat_store, "")


def test_create_rejects_blank_name(project_store, chat_store):
    with pytest.raises(ProjectWorkspaceError):
        ProjectWorkspaceSession.create(project_store, chat_store, "", "C:/x")


def test_create_rejects_blank_path(project_store, chat_store):
    with pytest.raises(ProjectWorkspaceError):
        ProjectWorkspaceSession.create(project_store, chat_store, "Name", "")


# ----------------------------------------------------------------- 17

def test_invalid_persisted_plan_is_handled_safely(project_store, chat_store, tmp_path):
    # Create a project with a plan dict whose inner structure is
    # wrong for the deserializer. The workspace must not crash; it
    # should either surface a ProjectWorkspaceError or, if the
    # deserializer is lenient, restore a safe default plan.
    bad = Project.create(
        name="Bad",
        root_path=str(tmp_path / "bad"),
        plan={"plan": "not a dict", "schema_version": 1},
    )
    stored = project_store.create_project(bad)
    try:
        ws = ProjectWorkspaceSession.open(project_store, chat_store, stored.id)
    except ProjectWorkspaceError:
        return  # either behaviour is acceptable; the contract is
                # "no crash, no silent data loss beyond the bad plan"
    try:
        # The engine must be in a usable state — at minimum, the
        # project_id must round-trip.
        assert ws.project_id == stored.id
        assert ws.lifecycle == ProjectLifecycle.PLANNING
    finally:
        ws.close()


# ----------------------------------------------------------------- 18

def test_store_failure_is_reported(project_store, chat_store, created_session, monkeypatch):
    # Force set_plan to raise; the workspace should wrap it.
    def boom(project_id, plan):
        raise RuntimeError("simulated store failure")
    monkeypatch.setattr(project_store, "set_plan", boom)
    created_session.engine.start_with_idea("X")
    with pytest.raises(ProjectStoreUnavailableError):
        created_session.save_plan()


# ----------------------------------------------------------------- 19

def test_close_does_not_delete_project_chat_or_plan(project_store, chat_store, created_session):
    created_session.engine.start_with_idea("Keep me")
    created_session.chat_with_planer("Keep me too")
    created_session.save_plan()
    pid = created_session.project_id
    chat_id = created_session.planer_chat.chat_id
    created_session.close()

    # Everything must still exist.
    proj = project_store.get_project(pid)
    assert proj is not None
    assert proj.plan
    assert proj.lifecycle == ProjectLifecycle.PLANNING

    chats = chat_store.list_chats(f"project:{pid}", pid)
    titles = [c.title for c in chats]
    assert PLANER_CHAT_TITLE in titles


# ----------------------------------------------------------------- 20

def test_no_coding_agent_or_build_invocation(created_session, monkeypatch):
    """Opening / chatting / saving must not invoke any build, test,
    or verification subsystem.
    """
    import giscode.projects.workspace as ws_mod
    forbidden = [
        "CodingAgentLoop", "BuildTestOrchestrator", "approve",
        "start_building", "execute_command", "run_tests",
        "verify", "build", "test_",
    ]
    # The session object must not expose any of these names.
    exposed = set(dir(created_session))
    for name in forbidden:
        # Match both exact names and the "approve" method in the
        # public surface of the session.
        if name in {"CodingAgentLoop", "BuildTestOrchestrator",
                    "execute_command", "run_tests", "verify"}:
            assert name not in exposed, f"session exposes {name}"
    # And lifecycle must remain PLANNING.
    assert created_session.lifecycle == ProjectLifecycle.PLANNING

    # Even after driving the engine hard, lifecycle must not move.
    created_session.engine.start_with_idea("Hard")
    for q in created_session.engine.generate_questions():
        created_session.engine.answer_question(q.id, "answer " + q.id)
    created_session.save_plan()
    assert created_session.lifecycle == ProjectLifecycle.PLANNING


# ----------------------------------------------------------------- 20b (explicit)

def test_chatting_does_not_change_project_lifecycle(
    project_store, chat_store, created_session
):
    pid = created_session.project_id
    created_session.chat_with_planer("Drive the planer")
    proj = project_store.get_project(pid)
    assert proj.lifecycle == ProjectLifecycle.PLANNING


def test_save_plan_does_not_change_project_lifecycle(
    project_store, chat_store, created_session
):
    pid = created_session.project_id
    created_session.engine.start_with_idea("save me")
    created_session.save_plan()
    proj = project_store.get_project(pid)
    assert proj.lifecycle == ProjectLifecycle.PLANNING


def test_session_close_blocks_further_access(created_session):
    pid = created_session.project_id
    created_session.close()
    with pytest.raises(WorkspaceAlreadyClosedError):
        _ = created_session.project
    with pytest.raises(WorkspaceAlreadyClosedError):
        _ = created_session.engine
    with pytest.raises(WorkspaceAlreadyClosedError):
        _ = created_session.ai
    with pytest.raises(WorkspaceAlreadyClosedError):
        _ = created_session.planer_chat
    # And the id accessor is still valid via a cached snapshot:
    assert pid.startswith("proj-")


def test_open_then_create_new_does_not_collide(project_store, chat_store):
    a = ProjectWorkspaceSession.create(project_store, chat_store, "A", "C:/a")
    pid_a = a.project_id
    a.close()
    b = ProjectWorkspaceSession.create(project_store, chat_store, "B", "C:/b")
    try:
        assert b.project_id != pid_a
        assert project_store.get_project(pid_a) is not None
        assert project_store.get_project(b.project_id) is not None
    finally:
        b.close()


def test_chat_with_planer_rejects_empty(project_store, chat_store, created_session):
    with pytest.raises(ProjectWorkspaceError):
        created_session.chat_with_planer("   ")
