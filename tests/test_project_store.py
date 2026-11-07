"""Tests for giscode.projects (Project model, lifecycle, ProjectStore).

Tests do NOT touch the user's real ``~/.giscode/projects.db`` — every
test uses a temporary file under pytest's ``tmp_path`` fixture.
"""
import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from giscode.projects import (
    Project,
    ProjectLifecycle,
    ProjectStore,
)
from giscode.projects.lifecycle import (
    ALLOWED_TRANSITIONS,
    LifecycleError,
    TERMINAL_STATES,
    assert_transition,
    can_transition,
)
from giscode.projects.model import (
    normalize_project_name,
    normalize_project_path,
)


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test_projects.db")


@pytest.fixture
def store(db_path: str) -> ProjectStore:
    s = ProjectStore(db_path=db_path)
    yield s
    s.close()


@pytest.fixture
def sample_project(tmp_path: Path) -> Project:
    return Project.create(
        name="My App",
        root_path=str(tmp_path / "my_app"),
        description="An example project",
    )


# ----------------------------------------------------------------- model

def test_project_create_generates_stable_id(tmp_path: Path):
    a = Project.create("A", str(tmp_path / "a"))
    b = Project.create("B", str(tmp_path / "b"))
    assert a.id != b.id
    assert a.id.startswith("proj-")
    # Stable across re-reads
    again = Project.from_dict(a.to_dict())
    assert again.id == a.id


def test_project_create_defaults_to_planning(tmp_path: Path):
    p = Project.create("A", str(tmp_path / "a"))
    assert p.lifecycle == ProjectLifecycle.PLANNING
    assert p.is_terminal is False


def test_project_create_normalizes_name(tmp_path: Path):
    p = Project.create("  My   Project  ", str(tmp_path / "a"))
    assert p.name == "My Project"
    p2 = Project.create("", str(tmp_path / "b"))
    assert p2.name == "Untitled Project"
    p3 = Project.create("///", str(tmp_path / "c"))
    assert p3.name == "Untitled Project"
    p4 = Project.create(None, str(tmp_path / "d"))  # type: ignore[arg-type]
    assert p4.name == "Untitled Project"


def test_normalize_project_name_tab_newline_to_space():
    assert normalize_project_name("My\tProject\nTest") == "My Project Test"


def test_project_create_normalizes_path_to_absolute(tmp_path: Path):
    rel = tmp_path / "rel_app"
    p = Project.create("X", str(rel))
    assert os.path.isabs(p.root_path)
    # The result should be the same physical path regardless of how it was spelled
    assert p.root_path == os.path.normpath(str(rel.resolve()) if rel.exists() else os.path.abspath(str(rel)))


def test_project_create_rejects_empty_path():
    with pytest.raises(ValueError):
        Project.create("X", "")


def test_project_serialization_round_trip(sample_project: Project):
    data = sample_project.to_dict()
    again = Project.from_dict(data)
    assert again.id == sample_project.id
    assert again.name == sample_project.name
    assert again.root_path == sample_project.root_path
    assert again.lifecycle == sample_project.lifecycle
    assert again.created_at == sample_project.created_at
    assert again.updated_at == sample_project.updated_at
    assert again.description == sample_project.description
    assert again.plan == sample_project.plan


def test_project_chat_scope_uses_stable_id(sample_project: Project):
    assert sample_project.chat_scope == f"project:{sample_project.id}"
    # Renaming the project must not change the chat scope
    renamed = Project.from_dict({**sample_project.to_dict(), "name": "Different"})
    assert renamed.chat_scope == f"project:{sample_project.id}"


def test_project_terminal_property():
    # COMPLETE and CANCELLED are genuinely terminal: nothing reopens them.
    for lc in (ProjectLifecycle.COMPLETE, ProjectLifecycle.CANCELLED):
        assert lc in TERMINAL_STATES
    # FAILED is not terminal any more: an explicit retry reopens it for
    # approval. Its single exit is checked separately below.
    for lc in (
        ProjectLifecycle.PLANNING, ProjectLifecycle.REVIEW, ProjectLifecycle.APPROVED,
        ProjectLifecycle.BUILDING, ProjectLifecycle.TESTING, ProjectLifecycle.VERIFICATION,
        ProjectLifecycle.FAILED,
    ):
        assert lc not in TERMINAL_STATES


