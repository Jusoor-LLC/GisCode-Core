"""Planner chat persistence recovery for a damaged ``chats.db``.

Regression coverage for the reported failure: with an Open Existing
project selected, sending a Planner turn raised::

    failed to persist planer turn: PlanerChatPersistenceError:
    failed to create planer chat for project 'proj-...':
    DatabaseError: database disk image is malformed

The cause was not the project identity: a damaged b-tree in
``~/.giscode/chats.db`` lets the store open, read its schema, and even
return an empty ``list_chats`` result, so the first real write is the
first thing that fails. :class:`giscode.storage.ChatStore` now applies
the same quarantine-and-rebuild recovery that
:class:`giscode.projects.store.ProjectStore` already used.

All tests use temporary databases under ``tmp_path``; the user's real
``~/.giscode/chats.db`` is never touched, and no external/transferred
project directory is ever read.
"""
import os
import sqlite3
from pathlib import Path

import pytest

from giscode.gui.project_workspace import ProjectWorkspaceController
from giscode.planer import PLANER_CHAT_TITLE, PlanerChatPersistence
from giscode.projects import Project, ProjectStore
from giscode.projects.workspace import ProjectWorkspaceError
from giscode.storage import ChatStore


@pytest.fixture
def chat_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "chats.db")


@pytest.fixture
def project_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "projects.db")


def _fabricate_partially_corrupt_db(path: Path) -> None:
    """Create a real chats.db, then damage an interior page.

    The file still opens and reports its schema, which is what makes the
    damage so confusing at runtime.
    """
    ChatStore(db_path=str(path)).close()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute(
            "INSERT INTO chats (id, scope, scope_id, title, created_at, updated_at)"
            " VALUES ('chat-x','project:proj-x','proj-x','Planer','t','t')"
        )
        conn.execute(
            "INSERT INTO messages (chat_id, role, content, ts, meta_json)"
            " VALUES ('chat-x','user','hello','t','{}')"
        )
        conn.commit()
    finally:
        conn.close()
    raw = bytearray(path.read_bytes())
    for i in range(4096 * 2, min(4096 * 3, len(raw))):
        raw[i] = 0
    path.write_bytes(bytes(raw))


def _registered_project(project_db_path: str, root: Path) -> Project:
    """Register a project the way Open Existing resolves an already
    known folder: a real ProjectStore row plus an id."""
    store = ProjectStore(db_path=project_db_path)
    try:
        return store.create_project(Project.create("Project", str(root)))
    finally:
        store.close()


# ------------------------------------------------------------------ ChatStore recovery

def test_corrupt_chats_db_is_quarantined_and_rebuilt(chat_db_path: str, tmp_path: Path):
    """A damaged chats.db is moved aside and rebuilt, not silently lost."""
    db = Path(chat_db_path)
    _fabricate_partially_corrupt_db(db)

    probe = sqlite3.connect(chat_db_path)
    try:
        qc = [str(r[0]) for r in probe.execute("PRAGMA quick_check")]
    finally:
        probe.close()
    assert any("malformed" in q or "btreeInitPage" in q for q in qc)

    store = ChatStore(db_path=chat_db_path)
    try:
        assert store.recovery_note and "corrupt" in store.recovery_note

        # The damaged file was preserved under a timestamped sidecar:
        # the damage is still there, so nothing was silently discarded.
        backups = [p for p in tmp_path.iterdir() if ".corrupt-" in p.name]
        assert len(backups) == 1
        kept = sqlite3.connect(str(backups[0]))
        try:
            kept_qc = [str(r[0]) for r in kept.execute("PRAGMA quick_check")]
        except sqlite3.DatabaseError as exc:
            kept_qc = [type(exc).__name__]
        finally:
            kept.close()
        assert any(
            "malformed" in q or "btreeInitPage" in q or "DatabaseError" in q
            for q in kept_qc
        )

        # ...and the rebuilt store is fully usable for reads and writes.
        chat = store.create_chat("project:proj-x", "proj-x", PLANER_CHAT_TITLE)
        assert chat.id
        assert store.list_chats("project:proj-x", "proj-x")[0].id == chat.id
    finally:
        store.close()

    healed = sqlite3.connect(chat_db_path)
    try:
        assert [str(r[0]) for r in healed.execute("PRAGMA quick_check")] == ["ok"]
    finally:
        healed.close()


