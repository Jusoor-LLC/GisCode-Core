"""Unit tests for giscode.storage.ChatStore.

All tests use a temporary database file under pytest's `tmp_path` so
the user's real `~/.giscode/chats.db` is never touched.
"""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from giscode.storage.chat_store import (
    ChatRecord,
    ChatStore,
    MessageRecord,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test_chats.db")


@pytest.fixture
def store(db_path: str) -> ChatStore:
    s = ChatStore(db_path=db_path)
    yield s
    s.close()


# ------------------------------------------------------------------ 1. schema

def test_database_and_tables_created_automatically(tmp_path: Path):
    target = str(tmp_path / "autoinit.db")
    assert not os.path.exists(target)
    ChatStore(db_path=target).close()
    assert os.path.exists(target)
    conn = sqlite3.connect(target)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "chats" in names
    assert "messages" in names


def test_chats_schema_has_expected_columns(store: ChatStore):
    cur = store._conn.execute("PRAGMA table_info(chats)")
    cols = {row[1] for row in cur.fetchall()}
    assert {"id", "scope", "scope_id", "title", "created_at", "updated_at"} <= cols


def test_messages_schema_has_expected_columns(store: ChatStore):
    cur = store._conn.execute("PRAGMA table_info(messages)")
    cols = {row[1] for row in cur.fetchall()}
    assert {"id", "chat_id", "role", "content", "ts", "meta_json"} <= cols


def test_reopening_store_keeps_existing_data(db_path: str):
    s1 = ChatStore(db_path=db_path)
    chat = s1.create_chat("daily", "", "Persisted")
    s1.append_message(chat.id, "user", "hello")
    s1.close()
    s2 = ChatStore(db_path=db_path)
    chats = s2.list_chats("daily", "")
    assert len(chats) == 1
    assert chats[0].id == chat.id
    assert chats[0].title == "Persisted"
    s2.close()


# ------------------------------------------------------------------ 2. create

def test_create_chat_returns_record_with_required_fields(store: ChatStore):
    rec = store.create_chat("daily", "", "My chat")
    assert isinstance(rec, ChatRecord)
    assert rec.id.startswith("chat-")
    assert rec.scope == "daily"
    assert rec.scope_id == ""
    assert rec.title == "My chat"
    assert rec.created_at
    assert rec.updated_at
    assert rec.created_at == rec.updated_at


def test_create_chat_blank_title_defaults(store: ChatStore):
    rec = store.create_chat("daily", "", "   ")
    assert rec.title == "New Chat"


def test_create_chat_rejects_invalid_scope(store: ChatStore):
    with pytest.raises(ValueError):
        store.create_chat("weird", "x", "title")


def test_create_chat_accepts_project_scope(store: ChatStore):
    rec = store.create_chat("project:abc", "abc", "Planer chat")
    assert rec.scope == "project:abc"
    assert rec.scope_id == "abc"


# ------------------------------------------------------------------ 3. list

def test_list_chats_empty(store: ChatStore):
    assert store.list_chats("daily", "") == []


def test_list_chats_returns_newest_activity_first(store: ChatStore):
    a = store.create_chat("daily", "", "A")
    time.sleep(0.01)
    b = store.create_chat("daily", "", "B")
    time.sleep(0.01)
    c = store.create_chat("daily", "", "C")
    # Touch b so it becomes most-recently-active
    time.sleep(0.01)
    store.append_message(b.id, "user", "hi")
    chats = store.list_chats("daily", "")
    assert [ch.id for ch in chats] == [b.id, c.id, a.id]


# ------------------------------------------------------------------ 4/5. append

def test_append_user_message(store: ChatStore):
    chat = store.create_chat("daily", "", "X")
    m = store.append_message(chat.id, "user", "hello world")
    assert isinstance(m, MessageRecord)
    assert m.chat_id == chat.id
    assert m.role == "user"
    assert m.content == "hello world"
    assert m.id > 0
    assert m.ts


def test_append_assistant_message(store: ChatStore):
    chat = store.create_chat("daily", "", "X")
    m = store.append_message(chat.id, "assistant", "Hi there!")
    assert m.role == "assistant"
    assert m.content == "Hi there!"


def test_append_message_rejects_unknown_role(store: ChatStore):
    chat = store.create_chat("daily", "", "X")
    with pytest.raises(ValueError):
        store.append_message(chat.id, "robot", "beep")


def test_append_message_rejects_unknown_chat(store: ChatStore):
    with pytest.raises(KeyError):
        store.append_message("chat-does-not-exist", "user", "hi")


# ------------------------------------------------------------------ 6. chronological

def test_load_history_is_chronological(store: ChatStore):
    chat = store.create_chat("daily", "", "Order")
    store.append_message(chat.id, "user", "msg 1")
    store.append_message(chat.id, "assistant", "msg 2")
    store.append_message(chat.id, "user", "msg 3")
    history = store.load_history(chat.id)
    assert [m.content for m in history] == ["msg 1", "msg 2", "msg 3"]
    ids = [m.id for m in history]
    assert ids == sorted(ids)


def test_load_history_empty(store: ChatStore):
    chat = store.create_chat("daily", "", "Empty")
    assert store.load_history(chat.id) == []


# ------------------------------------------------------------------ 7. updated_at

def test_updated_at_changes_on_message(store: ChatStore):
    chat = store.create_chat("daily", "", "Touch")
    original = chat.updated_at
    time.sleep(0.02)
    store.append_message(chat.id, "user", "hi")
    refreshed = store.get_chat(chat.id)
    assert refreshed is not None
    assert refreshed.updated_at != original
    assert refreshed.updated_at >= original


def test_updated_at_changes_on_rename(store: ChatStore):
    chat = store.create_chat("daily", "", "Old")
    original = chat.updated_at
    time.sleep(0.02)
    renamed = store.rename_chat(chat.id, "New")
    assert renamed is not None
    assert renamed.title == "New"
    assert renamed.updated_at != original


def test_rename_unknown_chat_returns_none(store: ChatStore):
    assert store.rename_chat("chat-bogus", "x") is None


def test_rename_blank_title_raises(store: ChatStore):
    chat = store.create_chat("daily", "", "X")
    with pytest.raises(ValueError):
        store.rename_chat(chat.id, "   ")


# ------------------------------------------------------------------ 8. delete

def test_delete_chat(store: ChatStore):
    chat = store.create_chat("daily", "", "Doomed")
    assert store.delete_chat(chat.id) is True
    assert store.get_chat(chat.id) is None
    assert store.load_history(chat.id) == []


def test_delete_unknown_chat_returns_false(store: ChatStore):
    assert store.delete_chat("chat-nope") is False


# ------------------------------------------------------------------ 9. independence

def test_multiple_chats_remain_independent(store: ChatStore):
    a = store.create_chat("daily", "", "A")
    b = store.create_chat("daily", "", "B")
    store.append_message(a.id, "user", "for A")
    store.append_message(b.id, "user", "for B1")
    store.append_message(b.id, "user", "for B2")
    assert [m.content for m in store.load_history(a.id)] == ["for A"]
    assert [m.content for m in store.load_history(b.id)] == ["for B1", "for B2"]


def test_delete_one_chat_does_not_affect_others(store: ChatStore):
    a = store.create_chat("daily", "", "A")
    b = store.create_chat("daily", "", "B")
    store.append_message(a.id, "user", "a1")
    store.append_message(b.id, "user", "b1")
    assert store.delete_chat(a.id) is True
    assert store.get_chat(a.id) is None
    assert store.get_chat(b.id) is not None
    assert [m.content for m in store.load_history(b.id)] == ["b1"]


# ------------------------------------------------------------------ 10. isolation

def test_daily_chats_isolated_from_project_chats(store: ChatStore):
    daily = store.create_chat("daily", "", "Daily one")
    proj = store.create_chat("project:p1", "p1", "Project one")
    assert daily.id != proj.id
    assert store.list_chats("daily", "") == [daily]
    assert store.list_chats("project:p1", "p1") == [proj]


def test_project_chats_isolated_by_project_id(store: ChatStore):
    a = store.create_chat("project:p1", "p1", "P1-A")
    b = store.create_chat("project:p2", "p2", "P2-A")
    assert store.list_chats("project:p1", "p1") == [a]
    assert store.list_chats("project:p2", "p2") == [b]
    assert store.list_chats("project:p1", "p1") != store.list_chats("project:p2", "p2")


def test_list_all_chats_includes_daily_and_project_scopes(store: ChatStore):
    daily = store.create_chat("daily", "", "Daily")
    project = store.create_chat("project:p1", "p1", "Planer")
    chats = store.list_all_chats()
    assert {chat.id for chat in chats} == {daily.id, project.id}
    assert {chat.scope for chat in chats} == {"daily", "project:p1"}


# ------------------------------------------------------------------ 11. persistence

def test_persistence_across_close_reopen(db_path: str):
    s1 = ChatStore(db_path=db_path)
    chat = s1.create_chat("daily", "", "Persists")
    s1.append_message(chat.id, "user", "first")
    s1.append_message(chat.id, "assistant", "second")
    s1.close()
    s2 = ChatStore(db_path=db_path)
    chats = s2.list_chats("daily", "")
    assert len(chats) == 1 and chats[0].id == chat.id
    history = s2.load_history(chat.id)
    assert [m.content for m in history] == ["first", "second"]
    s2.close()


# ------------------------------------------------------------------ 12. metadata JSON

def test_metadata_round_trip(store: ChatStore):
    chat = store.create_chat("daily", "", "Meta")
    meta = {"model": "qwen", "tokens": 17, "tags": ["a", "b"], "nested": {"k": "v"}}
    m = store.append_message(chat.id, "assistant", "ok", meta=meta)
    assert m.meta == meta
    history = store.load_history(chat.id)
    assert history[-1].meta == meta


def test_metadata_default_is_empty_dict(store: ChatStore):
    chat = store.create_chat("daily", "", "Meta")
    m = store.append_message(chat.id, "user", "no meta")
    assert m.meta == {}


def test_metadata_none_becomes_empty_dict(store: ChatStore):
    chat = store.create_chat("daily", "", "Meta")
    m = store.append_message(chat.id, "user", "none meta", meta=None)
    assert m.meta == {}


# ------------------------------------------------------------------ 13. misc

def test_count_helpers(store: ChatStore):
    a = store.create_chat("daily", "", "A")
    store.create_chat("daily", "", "B")
    store.create_chat("project:p1", "p1", "P")
    assert store.count_chats() == 3
    assert store.count_chats("daily") == 2
    assert store.count_chats("project:p1") == 1
    store.append_message(a.id, "user", "x")
    store.append_message(a.id, "assistant", "y")
    assert store.count_messages(a.id) == 2


# ------------------------------------------------------------------ 14. do not touch real DB

def test_real_user_chats_db_is_not_created_when_using_tmp_path(tmp_path: Path, monkeypatch):
    """Constructing a ChatStore with a tmp_path must not touch the default DB.

    The default database is resolved through ``giscode_home()`` on every call,
    so this points the profile at a fresh directory instead of skipping when
    some other test in the session had already created a chats.db in the
    shared isolated home. That skip made the suite's skip count depend on test
    order rather than on the behaviour under test.
    """
    profile = tmp_path / "profile" / ".giscode"
    profile.mkdir(parents=True)
    monkeypatch.setenv("GISCODE_HOME", str(profile))
    default_db = profile / "chats.db"

    s = ChatStore(db_path=str(tmp_path / "isolated.db"))
    s.create_chat("daily", "", "temp")
    s.close()

    assert not default_db.exists()
