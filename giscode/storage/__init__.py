"""Persistent storage layer for GisCode.

Currently exposes the multi-chat store used by the Daily Assistant
and (later) the Project Workspace. New persistence concerns should be
added here as additional modules (e.g. `project_store.py`).
"""

from giscode.storage.chat_store import ChatStore, ChatRecord, MessageRecord

__all__ = ["ChatStore", "ChatRecord", "MessageRecord"]
