from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from giscode.gui.main_window import MainWindow
from giscode.storage import ChatStore


def _history_window(store: ChatStore):
    window = MainWindow.__new__(MainWindow)
    window._chat_store = store
    window._project_store = MagicMock()
    window._conversation_records = []
    window._conversation_history_listbox = MagicMock()
    window._conversation_detail = MagicMock()
    window._chat_sessions = []
    window._current_chat_index = 0
    window._project_workspace_controller = MagicMock()
    window._project_workspace_view = MagicMock()
    window.root = MagicMock()
    window._select_tab = MagicMock()
    window._render_chat_tabs = MagicMock()
    window._render_current_chat = MagicMock()
    window.logger = MagicMock()
    return window


def test_history_lists_daily_and_project_conversations(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "history.db"))
    daily = store.create_chat("daily", "", "Daily conversation")
    project = store.create_chat("project:project-a", "project-a", "Planer")
    window = _history_window(store)

    window._refresh_conversation_history()

    assert {chat.id for chat in window._conversation_records} == {daily.id, project.id}
    assert window._conversation_history_listbox.insert.call_count == 2
    store.close()


def test_open_daily_history_restores_stable_chat_identity(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "history.db"))
    chat = store.create_chat("daily", "", "Saved chat")
    store.append_message(chat.id, "user", "hello")
    window = _history_window(store)
    window._refresh_conversation_history()
    target_index = next(
        i for i, record in enumerate(window._conversation_records) if record.id == chat.id
    )
    window._conversation_history_listbox.curselection.return_value = (target_index,)

    window._open_conversation()

    assert window._chat_sessions[0]["chat_id"] == chat.id
    assert window._chat_sessions[0]["history"] == [{"role": "user", "content": "hello"}]
    window._select_tab.assert_called_once_with("Chat")
    store.close()


def test_history_delete_removes_only_selected_chat(tmp_path, monkeypatch):
    store = ChatStore(db_path=str(tmp_path / "history.db"))
    first = store.create_chat("daily", "", "First")
    second = store.create_chat("daily", "", "Second")
    window = _history_window(store)
    window._refresh_conversation_history()
    target_index = next(
        i for i, record in enumerate(window._conversation_records) if record.id == first.id
    )
    window._conversation_history_listbox.curselection.return_value = (target_index,)
    monkeypatch.setattr(
        "giscode.gui.main_window.messagebox.askyesno",
        lambda *args, **kwargs: True,
    )

    window._delete_selected_conversation()

    assert store.get_chat(first.id) is None
    assert store.get_chat(second.id) is not None
    store.close()
