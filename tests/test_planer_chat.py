"""Tests for the persistent Planer project chat.

All tests use a temporary ``ChatStore`` SQLite file under
``tmp_path`` so the user's real ``~/.giscode/chats.db`` is never
touched.
"""
import threading
from pathlib import Path
from typing import Iterable

import pytest

from giscode.planer import (
    PLANER_CHAT_TITLE,
    PersistedTurn,
    PlanerChatPersistence,
    PlanerChatPersistenceError,
)
from giscode.projects import Project, ProjectStore
from giscode.storage import ChatRecord, ChatStore, MessageRecord


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def chat_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "chats.db")


@pytest.fixture
def chat_store(chat_db_path: str) -> ChatStore:
    s = ChatStore(db_path=chat_db_path)
    yield s
    s.close()


@pytest.fixture
def project_a(tmp_path: Path) -> Project:
    return Project.create("Project A", str(tmp_path / "a"))


@pytest.fixture
def project_b(tmp_path: Path) -> Project:
    return Project.create("Project B", str(tmp_path / "b"))


@pytest.fixture
def project_store(tmp_path: Path) -> ProjectStore:
    s = ProjectStore(db_path=str(tmp_path / "projects.db"))
    yield s
    s.close()


@pytest.fixture
def persistence_a(chat_store: ChatStore, project_a: Project) -> PlanerChatPersistence:
    return PlanerChatPersistence(project=project_a, chat_store=chat_store)


# ----------------------------------------------------------------- 1. create

def test_get_or_create_chat_creates_one(persistence_a: PlanerChatPersistence, project_a: Project):
    chat = persistence_a.get_or_create_chat()
    assert isinstance(chat, ChatRecord)
    assert chat.id.startswith("chat-")
    assert chat.title == PLANER_CHAT_TITLE
    assert chat.scope == f"project:{project_a.id}"
    assert chat.scope_id == project_a.id


def test_get_or_create_chat_caches_id(persistence_a: PlanerChatPersistence):
    chat1 = persistence_a.get_or_create_chat()
    chat2 = persistence_a.get_or_create_chat()
    assert chat1.id == chat2.id


# ----------------------------------------------------------------- 2. reuse

def test_existing_planer_chat_is_reused(chat_store: ChatStore, project_a: Project):
    # First instance: creates
    p1 = PlanerChatPersistence(project=project_a, chat_store=chat_store)
    chat1 = p1.get_or_create_chat()
    # New instance over the same store: must find the same chat
    p2 = PlanerChatPersistence(project=project_a, chat_store=chat_store)
    chat2 = p2.get_or_create_chat()
    assert chat1.id == chat2.id


def test_reused_chat_id_is_stable_across_reopens(chat_db_path: str, project_a: Project):
    s1 = ChatStore(db_path=chat_db_path)
    p1 = PlanerChatPersistence(project=project_a, chat_store=s1)
    chat1 = p1.get_or_create_chat()
    p1.record_user_turn("first", turn_id="t-1")
    s1.close()
    s2 = ChatStore(db_path=chat_db_path)
    p2 = PlanerChatPersistence(project=project_a, chat_store=s2)
    chat2 = p2.get_or_create_chat()
    assert chat2.id == chat1.id
    history = p2.load_history()
    assert [m.content for m in history] == ["first"]
    s2.close()


# ----------------------------------------------------------------- 3. scope

def test_uses_project_chat_scope(chat_store: ChatStore, project_a: Project):
    p = PlanerChatPersistence(project=project_a, chat_store=chat_store)
    chat = p.get_or_create_chat()
    assert chat.scope == project_a.chat_scope == f"project:{project_a.id}"


def test_scope_is_never_daily(chat_store: ChatStore, project_a: Project):
    p = PlanerChatPersistence(project=project_a, chat_store=chat_store)
    chat = p.get_or_create_chat()
    assert chat.scope != "daily"
    assert not chat.scope.startswith("daily")


# ----------------------------------------------------------------- 4. persistence

def test_record_user_turn_persists(persistence_a: PlanerChatPersistence):
    turn = persistence_a.record_user_turn("hello", turn_id="t-u1")
    assert turn.skipped is False
    assert turn.turn_id == "t-u1"
    assert turn.message_id > 0
    history = persistence_a.load_history()
    assert len(history) == 1
    assert history[0].role == "user"
    assert history[0].content == "hello"
    assert history[0].meta.get("turn_id") == "t-u1"
    assert history[0].meta.get("source") == "planer"