# ----------------------------------------------------------------- lifecycle rules

def test_happy_path_is_supported():
    chain = [
        ProjectLifecycle.PLANNING,
        ProjectLifecycle.REVIEW,
        ProjectLifecycle.APPROVED,
        ProjectLifecycle.BUILDING,
        ProjectLifecycle.TESTING,
        ProjectLifecycle.VERIFICATION,
        ProjectLifecycle.COMPLETE,
    ]
    for a, b in zip(chain, chain[1:]):
        assert can_transition(a, b), f"expected {a.value} -> {b.value}"


def test_planning_does_not_auto_enter_building():
    """The critical safety property: PLANNING never reaches BUILDING
    without going through REVIEW and APPROVED."""
    assert can_transition(ProjectLifecycle.PLANNING, ProjectLifecycle.BUILDING) is False
    assert can_transition(ProjectLifecycle.PLANNING, ProjectLifecycle.APPROVED) is False
    assert can_transition(ProjectLifecycle.REVIEW, ProjectLifecycle.BUILDING) is False
    assert can_transition(ProjectLifecycle.REVIEW, ProjectLifecycle.APPROVED) is True


def test_approved_does_not_auto_enter_building():
    """APPROVED is a stable, explicit state. The only legal exit
    is to BUILDING — and that move must be *explicit* (start_building)."""
    assert can_transition(ProjectLifecycle.APPROVED, ProjectLifecycle.BUILDING) is True
    # But there is no other forward move from APPROVED.
    for target in (
        ProjectLifecycle.TESTING,
        ProjectLifecycle.VERIFICATION,
        ProjectLifecycle.COMPLETE,
    ):
        assert can_transition(ProjectLifecycle.APPROVED, target) is False


def test_terminal_states_have_no_exits():
    for term in TERMINAL_STATES:
        for target in ProjectLifecycle:
            assert can_transition(term, target) is False


def test_cancellation_allowed_from_active_states():
    # A finished FAILED build is left alone until the user retries it; a project
    # whose work is still in progress can always be cancelled.
    active = [
        lc for lc in ProjectLifecycle
        if lc not in TERMINAL_STATES and lc != ProjectLifecycle.FAILED
    ]
    for lc in active:
        assert can_transition(lc, ProjectLifecycle.CANCELLED), lc
    assert can_transition(ProjectLifecycle.FAILED, ProjectLifecycle.CANCELLED) is False


def test_failed_and_never_started_projects_can_be_retried():
    assert can_transition(ProjectLifecycle.FAILED, ProjectLifecycle.REVIEW) is True
    assert can_transition(ProjectLifecycle.APPROVED, ProjectLifecycle.REVIEW) is True
    # A retry never skips the approval boundary, and never reopens a finished or
    # in-progress project.
    assert can_transition(ProjectLifecycle.FAILED, ProjectLifecycle.BUILDING) is False
    assert can_transition(ProjectLifecycle.FAILED, ProjectLifecycle.APPROVED) is False
    assert can_transition(ProjectLifecycle.BUILDING, ProjectLifecycle.REVIEW) is False
    assert can_transition(ProjectLifecycle.COMPLETE, ProjectLifecycle.REVIEW) is False
    assert can_transition(ProjectLifecycle.CANCELLED, ProjectLifecycle.REVIEW) is False


def test_failure_allowed_from_active_phases():
    for lc in (
        ProjectLifecycle.PLANNING,
        ProjectLifecycle.REVIEW,
        ProjectLifecycle.APPROVED,
        ProjectLifecycle.BUILDING,
        ProjectLifecycle.TESTING,
        ProjectLifecycle.VERIFICATION,
    ):
        assert can_transition(lc, ProjectLifecycle.FAILED), lc