def test_healthy_chats_db_is_not_touched(chat_db_path: str, tmp_path: Path):
    """Normal persistence is unchanged: no quarantine, empty note."""
    store = ChatStore(db_path=chat_db_path)
    try:
        assert store.recovery_note == ""
        chat = store.create_chat("project:p1", "p1", PLANER_CHAT_TITLE)
        store.append_message(chat.id, "user", "hi")
        assert store.count_messages(chat.id) == 1
    finally:
        store.close()
    assert [p for p in tmp_path.iterdir() if ".corrupt-" in p.name] == []


def test_existing_chats_survive_a_healthy_reopen(chat_db_path: str):
    """A healthy store keeps its history across restarts (no data loss)."""
    first = ChatStore(db_path=chat_db_path)
    try:
        chat = first.create_chat("project:p1", "p1", PLANER_CHAT_TITLE)
        first.append_message(chat.id, "user", "remember me")
    finally:
        first.close()

    second = ChatStore(db_path=chat_db_path)
    try:
        assert second.recovery_note == ""
        chats = second.list_chats("project:p1", "p1")
        assert len(chats) == 1
        assert [m.content for m in second.load_history(chats[0].id)] == ["remember me"]
    finally:
        second.close()


# ------------------------------------------------------------------ planner send (registered / Open Existing)

def test_planner_send_succeeds_after_corrupt_db_recovery(
    chat_db_path: str, project_db_path: str, tmp_path: Path
):
    """The reported workflow: Open Existing + Send must persist the turn."""
    root = tmp_path / "existing_project"
    root.mkdir()
    _fabricate_partially_corrupt_db(Path(chat_db_path))

    chat_store = ChatStore(db_path=chat_db_path)
    project_store = ProjectStore(db_path=project_db_path)
    try:
        project = _registered_project(project_db_path, root)
        controller = ProjectWorkspaceController(project_store, chat_store)
        controller.open_project(project.id)

        result = controller.send_planer_message("Add a login screen")

        # The turn is really persisted, not swallowed or faked. (The
        # assistant turn is persisted too; its text depends on the
        # configured inference backend, which tests do not provide.)
        history = controller.load_chat_history()
        assert [m["content"] for m in history[:1]] == ["Add a login screen"]
        assert [m["role"] for m in history[:2]] == ["user", "assistant"]
        # Reopening in a fresh store sees the same conversation.
        chat = PlanerChatPersistence(project=project, chat_store=chat_store)
        assert len(chat.load_history()) == 2
    finally:
        project_store.close()
        chat_store.close()


def test_planner_send_and_history_are_intact_for_a_healthy_store(
    chat_db_path: str, project_db_path: str, tmp_path: Path
):
    """Registered/new project persistence is preserved end to end."""
    root = tmp_path / "registered"
    root.mkdir()
    chat_store = ChatStore(db_path=chat_db_path)
    project_store = ProjectStore(db_path=project_db_path)
    try:
        session = ProjectWorkspaceController(project_store, chat_store).create_project(
            name="New", root_path=str(root)
        )
        session.chat_with_planer("first")
        session.chat_with_planer("second")

        controller = ProjectWorkspaceController(project_store, chat_store)
        controller.open_project(session.project_id)
        history = controller.load_chat_history()
        # Two sends -> four persisted turns, in order.
        assert [m["content"] for m in history][0::2] == ["first", "second"]
        assert [m["role"] for m in history] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert chat_store.recovery_note == ""
    finally:
        project_store.close()
        chat_store.close()


# ------------------------------------------------------------------ unregistered path-only project

def test_unregistered_path_only_project_does_not_fake_persistence(chat_db_path: str, project_db_path: str, tmp_path: Path):
    """An unregistered folder keeps its documented limitation.

    It must fail loudly with the specific "no session" error rather
    than pretending to persist a turn.
    """
    root = tmp_path / "transferred"
    root.mkdir()
    chat_store = ChatStore(db_path=chat_db_path)
    project_store = ProjectStore(db_path=project_db_path)
    try:
        controller = ProjectWorkspaceController(project_store, chat_store)
        controller.select_existing_project_root(str(root))

        assert controller.current_project_id is None
        with pytest.raises(ProjectWorkspaceError) as excinfo:
            controller.send_planer_message("Add a login screen")
        assert "No active project session" in str(excinfo.value)

        # No chat was created for the unregistered folder.
        assert chat_store.list_chats(f"project:{root}", str(root)) == []
    finally:
        project_store.close()
        chat_store.close()