def test_record_assistant_turn_persists(persistence_a: PlanerChatPersistence):
    persistence_a.record_user_turn("hi", turn_id="t-u1")
    turn = persistence_a.record_assistant_turn("hello there", turn_id="t-a1")
    assert turn.skipped is False
    history = persistence_a.load_history()
    assert [m.role for m in history] == ["user", "assistant"]
    assert [m.content for m in history] == ["hi", "hello there"]


def test_record_turn_validates_role(persistence_a: PlanerChatPersistence):
    with pytest.raises(ValueError):
        persistence_a.record_turn("hacker", "x", turn_id="t-x")
    with pytest.raises(ValueError):
        persistence_a.record_turn("", "x", turn_id="t-x")


def test_record_turn_rejects_none_content(persistence_a: PlanerChatPersistence):
    with pytest.raises(ValueError):
        persistence_a.record_turn("user", None, turn_id="t-x")


def test_record_turn_generates_turn_id_when_missing(persistence_a: PlanerChatPersistence):
    turn = persistence_a.record_user_turn("hi")
    assert turn.turn_id
    assert turn.turn_id.startswith("turn-")


# ----------------------------------------------------------------- 5. idempotency

def test_duplicate_turn_id_is_skipped(persistence_a: PlanerChatPersistence):
    first = persistence_a.record_user_turn("hi", turn_id="t-1")
    second = persistence_a.record_user_turn("hi", turn_id="t-1")
    assert first.skipped is False
    assert second.skipped is True
    assert first.message_id == second.message_id
    history = persistence_a.load_history()
    assert len(history) == 1


def test_different_turn_ids_create_different_messages(persistence_a: PlanerChatPersistence):
    persistence_a.record_user_turn("a", turn_id="t-1")
    persistence_a.record_user_turn("b", turn_id="t-2")
    persistence_a.record_user_turn("c", turn_id="t-3")
    history = persistence_a.load_history()
    assert [m.content for m in history] == ["a", "b", "c"]


def test_re_recording_after_failure_preserves_idempotency(
    persistence_a: PlanerChatPersistence,
):
    # First call persists.
    persistence_a.record_assistant_turn("draft", turn_id="t-1")
    # Caller retries — same id, different content (e.g. corrected
    # answer). The second call is skipped; the original message is
    # preserved.
    retry = persistence_a.record_assistant_turn("corrected", turn_id="t-1")
    assert retry.skipped is True
    history = persistence_a.load_history()
    assert [m.content for m in history] == ["draft"]


# ----------------------------------------------------------------- 6. restore

def test_history_restores_after_recreating_persistence(
    chat_db_path: str, project_a: Project
):
    s1 = ChatStore(db_path=chat_db_path)
    p1 = PlanerChatPersistence(project=project_a, chat_store=s1)
    p1.get_or_create_chat()
    p1.record_user_turn("u1", turn_id="t-u1")
    p1.record_assistant_turn("a1", turn_id="t-a1")
    p1.record_user_turn("u2", turn_id="t-u2")
    p1.record_assistant_turn("a2", turn_id="t-a2")
    s1.close()

    s2 = ChatStore(db_path=chat_db_path)
    p2 = PlanerChatPersistence(project=project_a, chat_store=s2)
    history = p2.load_history()
    assert [m.role for m in history] == ["user", "assistant", "user", "assistant"]
    assert [m.content for m in history] == ["u1", "a1", "u2", "a2"]
    s2.close()


def test_has_history(persistence_a: PlanerChatPersistence):
    assert persistence_a.has_history() is False
    persistence_a.record_user_turn("hi", turn_id="t-1")
    assert persistence_a.has_history() is True


def test_load_history_empty_when_no_chat(chat_store: ChatStore, project_a: Project):
    p = PlanerChatPersistence(project=project_a, chat_store=chat_store)
    assert p.load_history() == []


# ----------------------------------------------------------------- 7. project isolation

def test_project_a_b_isolation(
    chat_store: ChatStore, project_a: Project, project_b: Project
):
    pa = PlanerChatPersistence(project=project_a, chat_store=chat_store)
    pb = PlanerChatPersistence(project=project_b, chat_store=chat_store)
    pa.record_user_turn("for A", turn_id="t-a-u1")
    pa.record_assistant_turn("A response", turn_id="t-a-a1")
    pb.record_user_turn("for B", turn_id="t-b-u1")
    pb.record_assistant_turn("B response", turn_id="t-b-a1")

    a_history = pa.load_history()
    b_history = pb.load_history()
    assert [m.content for m in a_history] == ["for A", "A response"]
    assert [m.content for m in b_history] == ["for B", "B response"]
    # No cross-contamination
    assert all("B" not in m.content for m in a_history)
    assert all("A" not in m.content for m in b_history)