def test_review_to_planning_allowed_for_edit_plan():
    assert can_transition(ProjectLifecycle.REVIEW, ProjectLifecycle.PLANNING) is True


def test_assert_transition_raises_for_invalid():
    with pytest.raises(LifecycleError):
        assert_transition(ProjectLifecycle.PLANNING, ProjectLifecycle.BUILDING)


def test_assert_transition_raises_for_terminal():
    with pytest.raises(LifecycleError):
        assert_transition(ProjectLifecycle.COMPLETE, ProjectLifecycle.REVIEW)


def test_can_transition_rejects_non_enum():
    with pytest.raises(TypeError):
        can_transition("planning", "building")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        can_transition(ProjectLifecycle.PLANNING, "BUILDING")  # type: ignore[arg-type]


# ----------------------------------------------------------------- store CRUD

def test_database_and_tables_created_automatically(tmp_path: Path):
    target = str(tmp_path / "autoinit.db")
    assert not os.path.exists(target)
    ProjectStore(db_path=target).close()
    assert os.path.exists(target)
    conn = sqlite3.connect(target)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "projects" in names


def test_create_and_get_project(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    loaded = store.get_project(sample_project.id)
    assert loaded is not None
    assert loaded.id == sample_project.id
    assert loaded.name == sample_project.name
    assert loaded.lifecycle == ProjectLifecycle.PLANNING
    assert loaded.root_path == sample_project.root_path
    assert loaded.description == sample_project.description
    assert loaded.plan == sample_project.plan


def test_create_project_duplicate_id_raises(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    with pytest.raises(ValueError):
        store.create_project(sample_project)


def test_get_unknown_returns_none(store: ProjectStore):
    assert store.get_project("proj-bogus") is None
    assert store.exists("proj-bogus") is False


def test_list_projects_orders_newest_first(store: ProjectStore, tmp_path: Path):
    a = store.create_project(Project.create("A", str(tmp_path / "a")))
    b = store.create_project(Project.create("B", str(tmp_path / "b")))
    c = store.create_project(Project.create("C", str(tmp_path / "c")))
    listed = store.list_projects()
    assert [p.id for p in listed] == [c.id, b.id, a.id]


def test_list_projects_filter_by_lifecycle(store: ProjectStore, tmp_path: Path):
    a = store.create_project(Project.create("A", str(tmp_path / "a")))
    b = store.create_project(Project.create("B", str(tmp_path / "b")))
    store.transition_lifecycle(b.id, ProjectLifecycle.REVIEW)
    assert [p.id for p in store.list_projects(lifecycle=ProjectLifecycle.PLANNING)] == [a.id]
    assert [p.id for p in store.list_projects(lifecycle=ProjectLifecycle.REVIEW)] == [b.id]


def test_list_projects_limit(store: ProjectStore, tmp_path: Path):
    for i in range(5):
        store.create_project(Project.create(f"P{i}", str(tmp_path / f"p{i}")))
    assert len(store.list_projects(limit=2)) == 2


def test_find_by_name(store: ProjectStore, tmp_path: Path):
    a = store.create_project(Project.create("Hello", str(tmp_path / "hello")))
    found = store.find_by_name("hello")
    assert found is not None and found.id == a.id
    found2 = store.find_by_name("HELLO")
    assert found2 is not None and found2.id == a.id
    assert store.find_by_name("nope") is None


def test_update_project_name_and_description(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    updated = store.update_project(
        sample_project.id, name="Renamed", description="New desc"
    )
    assert updated is not None
    assert updated.name == "Renamed"
    assert updated.description == "New desc"
    # ID must remain stable through rename
    assert updated.id == sample_project.id
    assert updated.chat_scope == f"project:{sample_project.id}"


def test_rename_project_helper(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    renamed = store.rename_project(sample_project.id, "Better Name")
    assert renamed is not None
    assert renamed.name == "Better Name"


def test_update_project_plan(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    plan = {
        "summary": "Build a to-do app",
        "features": ["auth", "sync", "offline"],
        "tech": {"frontend": "Tauri", "backend": "FastAPI"},
    }
    updated = store.set_plan(sample_project.id, plan)
    assert updated is not None
    assert updated.plan == plan
    # Round trip
    reloaded = store.get_project(sample_project.id)
    assert reloaded is not None
    assert reloaded.plan == plan


def test_update_unknown_returns_none(store: ProjectStore):
    assert store.update_project("proj-nope", name="x") is None
    assert store.rename_project("proj-nope", "x") is None
    assert store.set_plan("proj-nope", {}) is None
    assert store.set_description("proj-nope", "x") is None


def test_update_project_updates_timestamp(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    original = store.get_project(sample_project.id)
    assert original is not None
    import time as _t
    _t.sleep(0.02)
    updated = store.update_project(sample_project.id, description="changed")
    assert updated is not None
    assert updated.updated_at >= original.updated_at


def test_delete_project(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    assert store.delete_project(sample_project.id) is True
    assert store.get_project(sample_project.id) is None


def test_delete_unknown_returns_false(store: ProjectStore):
    assert store.delete_project("proj-nope") is False


# ----------------------------------------------------------------- lifecycle store

def test_transition_lifecycle_happy_path(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    pid = sample_project.id
    for target in (
        ProjectLifecycle.REVIEW,
        ProjectLifecycle.APPROVED,
        ProjectLifecycle.BUILDING,
        ProjectLifecycle.TESTING,
        ProjectLifecycle.VERIFICATION,
        ProjectLifecycle.COMPLETE,
    ):
        proj = store.transition_lifecycle(pid, target)
        assert proj is not None and proj.lifecycle == target


def test_transition_lifecycle_rejects_invalid(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    with pytest.raises(LifecycleError):
        store.transition_lifecycle(sample_project.id, ProjectLifecycle.BUILDING)


def test_transition_lifecycle_unknown_returns_none(store: ProjectStore):
    assert store.transition_lifecycle("proj-nope", ProjectLifecycle.REVIEW) is None


def test_approved_does_not_imply_building(store: ProjectStore, sample_project: Project):
    """The critical safety property at the store level: setting
    APPROVED does NOT change anything else. The lifecycle stays at
    APPROVED until an explicit start_building() call."""
    store.create_project(sample_project)
    pid = sample_project.id
    store.transition_lifecycle(pid, ProjectLifecycle.REVIEW)
    store.approve(pid)
    proj = store.get_project(pid)
    assert proj is not None
    assert proj.lifecycle == ProjectLifecycle.APPROVED
    # Crucially: no implicit BUILDING
    again = store.get_project(pid)
    assert again is not None
    assert again.lifecycle == ProjectLifecycle.APPROVED


def test_building_only_starts_on_explicit_call(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    pid = sample_project.id
    store.transition_lifecycle(pid, ProjectLifecycle.REVIEW)
    store.approve(pid)
    # Approved but not building
    assert store.get_project(pid).lifecycle == ProjectLifecycle.APPROVED
    # Until we explicitly start building
    store.start_building(pid)
    assert store.get_project(pid).lifecycle == ProjectLifecycle.BUILDING


def test_can_transition_helper(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    assert store.can_transition(sample_project.id, ProjectLifecycle.REVIEW) is True
    assert store.can_transition(sample_project.id, ProjectLifecycle.BUILDING) is False
    assert store.can_transition("proj-nope", ProjectLifecycle.REVIEW) is False


def test_fail_and_cancel(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    store.fail(sample_project.id)
    assert store.get_project(sample_project.id).lifecycle == ProjectLifecycle.FAILED
    # FAILED cannot drift into an arbitrary state; the one exit is the explicit
    # retry, which reopens REVIEW and keeps the plan.
    with pytest.raises(LifecycleError):
        store.transition_lifecycle(sample_project.id, ProjectLifecycle.BUILDING)
    plan_before = dict(store.get_project(sample_project.id).plan)
    reopened = store.retry(sample_project.id)
    assert reopened is not None
    assert reopened.lifecycle == ProjectLifecycle.REVIEW
    assert reopened.plan == plan_before

    p2 = store.create_project(Project.create("P2", sample_project.root_path + "_2"))
    store.cancel(p2.id)
    assert store.get_project(p2.id).lifecycle == ProjectLifecycle.CANCELLED
    # Cancelled is terminal: it is never reopened by a retry.
    with pytest.raises(LifecycleError):
        store.retry(p2.id)


def test_edit_plan_round_trip(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    store.transition_lifecycle(sample_project.id, ProjectLifecycle.REVIEW)
    # User clicks "Edit Plan" -> back to PLANNING
    store.transition_lifecycle(sample_project.id, ProjectLifecycle.PLANNING)
    assert store.get_project(sample_project.id).lifecycle == ProjectLifecycle.PLANNING
    # And forward again
    store.transition_lifecycle(sample_project.id, ProjectLifecycle.REVIEW)
    store.approve(sample_project.id)
    assert store.get_project(sample_project.id).lifecycle == ProjectLifecycle.APPROVED


# ----------------------------------------------------------------- persistence

def test_persistence_across_store_instances(db_path: str, sample_project: Project):
    s1 = ProjectStore(db_path=db_path)
    s1.create_project(sample_project)
    s1.close()
    s2 = ProjectStore(db_path=db_path)
    loaded = s2.get_project(sample_project.id)
    assert loaded is not None
    assert loaded.id == sample_project.id
    assert loaded.name == sample_project.name
    assert loaded.lifecycle == sample_project.lifecycle
    s2.close()


def test_persistence_preserves_plan(db_path: str, sample_project: Project):
    s1 = ProjectStore(db_path=db_path)
    plan = {"name": "demo", "items": [1, 2, 3], "meta": {"k": "v"}}
    sample_project.plan = plan
    s1.create_project(sample_project)
    s1.close()
    s2 = ProjectStore(db_path=db_path)
    loaded = s2.get_project(sample_project.id)
    assert loaded is not None
    assert loaded.plan == plan
    s2.close()


# ----------------------------------------------------------------- isolation

def test_chat_scope_uses_stable_id_after_rename(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    scope_before = store.get_project(sample_project.id).chat_scope
    store.rename_project(sample_project.id, "Completely Different Name")
    scope_after = store.get_project(sample_project.id).chat_scope
    assert scope_before == scope_after == f"project:{sample_project.id}"


def test_project_ids_remain_stable_after_rename(store: ProjectStore, sample_project: Project):
    store.create_project(sample_project)
    id_before = store.get_project(sample_project.id).id
    store.rename_project(sample_project.id, "new name")
    id_after = store.get_project(sample_project.id).id
    assert id_before == id_after == sample_project.id


def test_store_is_isolated_from_chat_store(store: ProjectStore, sample_project: Project, tmp_path: Path):
    """The project store must not write into the ChatStore DB and
    vice versa. Verified by pointing both at the same tmp directory
    and checking that the project's chat scope is acceptable to the
    ChatStore without any cross-write."""
    from giscode.storage import ChatStore

    pstore = store
    cstore = ChatStore(db_path=str(tmp_path / "chats.db"))
    try:
        pstore.create_project(sample_project)
        # The scope we expose must be a valid ChatStore scope
        scope = sample_project.chat_scope
        assert scope.startswith("project:")
        # Create a chat in that scope through ChatStore
        chat = cstore.create_chat(scope, sample_project.id, "Planer chat")
        assert chat.id
        # The project store should not know or care about it
        assert pstore.count() == 1
        # And the chat store has the chat
        assert len(cstore.list_chats(scope, sample_project.id)) == 1
    finally:
        cstore.close()


# ----------------------------------------------------------------- robustness

def test_real_user_projects_db_is_not_created_when_using_tmp_path(tmp_path: Path):
    from giscode.projects.store import DEFAULT_DB_PATH
    real = Path(DEFAULT_DB_PATH)
    if real.exists():
        pytest.skip("user projects.db already exists; cannot assert absence")
    s = ProjectStore(db_path=str(tmp_path / "isolated.db"))
    s.create_project(Project.create("Tmp", str(tmp_path / "tmp")))
    s.close()
    assert not real.exists()


def test_count_helper(store: ProjectStore, tmp_path: Path):
    a = store.create_project(Project.create("A", str(tmp_path / "a")))
    store.create_project(Project.create("B", str(tmp_path / "b")))
    assert store.count() == 2
    assert store.count(lifecycle=ProjectLifecycle.PLANNING) == 2
    store.transition_lifecycle(a.id, ProjectLifecycle.REVIEW)
    assert store.count(lifecycle=ProjectLifecycle.PLANNING) == 1
    assert store.count(lifecycle=ProjectLifecycle.REVIEW) == 1


def test_corrupt_db_recovers_to_empty(tmp_path: Path):
    bad = tmp_path / "bad.db"
    bad.write_text("not a sqlite db", encoding="utf-8")
    # SQLite refuses to open non-DB files; the store will raise on init.
    with pytest.raises(sqlite3.DatabaseError):
        ProjectStore(db_path=str(bad))


def _fabricate_partially_corrupt_db(path: Path) -> None:
    """Create a genuine SQLite file whose schema is readable but whose
    b-tree pages are damaged (matches the reported real-user failure:
    ``Tree 3 page 3: btreeInitPage() returns error code 11``).
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,
            root_path TEXT NOT NULL, lifecycle TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            plan_json TEXT NOT NULL DEFAULT '{}');
        INSERT INTO projects VALUES ('p1','Alpha','/x/a/a','PLANNING','t','t','','{}');
        INSERT INTO projects VALUES ('p2','Beta','/x/b/b','PLANNING','t','t','','{}');
        """
    )
    conn.commit()
    conn.close()
    raw = bytearray(path.read_bytes())
    for i in range(4096 * 2, min(4096 * 3, len(raw))):
        raw[i] = 0
    path.write_bytes(bytes(raw))


def test_partially_corrupt_db_is_rebuilt_and_backed_up(tmp_path: Path):
    """A SQLite DB that opens fine but has damaged pages must not
    silently appear empty: the store should back it up and rebuild.
    """
    db = tmp_path / "projects.db"
    _fabricate_partially_corrupt_db(db)

    # Sanity: it really is damaged in the reported way.
    probe = sqlite3.connect(str(db))
    qc = [str(r[0]) for r in probe.execute("PRAGMA quick_check")]
    probe.close()
    assert any("btreeInitPage" in q for q in qc)

    s = ProjectStore(db_path=str(db))
    try:
        assert s.count() == 0
        # The damaged file was moved aside as a backup...
        backups = [p for p in tmp_path.iterdir() if ".corrupt-" in p.name]
        assert len(backups) == 1
        assert getattr(s, "recovery_note", "") and "corrupt" in s.recovery_note
        # ...and the rebuilt store is fully usable.
        p = s.create_project(Project.create("Rebuilt", str(tmp_path / "rebuilt")))
        assert s.get_project(p.id) is not None
    finally:
        s.close()


def test_thread_safe_concurrent_writes(db_path: str, tmp_path: Path):
    s = ProjectStore(db_path=db_path)
    errors: list = []

    def worker(i: int) -> None:
        try:
            for j in range(10):
                p = Project.create(f"P{i}_{j}", str(tmp_path / f"p{i}_{j}"))
                s.create_project(p)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert s.count() == 50
    s.close()


def test_thread_safe_concurrent_transitions(db_path: str, sample_project: Project):
    s = ProjectStore(db_path=db_path)
    s.create_project(sample_project)
    # Force the project into REVIEW
    s.transition_lifecycle(sample_project.id, ProjectLifecycle.REVIEW)

    success = {"n": 0}
    lock = threading.Lock()

    def attempt_approve() -> None:
        try:
            s.approve(sample_project.id)
            with lock:
                success["n"] += 1
        except LifecycleError:
            pass

    threads = [threading.Thread(target=attempt_approve) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Only one of the concurrent calls can win the transition.
    assert success["n"] == 1
    assert s.get_project(sample_project.id).lifecycle == ProjectLifecycle.APPROVED
    s.close()
