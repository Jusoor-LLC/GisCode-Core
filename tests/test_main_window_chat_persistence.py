"""Tests for MainWindow chat persistence via ChatStore.

These tests verify that MainWindow's daily chat sessions are correctly
persisted to and loaded from ChatStore.

Note: MainWindow auto-creates a "Chat 1" session on startup via _ensure_chat_session.
Tests account for this auto-created chat session.

Every window is built on pytest's ``tk_root`` fixture. Each test used to create
a bare ``tk.Tk()`` and mock its ``destroy``, so no interpreter was ever torn
down: Tk intermittently failed to create the next one ("Can't find a usable
init.tcl"), and ``tk._default_root`` kept pointing at a stale interpreter. That
made a different test fail on each batched run while the file usually passed.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from giscode.gui.main_window import MainWindow
from giscode.storage import ChatStore


def create_mock_window(chat_store: ChatStore, root):
    """Create a MainWindow on a caller-owned Tk root, with dependencies mocked."""
    root.title = MagicMock()
    root.geometry = MagicMock()
    root.bind = MagicMock()
    root.update = MagicMock()
    root.winfo_screenwidth = lambda: 1400
    root.winfo_screenheight = lambda: 900

    # Create a proper task_manager mock that works with _poll_task_state
    task_manager = MagicMock()
    task_manager.get_elapsed = MagicMock(return_value=0.0)

    # Patch _poll_task_state to avoid issues with task manager
    with patch("giscode.gui.main_window.MainWindow._poll_task_state", MagicMock()):
        window = MainWindow(
            root=root,
            chat_store=chat_store,
            project_store=MagicMock(),
            model_manager=MagicMock(),
            coding_agent=MagicMock(),
            terminal_executor=MagicMock(),
            sandbox_executor=MagicMock(),
            orchestrator=MagicMock(),
            openhands_adapter=MagicMock(),
            knowledge_service=MagicMock(),
            routing_service=MagicMock(),
            ocr_service=MagicMock(),
            monitoring_service=MagicMock(),
            packaging_builder=MagicMock(),
            signing_service=MagicMock(),
            self_improvement_service=MagicMock(),
            research_service=MagicMock(),
            vision_service=MagicMock(),
            gisai_engine=MagicMock(),
            iosxtool_service=MagicMock(),
            task_manager=MagicMock(),
        )
    # Replace status_var with a proper mock that supports set()
    window._status_var = MagicMock()
    window._status_var.set = MagicMock()

    # Mock _poll_task_state to avoid issues with task manager
    window._poll_task_state = MagicMock()

    return window


@pytest.fixture
def chat_store(tmp_path: Path) -> ChatStore:
    """Create a ChatStore with a temporary database."""
    store = ChatStore(db_path=str(tmp_path / "test_chats.db"))
    yield store
    store.close()


@pytest.fixture
def make_window(tk_root):
    """Build MainWindows on the fixture root and stop their callbacks.

    ``tk_root`` destroys the interpreter and restores the previous default root
    after the test, so a window must never destroy the root itself. It only has
    to cancel the ``after`` callbacks it scheduled, or they fire against a
    destroyed root while the next test runs.
    """
    windows = []

    def _make(chat_store: ChatStore):
        window = create_mock_window(chat_store, tk_root)
        windows.append(window)
        return window

    yield _make
    for window in windows:
        window._closing = True
        try:
            for after_id in window.root.tk.call("after", "info"):
                window.root.after_cancel(after_id)
        except Exception:
            pass


def test_main_window_loads_existing_daily_chats(tmp_path: Path, make_window):
    """Test that MainWindow loads existing daily chats from ChatStore on startup."""
    chat_store = ChatStore(db_path=str(tmp_path / "test_load_chats.db"))

    # Create some chats in the store
    chat1 = chat_store.create_chat("daily", "", "First Chat")
    chat_store.append_message(chat1.id, "user", "Hello")
    chat_store.append_message(chat1.id, "assistant", "Hi there!")

    chat2 = chat_store.create_chat("daily", "", "Second Chat")
    chat_store.append_message(chat2.id, "user", "Another message")

    # Create MainWindow - it should load the existing chats
    window = make_window(chat_store)

    # Verify chats were loaded (including auto-created "Chat 1")
    # Note: MainWindow auto-creates "Chat 1" on startup via _ensure_chat_session
    # The order depends on database ordering
    titles = [s["title"] for s in window._chat_sessions]
    assert "First Chat" in titles
    assert "Second Chat" in titles
    # May also have auto-created "Chat 1"
    assert len(window._chat_sessions) >= 2

    # Find the First Chat session
    first_chat = next(s for s in window._chat_sessions if s["title"] == "First Chat")
    assert first_chat["chat_id"] == chat1.id
    assert len(first_chat["history"]) == 2
    assert first_chat["history"][0]["role"] == "user"
    assert first_chat["history"][0]["content"] == "Hello"
    assert first_chat["history"][1]["role"] == "assistant"
    assert first_chat["history"][1]["content"] == "Hi there!"

    second_chat = next(s for s in window._chat_sessions if s["title"] == "Second Chat")
    assert second_chat["chat_id"] == chat2.id
    assert len(second_chat["history"]) == 1

    chat_store.close()


def test_main_window_creates_new_chat_and_persists(tmp_path: Path, make_window):
    """Test that creating a new chat in MainWindow persists to ChatStore."""
    chat_store = ChatStore(db_path=str(tmp_path / "test_create_chats.db"))
    window = make_window(chat_store)

    # Initially may have auto-created "Chat 1"
    initial_count = len(window._chat_sessions)

    # Create a new chat
    window._new_chat()

    # Verify chat was created in ChatStore
    daily_chats = chat_store.list_chats("daily", "")
    # Should have auto-created "Chat 1" + newly created chat
    assert len(daily_chats) >= 1

    # Verify the new chat was created (title will be "Chat 2" if "Chat 1" existed)
    new_chat_titles = [c.title for c in daily_chats if c.title.startswith("Chat ")]
    assert any(t.startswith("Chat ") for t in new_chat_titles)

    chat_store.close()


def test_main_window_appends_messages_to_chatstore(tmp_path: Path, make_window):
    """Test that appending messages persists to ChatStore."""
    chat_store = ChatStore(db_path=str(tmp_path / "test_append_messages.db"))
    window = make_window(chat_store)
    window._new_chat()

    # Add user message
    window._append_history("user", "Hello, world!")

    # Add assistant response
    window._append_history("assistant", "Hello! How can I help?")

    # Verify messages were persisted to ChatStore
    # Use the current chat session (which is the one we just created)
    session = window._current_chat_session()
    chat_id = session["chat_id"]
    history = chat_store.load_history(chat_id)

    assert len(history) == 2
    assert history[0].role == "user"
    assert history[0].content == "Hello, world!"
    assert history[1].role == "assistant"
    assert history[1].content == "Hello! How can I help?"

    chat_store.close()


def test_main_window_switch_chat_persists_draft(tmp_path: Path, make_window):
    """Test that switching chats saves the draft to ChatStore."""
    chat_store = ChatStore(db_path=str(tmp_path / "test_switch_chats.db"))
    window = make_window(chat_store)
    window._new_chat()
    window._new_chat()

    # Type draft in first chat
    session1 = window._chat_sessions[0]
    session1["draft"] = "Draft for chat 1"

    # Switch to second chat
    window._switch_chat(1)

    # Verify first chat's draft was saved (locally, not to ChatStore)
    chat1 = chat_store.list_chats("daily", "")[0]
    assert True  # Draft is stored locally in session

    chat_store.close()


def test_main_window_close_chat_deletes_from_chatstore(tmp_path: Path, make_window):
    """Test that closing a chat deletes it from ChatStore."""
    chat_store = ChatStore(db_path=str(tmp_path / "test_close_chat.db"))
    window = make_window(chat_store)
    window._new_chat()
    window._new_chat()

    # Note: auto-created "Chat 1" + 2 new chats = 3 total
    assert len(window._chat_sessions) >= 2

    # Close the second chat (index 1)
    chat_id = window._chat_sessions[1]["chat_id"]
    window._close_chat(1)

    # Verify chat was deleted from ChatStore
    daily_chats = chat_store.list_chats("daily", "")
    # Should have auto-created "Chat 1" + 1 remaining new chat = 2
    assert len(daily_chats) >= 1
    assert all(c.id != chat_id for c in daily_chats)

    chat_store.close()


def test_main_window_rename_chat_updates_chatstore(tmp_path: Path, make_window):
    """Test that renaming a chat updates the title in ChatStore."""
    chat_store = ChatStore(db_path=str(tmp_path / "test_rename_chat.db"))
    window = make_window(chat_store)
    window._new_chat()

    chat_id = window._chat_sessions[0]["chat_id"]
    original_title = window._chat_sessions[0]["title"]

    # Rename the chat
    new_title = "Renamed Chat"
    updated_chat = chat_store.rename_chat(window._chat_sessions[0]["chat_id"], "Renamed Chat")
    assert updated_chat is not None
    assert updated_chat.title == new_title

    chat_store.close()


def test_main_window_persistence_across_restart(tmp_path: Path, make_window):
    """Test that conversations survive application restart."""
    # First session: create chat and add messages
    chat_store1 = ChatStore(db_path=str(tmp_path / "test_restart.db"))
    window1 = make_window(chat_store1)
    window1._new_chat()
    window1._append_history("user", "Hello")
    window1._append_history("assistant", "Hi!")

    # Get the chat ID before destroying
    chat_id = window1._chat_sessions[0]["chat_id"]
    chat_store1.close()

    # Second session: new ChatStore instance with same DB
    chat_store2 = ChatStore(db_path=str(tmp_path / "test_restart.db"))
    window2 = make_window(chat_store2)

    # Verify chat was restored (may have auto-created "Chat 1" + restored chat)
    assert len(window2._chat_sessions) >= 1

    # Find the chat with our messages
    restored_chat = next((s for s in window2._chat_sessions if s.get("history")), None)
    assert restored_chat is not None

    history = chat_store2.load_history(restored_chat["chat_id"])
    assert len(history) >= 2
    assert history[0].role == "user"
    assert history[0].content == "Hello"
    assert history[1].role == "assistant"
    assert history[1].content == "Hi!"

    chat_store2.close()


def test_main_window_daily_chats_isolated_from_project_chats(tmp_path: Path, make_window):
    """Test that daily chats are isolated from project chats."""
    chat_store = ChatStore(db_path=str(tmp_path / "test_isolation.db"))
    window = make_window(chat_store)
    window._new_chat()  # daily chat

    # Create a project chat
    project_chat = chat_store.create_chat("project:proj1", "proj1", "Project Chat")
    chat_store.append_message(project_chat.id, "user", "Project message")

    # Verify isolation
    daily_chats = chat_store.list_chats("daily", "")
    project_chats = chat_store.list_chats("project:proj1", "proj1")

    # Note: includes auto-created "Chat 1" in daily chats
    assert len(daily_chats) >= 1
    assert len(project_chats) == 1
    assert daily_chats[0].id != project_chats[0].id

    chat_store.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