def test_chat_ids_distinct_per_project(chat_store: ChatStore, project_a: Project, project_b: Project):
    pa = PlanerChatPersistence(project=project_a, chat_store=chat_store)
    pb = PlanerChatPersistence(project=project_b, chat_store=chat_store)
    assert pa.get_or_create_chat().id != pb.get_or_create_chat().id


# ----------------------------------------------------------------- 8. rename does not change identity

def test_renaming_project_preserves_chat_identity(
    chat_store: ChatStore, project_store: ProjectStore, project_a: Project
):
    project_store.create_project(project_a)
    p = PlanerChatPersistence(project=project_a, chat_store=chat_store)
    chat_id_before = p.get_or_create_chat().id
    # Rename the project
    renamed = project_store.rename_project(project_a.id, "Project Alpha Renamed")
    assert renamed is not None and renamed.id == project_a.id
    # The persistence object should still find the same chat
    chat_id_after = p.get_or_create_chat().id
    assert chat_id_after == chat_id_before


def test_renamed_project_still_loads_own_history(
    chat_db_path: str, project_a: Project
):
    s = ChatStore(db_path=chat_db_path)
    p = PlanerChatPersistence(project=project_a, chat_store=s)
    p.get_or_create_chat()
    p.record_user_turn("hello", turn_id="t-1")
    s.close()

    renamed = Project.from_dict({**project_a.to_dict(), "name": "renamed"})
    assert renamed.id == project_a.id
    s2 = ChatStore(db_path=chat_db_path)
    p2 = PlanerChatPersistence(project=renamed, chat_store=s2)
    history = p2.load_history()
    assert [m.content for m in history] == ["hello"]
    s2.close()


# ----------------------------------------------------------------- 9. daily isolation

def test_daily_chat_does_not_contain_planer_messages(
    chat_store: ChatStore, project_a: Project
):
    # Planer side
    p = PlanerChatPersistence(project=project_a, chat_store=chat_store)
    p.get_or_create_chat()
    p.record_user_turn("planer message", turn_id="t-p1")

    # Daily side
    daily_chat = chat_store.create_chat("daily", "", "Daily chat")
    chat_store.append_message(daily_chat.id, "user", "daily message", meta={"x": 1})

    # Project history is project-only
    project_history = p.load_history()
    assert all("daily" not in m.content for m in project_history)
    assert [m.content for m in project_history] == ["planer message"]

    # Daily history is daily-only
    daily_history = chat_store.load_history(daily_chat.id)
    assert [m.content for m in daily_history] == ["daily message"]
    assert all("planer" not in m.content for m in daily_history)


def test_planer_scope_is_distinct_from_daily(chat_store: ChatStore, project_a: Project):
    p = PlanerChatPersistence(project=project_a, chat_store=chat_store)
    planer_chat = p.get_or_create_chat()
    daily_chat = chat_store.create_chat("daily", "", "Daily chat")
    assert planer_chat.scope != daily_chat.scope
    assert planer_chat.scope.startswith("project:")
    assert daily_chat.scope == "daily"


def test_listing_daily_does_not_include_planer(
    chat_store: ChatStore, project_a: Project
):
    p = PlanerChatPersistence(project=project_a, chat_store=chat_store)
    p.get_or_create_chat()
    p.record_user_turn("hi", turn_id="t-1")
    chat_store.create_chat("daily", "", "Daily chat")
    # Daily list: only daily chat
    daily_chats = chat_store.list_chats("daily", "")
    assert {c.title for c in daily_chats} == {"Daily chat"}
    # Project list: only the planer chat
    project_chats = chat_store.list_chats(f"project:{project_a.id}", project_a.id)
    assert {c.title for c in project_chats} == {PLANER_CHAT_TITLE}


# ----------------------------------------------------------------- 10. plan / chat separation

def test_persistence_does_not_modify_project_plan(
    project_store: ProjectStore,
    chat_db_path: str,
    project_a: Project,
):
    project_store.create_project(project_a)
    plan = {
        "understanding": {"original_idea": "X"},
        "features": [{"id": "f1", "name": "F1"}],
    }
    project_store.set_plan(project_a.id, plan)
    plan_before = project_store.get_project(project_a.id).plan

    s = ChatStore(db_path=chat_db_path)
    p = PlanerChatPersistence(project=project_a, chat_store=s)
    p.get_or_create_chat()
    for i in range(5):
        p.record_user_turn(f"turn {i}", turn_id=f"t-{i}")
        p.record_assistant_turn(f"reply {i}", turn_id=f"r-{i}")
    s.close()

    plan_after = project_store.get_project(project_a.id).plan
    assert plan_after == plan_before


