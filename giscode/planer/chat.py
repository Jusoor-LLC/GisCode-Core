"""Persistent Planer project chat.

This module is a small, focused integration layer that:

* uses the existing :class:`giscode.storage.ChatStore` to persist
  Planer conversation turns,
* scopes every chat to ``project:<project_id>`` (the existing
  :attr:`giscode.projects.Project.chat_scope`),
* gives each project exactly one Planer chat (deterministic lookup
  by scope + scope_id + canonical title),
* prevents duplicate persistence via per-turn idempotency keys
  stored in the message's ``meta`` JSON,
* exposes a clean interface for the future Project Workspace GUI.

It deliberately does **not**:

* modify :class:`ChatStore` or its schema,
* store or modify the structured :class:`ProjectPlan` (that lives
  in :class:`giscode.projects.ProjectStore`),
* introduce a second chat database,
* make the deterministic :class:`PlanerEngine` aware of
  persistence,
* modify the LLM-driven :class:`PlanerAI`,
* touch the coding/build/test/verification pipeline.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Iterable, Optional

from giscode.projects.model import Project
from giscode.storage.chat_store import (
    ChatRecord,
    ChatStore,
    MessageRecord,
)


PLANER_CHAT_TITLE = "Planer"
_VALID_ROLES = frozenset({"user", "assistant", "system"})


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time()%1)*1000):03d}Z"


def _new_turn_id() -> str:
    return f"turn-{uuid.uuid4().hex}"


@dataclass
class PersistedTurn:
    turn_id: str
    role: str
    content: str
    ts: str
    message_id: int
    skipped: bool = False

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "role": self.role,
            "content": self.content,
            "ts": self.ts,
            "message_id": self.message_id,
            "skipped": self.skipped,
        }


class PlanerChatPersistenceError(RuntimeError):
    """Raised when a Planer chat persistence operation fails.

    Caller-facing failures (missing project, missing chat, invalid
    inputs) are raised as :class:`ValueError` or :class:`KeyError`
    to match the existing codebase's conventions. This class is
    reserved for unexpected storage / I/O failures.
    """


class PlanerChatPersistence:
    """Adapter that persists the Planer conversation for a single
    project.

    The class is intentionally stateful but narrow: it owns a
    reference to a :class:`ChatStore` and a :class:`Project` (or a
    raw project id) and a cached ``chat_id``. All public methods are
    safe to call multiple times; the idempotency machinery lives
    in :meth:`record_turn`.

    Thread safety
    -------------
    :class:`ChatStore` serializes its SQLite operations and permits a
    connection to be used by the Project Workspace UI and its Planner
    worker. A persistence adapter's cached chat id is lightweight session
    state; callers should not concurrently mutate the same adapter from
    multiple writer workers.
    """

    def __init__(
        self,
        project: Project,
        chat_store: ChatStore,
    ) -> None:
        if project is None:
            raise ValueError("project is required")
        if chat_store is None:
            raise ValueError("chat_store is required")
        self._project = project
        self._chat_store = chat_store
        self._chat_id: Optional[str] = None  # cached after first lookup

    # ------------------------------------------------------------------ props

    @property
    def project(self) -> Project:
        return self._project

    @property
    def project_id(self) -> str:
        return self._project.id

    @property
    def scope(self) -> str:
        return self._project.chat_scope

    @property
    def chat_store(self) -> ChatStore:
        return self._chat_store

    @property
    def chat_id(self) -> Optional[str]:
        return self._chat_id

    # ------------------------------------------------------------------ chat

    def get_or_create_chat(self) -> ChatRecord:
        """Return the existing Planer chat for this project, or create
        a new one. The result is cached so subsequent calls are
        O(1)."""
        if self._chat_id is not None:
            cached = self._chat_store.get_chat(self._chat_id)
            if cached is not None:
                return cached
            # The cached id was lost (e.g. DB was wiped). Fall through
            # and re-resolve.
            self._chat_id = None

        scope = self.scope
        scope_id = self.project_id
        try:
            existing_list = self._chat_store.list_chats(scope, scope_id)
        except Exception as e:
            raise PlanerChatPersistenceError(
                f"failed to list chats for project {self.project_id!r}: "
                f"{type(e).__name__}: {e}"
            ) from e
        for existing in existing_list:
            if existing.title == PLANER_CHAT_TITLE:
                self._chat_id = existing.id
                return existing
        # None found — create.
        try:
            chat = self._chat_store.create_chat(scope, scope_id, PLANER_CHAT_TITLE)
        except Exception as e:
            raise PlanerChatPersistenceError(
                f"failed to create planer chat for project {self.project_id!r}: "
                f"{type(e).__name__}: {e}"
            ) from e
        self._chat_id = chat.id
        return chat

    def find_existing_chat(self) -> Optional[ChatRecord]:
        """Find the existing Planer chat for this project without
        creating a new one."""
        if self._chat_id is not None:
            cached = self._chat_store.get_chat(self._chat_id)
            if cached is not None:
                return cached
            self._chat_id = None
        for existing in self._chat_store.list_chats(self.scope, self.project_id):
            if existing.title == PLANER_CHAT_TITLE:
                self._chat_id = existing.id
                return existing
        return None

    def delete_planer_chat(self) -> bool:
        """Delete the Planer chat for this project, if one exists.
        Used by future 'reset' flows; not part of normal use."""
        chat = self.find_existing_chat()
        if chat is None:
            return False
        ok = self._chat_store.delete_chat(chat.id)
        if ok:
            self._chat_id = None
        return ok

    # ------------------------------------------------------------------ turns

    def record_turn(
        self,
        role: str,
        content: str,
        turn_id: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> PersistedTurn:
        """Append a single conversation turn to the Planer chat.

        * Idempotent on ``turn_id``: a turn whose id has already
          been persisted is returned with ``skipped=True`` and no
          new row is written.
        * If ``turn_id`` is ``None``, a fresh id is generated and
          the turn is always appended.
        * ``meta`` is merged with ``{"turn_id": ..., "source":
          "planer"}``; the merged dict is stored in the message's
          ``meta_json`` field.

        Raises:
            ValueError: if ``role`` is not in the valid set or
                ``content`` is ``None``.
            KeyError: if the Planer chat could not be created.
        """
        if role not in _VALID_ROLES:
            raise ValueError(
                f"invalid role {role!r}; must be one of {sorted(_VALID_ROLES)}"
            )
        if content is None:
            raise ValueError("content must not be None")

        chat = self.get_or_create_chat()
        tid = turn_id or _new_turn_id()
        merged_meta: dict = {"turn_id": tid, "source": "planer"}
        if meta:
            for k, v in meta.items():
                merged_meta.setdefault(k, v)

        # Idempotency check: do we already have a message with this
        # turn_id in this chat? We rely on the per-chat history
        # (cheap for a Planer chat that is normally short).
        existing = self._find_turn(chat.id, tid)
        if existing is not None:
            return PersistedTurn(
                turn_id=tid,
                role=role,
                content=content,
                ts=existing.ts,
                message_id=existing.id,
                skipped=True,
            )

        # We do not store the content redundantly in meta — keep meta
        # small and unambiguous. ChatStore stores the content itself.
        try:
            msg = self._chat_store.append_message(
                chat_id=chat.id,
                role=role,
                content=content,
                meta=merged_meta,
            )
        except Exception as e:
            raise PlanerChatPersistenceError(
                f"failed to persist planer turn for project {self.project_id!r}: "
                f"{type(e).__name__}: {e}"
            ) from e

        return PersistedTurn(
            turn_id=tid,
            role=role,
            content=content,
            ts=msg.ts,
            message_id=msg.id,
            skipped=False,
        )

    def record_user_turn(
        self,
        content: str,
        turn_id: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> PersistedTurn:
        """Record a user turn.

        ``meta`` is stored in the message's existing ``meta_json``
        column; it carries no schema change. Image attachments are
        recorded this way so a stored turn still says which picture it
        referred to.
        """
        return self.record_turn("user", content, turn_id=turn_id, meta=meta)

    def record_assistant_turn(
        self,
        content: str,
        turn_id: Optional[str] = None,
    ) -> PersistedTurn:
        return self.record_turn("assistant", content, turn_id=turn_id)

    def _find_turn(self, chat_id: str, turn_id: str) -> Optional[MessageRecord]:
        """Return the first message in ``chat_id`` whose
        ``meta.turn_id`` matches ``turn_id``, or ``None``."""
        for msg in self._chat_store.load_history(chat_id):
            meta = msg.meta or {}
            if str(meta.get("turn_id", "")) == str(turn_id):
                return msg
        return None

    # ------------------------------------------------------------------ restore

    def load_history(self) -> list[MessageRecord]:
        """Return the persisted Planer conversation in chronological
        order. If no Planer chat exists yet, returns an empty list."""
        chat = self.find_existing_chat()
        if chat is None:
            return []
        return self._chat_store.load_history(chat.id)

    def has_history(self) -> bool:
        chat = self.find_existing_chat()
        if chat is None:
            return False
        return self._chat_store.count_messages(chat.id) > 0

    def clear_cached_chat_id(self) -> None:
        """Reset the cached chat id. Useful in tests; not normally
        needed at runtime."""
        self._chat_id = None


__all__ = [
    "PLANER_CHAT_TITLE",
    "PersistedTurn",
    "PlanerChatPersistence",
    "PlanerChatPersistenceError",
]