def test_persistence_does_not_modify_project_lifecycle(
    project_store: ProjectStore,
    chat_db_path: str,
    project_a: Project,
):
    from giscode.projects import ProjectLifecycle
    project_store.create_project(project_a)
    p = PlanerChatPersistence(project=project_a, chat_store=ChatStore(db_path=chat_db_path))
    p.get_or_create_chat()
    p.record_user_turn("hi", turn_id="t-1")
    # Project lifecycle is still PLANNING
    assert project_store.get_project(project_a.id).lifecycle == ProjectLifecycle.PLANNING


# ----------------------------------------------------------------- 11. delete_planer_chat

def test_delete_planer_chat(persistence_a: PlanerChatPersistence):
    persistence_a.get_or_create_chat()
    persistence_a.record_user_turn("x", turn_id="t-1")
    assert persistence_a.has_history() is True
    assert persistence_a.delete_planer_chat() is True
    assert persistence_a.has_history() is False


def test_delete_planer_chat_when_none(persistence_a: PlanerChatPersistence):
    assert persistence_a.delete_planer_chat() is False


# ----------------------------------------------------------------- 12. failure handling

def test_invalid_role_raises_value_error(persistence_a: PlanerChatPersistence):
    with pytest.raises(ValueError):
        persistence_a.record_turn("not-a-role", "x", turn_id="t-x")


def test_persistence_constructor_rejects_none():
    from giscode.projects import Project
    p = Project.create("X", "C:/tmp/x")
    with pytest.raises(ValueError):
        PlanerChatPersistence(project=p, chat_store=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PlanerChatPersistence(project=None, chat_store=ChatStore(db_path=":memory:"))  # type: ignore[arg-type]


def test_chat_store_failure_is_reported(tmp_path: Path, project_a: Project):
    """When the underlying ChatStore raises during append_message,
    the persistence layer must surface a clear
    :class:`PlanerChatPersistenceError` (not a silent success)."""
    class _FailingChatStore:
        def __init__(self, real: ChatStore) -> None:
            self._real = real
            self.fail = False

        def get_chat(self, chat_id):
            return self._real.get_chat(chat_id)

        def list_chats(self, scope, scope_id):
            return self._real.list_chats(scope, scope_id)

        def create_chat(self, scope, scope_id, title):
            return self._real.create_chat(scope, scope_id, title)

        def append_message(self, chat_id, role, content, meta=None):
            if self.fail:
                raise RuntimeError("simulated db failure")
            return self._real.append_message(chat_id, role, content, meta=meta)

        def load_history(self, chat_id):
            return self._real.load_history(chat_id)

        def delete_chat(self, chat_id):
            return self._real.delete_chat(chat_id)

    real = ChatStore(db_path=str(tmp_path / "chats.db"))
    failing = _FailingChatStore(real)
    p = PlanerChatPersistence(project=project_a, chat_store=failing)  # type: ignore[arg-type]
    p.get_or_create_chat()  # works
    failing.fail = True
    with pytest.raises(PlanerChatPersistenceError):
        p.record_user_turn("x", turn_id="t-1")
    # The chat was not silently updated
    chat = p.find_existing_chat()
    assert chat is not None
    assert real.load_history(chat.id) == []
    real.close()


def test_persistence_failure_does_not_corrupt_plan(
    project_store: ProjectStore,
    tmp_path: Path,
    project_a: Project,
):
    project_store.create_project(project_a)
    plan_before = project_store.get_project(project_a.id).plan

    class _AlwaysFailingChatStore:
        def get_chat(self, chat_id): return None
        def list_chats(self, scope, scope_id): return []
        def create_chat(self, scope, scope_id, title):
            raise RuntimeError("nope")
        def append_message(self, *a, **kw): raise RuntimeError("nope")
        def load_history(self, chat_id): return []
        def delete_chat(self, chat_id): return False

    p = PlanerChatPersistence(
        project=project_a,
        chat_store=_AlwaysFailingChatStore(),  # type: ignore[arg-type]
    )
    with pytest.raises(PlanerChatPersistenceError):
        p.record_user_turn("x", turn_id="t-1")
    # Project plan is intact
    assert project_store.get_project(project_a.id).plan == plan_before


# ----------------------------------------------------------------- 13. ordering

def test_conversation_preserves_chronological_order(persistence_a: PlanerChatPersistence):
    pairs = [
        ("user", "u1", "t-1"),
        ("assistant", "a1", "r-1"),
        ("user", "u2", "t-2"),
        ("assistant", "a2", "r-2"),
        ("user", "u3", "t-3"),
        ("assistant", "a3", "r-3"),
    ]
    for role, content, tid in pairs:
        persistence_a.record_turn(role, content, turn_id=tid)
    history = persistence_a.load_history()
    assert [(m.role, m.content) for m in history] == [(r, c) for r, c, _ in pairs]
    ids = [m.id for m in history]
    assert ids == sorted(ids)


# ----------------------------------------------------------------- 14. thread safety

def test_concurrent_recording_is_safe(chat_db_path: str, project_a: Project):
    """The PlanerChatPersistence layer is **not** designed to be
    shared across threads as a single instance. The recommended
    pattern (documented on the class) is one ``ChatStore`` and one
    ``PlanerChatPersistence`` per thread.

    This test verifies the realistic flow: each thread opens its
    own connection, serialises its own writes (the GUI does this
    implicitly because Tk runs the worker thread on its own), and
    all turns end up persisted exactly once thanks to the
    per-turn idempotency check.
    """
    errors: list = []
    lock = threading.Lock()

    def worker(prefix: str, n: int) -> None:
        try:
            s = ChatStore(db_path=chat_db_path)
            try:
                p = PlanerChatPersistence(project=project_a, chat_store=s)
                p.get_or_create_chat()
                for i in range(n):
                    p.record_user_turn(f"{prefix}-{i}", turn_id=f"{prefix}-{i}")
                    p.record_assistant_turn(f"{prefix}-r{i}", turn_id=f"{prefix}-r{i}")
            finally:
                s.close()
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(f"t{i}", 3)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    # Read back the final state with a fresh connection
    s = ChatStore(db_path=chat_db_path)
    try:
        p = PlanerChatPersistence(project=project_a, chat_store=s)
        history = p.load_history()
    finally:
        s.close()
    # 2 threads * 3 pairs * 2 = 12 turns total
    assert len(history) == 12
    # Every persisted message must carry a turn_id and the source tag
    assert all(m.meta.get("source") == "planer" for m in history)
    assert all(m.meta.get("turn_id") for m in history)
    # And the turn ids are unique (no duplicates)
    turn_ids = [m.meta.get("turn_id") for m in history]
    assert len(set(turn_ids)) == len(turn_ids)


def test_persistence_idempotency_under_repeated_calls(chat_db_path: str, project_a: Project):
    """Repeated calls with the same ``turn_id`` must never create
    duplicate messages, even if the caller is retrying after a
    network blip or process restart."""
    s = ChatStore(db_path=chat_db_path)
    try:
        p = PlanerChatPersistence(project=project_a, chat_store=s)
        p.get_or_create_chat()
        for _ in range(5):
            p.record_user_turn("hello", turn_id="same-id")
            p.record_assistant_turn("hi", turn_id="other-id")
        history = p.load_history()
    finally:
        s.close()
    assert len(history) == 2
    assert {m.meta.get("turn_id") for m in history} == {"same-id", "other-id"}


# ----------------------------------------------------------------- 15. clear_cached_chat_id

def test_clear_cached_chat_id(persistence_a: PlanerChatPersistence):
    chat1 = persistence_a.get_or_create_chat()
    persistence_a.clear_cached_chat_id()
    chat2 = persistence_a.get_or_create_chat()
    assert chat1.id == chat2.id


# ----------------------------------------------------------------- 16. additional Project chat does not get confused with Planer chat

def test_user_can_have_additional_project_chats_without_confusion(
    chat_store: ChatStore, project_a: Project
):
    p = PlanerChatPersistence(project=project_a, chat_store=chat_store)
    planer_chat = p.get_or_create_chat()
    # User creates additional chats on the same project
    chat_store.create_chat(f"project:{project_a.id}", project_a.id, "Notes")
    chat_store.create_chat(f"project:{project_a.id}", project_a.id, "Brainstorm")
    # Planer must still find the dedicated Planer chat
    found = p.find_existing_chat()
    assert found is not None
    assert found.id == planer_chat.id
    assert found.title == PLANER_CHAT_TITLE


# ----------------------------------------------------------------- 17. isolation: real user chats.db not touched

def test_real_user_chats_db_is_not_touched(tmp_path: Path, project_a: Project):
    from giscode.storage.chat_store import DEFAULT_DB_PATH
    real = Path(DEFAULT_DB_PATH)
    if real.exists():
        pytest.skip("user chats.db already exists; cannot assert absence")
    s = ChatStore(db_path=str(tmp_path / "isolated.db"))
    try:
        p = PlanerChatPersistence(project=project_a, chat_store=s)
        p.get_or_create_chat()
        p.record_user_turn("hi", turn_id="t-1")
    finally:
        s.close()
    assert not real.exists()
