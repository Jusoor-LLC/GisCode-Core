"""SQLite-backed multi-chat persistence.

The store is intentionally decoupled from any GUI/agent so that both
the Daily Assistant and the future Project Workspace can share a single
storage layer with strict `scope` isolation.

Scope conventions:
    * "daily"            — chats that belong to the general Daily Assistant
    * "project:<id>"     — chats that belong to a specific project

A single database (`~/.giscode/chats.db`) is used; tables are created
on first use. All SQL is parameterized.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


from giscode.config.paths import giscode_home

VALID_SCOPES = ("daily",)
VALID_ROLES = ("system", "user", "assistant", "tool")


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time()%1)*1000):03d}Z"


def _default_db_path() -> str:
    return str(giscode_home() / "chats.db")


def __getattr__(name: str):
    """Lazy ``DEFAULT_DB_PATH`` so it follows ``GISCODE_HOME``."""
    if name == "DEFAULT_DB_PATH":
        return giscode_home() / "chats.db"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass
class ChatRecord:
    id: str
    scope: str
    scope_id: str
    title: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class MessageRecord:
    id: int
    chat_id: str
    role: str
    content: str
    ts: str
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "role": self.role,
            "content": self.content,
            "ts": self.ts,
            "meta": dict(self.meta),
        }


class ChatStore:
    """Thin SQLite wrapper providing the multi-chat API.

    Each instance owns one connection. Calls through this store are
    serialized by a re-entrant lock, allowing the same store instance to
    be used by the GUI thread and its Planner worker without concurrent
    access to the SQLite connection. There is intentionally no
    in-process cache — the source of truth is the database file.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _default_db_path()
        path = Path(self._db_path)
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.recovery_note: str = ""
        self._conn = sqlite3.connect(
            self._db_path,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        mode = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            self._enable_wal()
        self._create_schema()
        self._recover_if_corrupt()

    def _reconnect(self) -> None:
        """(Re)open the connection with the same pragmas as __init__."""
        self._conn = sqlite3.connect(
            self._db_path,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        mode = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            self._enable_wal()

    def _enable_wal(self) -> None:
        deadline = time.time() + 30.0
        while True:
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY:
                    raise
                if time.time() >= deadline:
                    raise
                time.sleep(0.02)

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "ChatStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ integrity

    def _recover_if_corrupt(self) -> None:
        """Detect a partially-corrupt SQLite file and rebuild it.

        This mirrors :meth:`giscode.projects.store.ProjectStore._recover_if_corrupt`,
        which already fixed this exact failure class for the project
        database. A genuinely SQLite file whose b-tree pages are
        damaged opens and reads its schema fine, so ``__init__`` (and
        ``_create_schema``'s no-op ``CREATE TABLE IF NOT EXISTS``)
        succeed. The damage only shows up later, on write: a
        ``list_chats`` scope query silently returns an empty list and
        the following ``create_chat`` insert raises ``sqlite3.
        DatabaseError: database disk image is malformed``. That surfaced
        to the user as ``failed to persist planer turn:
        PlanerChatPersistenceError: failed to create planer chat for
        project ...`` -- a Planner error that was really a storage
        error, and it made every chat send fail for that store.

        When ``quick_check`` reports corruption the damaged file is
        moved aside (``<db>.corrupt-<timestamp>``, including any
        ``-wal``/``-shm`` sidecars) and rebuilt fresh, so the store is
        usable and nothing is silently lost: the original bytes stay on
        disk and :attr:`recovery_note` records where.
        """
        try:
            rows = self._conn.execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError:
            rows = [("FAILED",)]
        if not rows:
            return
        problems = [str(r[0]).strip().lower() for r in rows]
        if all(p == "ok" for p in problems):
            return
        self._heal_corrupt_db(problems)

    def _heal_corrupt_db(self, problems: list[str]) -> None:
        import datetime as _dt

        note = "; ".join(problems[:3])
        self._conn.close()
        db = Path(self._db_path)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = str(db) + f".corrupt-{stamp}"
        for cand, tag in (
            (db, ""),
            (Path(str(db) + "-wal"), "-wal"),
            (Path(str(db) + "-shm"), "-shm"),
        ):
            if cand.exists():
                try:
                    os.replace(cand, Path(stem + tag))
                except OSError:
                    pass
        self._reconnect()
        self._create_schema()
        self.recovery_note = f"corrupt database rebuilt; backup at {stem} ({note})"

    # ------------------------------------------------------------------ schema

    def _create_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    id          TEXT PRIMARY KEY,
                    scope       TEXT NOT NULL,
                    scope_id    TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id   TEXT NOT NULL,
                    role      TEXT NOT NULL,
                    content   TEXT NOT NULL,
                    ts        TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chats_scope ON chats (scope, scope_id, updated_at DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, id ASC)"
            )

    # ------------------------------------------------------------------ helpers

    def _validate_scope(self, scope: str) -> None:
        if not scope or not isinstance(scope, str):
            raise ValueError("scope must be a non-empty string")
        if scope not in VALID_SCOPES and not scope.startswith("project:"):
            raise ValueError(
                f"unsupported scope {scope!r}; must be 'daily' or 'project:<id>'"
            )

    def _validate_role(self, role: str) -> None:
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role {role!r}; must be one of {VALID_ROLES}")

    def _touch_chat(self, chat_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE chats SET updated_at = ? WHERE id = ?",
                (_utc_now_iso(), chat_id),
            )

    # ------------------------------------------------------------------ chats

    def create_chat(self, scope: str, scope_id: str, title: str) -> ChatRecord:
        self._validate_scope(scope)
        if scope_id is None:
            scope_id = ""
        scope_id = str(scope_id)
        title = (title or "").strip() or "New Chat"
        chat_id = f"chat-{uuid.uuid4().hex}"
        now = _utc_now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO chats (id, scope, scope_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, scope, scope_id, title, now, now),
            )
        return ChatRecord(
            id=chat_id,
            scope=scope,
            scope_id=scope_id,
            title=title,
            created_at=now,
            updated_at=now,
        )

    def list_chats(self, scope: str, scope_id: str = "") -> list[ChatRecord]:
        self._validate_scope(scope)
        scope_id = str(scope_id or "")
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT id, scope, scope_id, title, created_at, updated_at
                FROM chats
                WHERE scope = ? AND scope_id = ?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                """,
                (scope, scope_id),
            )
            rows = cur.fetchall()
        return [
            ChatRecord(
                id=row["id"],
                scope=row["scope"],
                scope_id=row["scope_id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def list_all_chats(self) -> list[ChatRecord]:
        """Return every conversation, newest activity first."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT id, scope, scope_id, title, created_at, updated_at
                FROM chats
                ORDER BY updated_at DESC, created_at DESC, id DESC
                """
            )
            rows = cur.fetchall()
        return [
            ChatRecord(
                id=row["id"],
                scope=row["scope"],
                scope_id=row["scope_id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def get_chat(self, chat_id: str) -> Optional[ChatRecord]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, scope, scope_id, title, created_at, updated_at FROM chats WHERE id = ?",
                (chat_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return ChatRecord(
            id=row["id"],
            scope=row["scope"],
            scope_id=row["scope_id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def rename_chat(self, chat_id: str, title: str) -> Optional[ChatRecord]:
        if not title or not title.strip():
            raise ValueError("title must be a non-empty string")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE chats SET title = ?, updated_at = ? WHERE id = ?",
                (title.strip(), _utc_now_iso(), chat_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get_chat(chat_id)

    def delete_chat(self, chat_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------ messages

    def append_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        meta: Optional[dict] = None,
    ) -> MessageRecord:
        self._validate_role(role)
        if content is None:
            content = ""
        meta_str = json.dumps(meta or {}, ensure_ascii=False, sort_keys=True)
        ts = _utc_now_iso()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "SELECT 1 FROM chats WHERE id = ?",
                (chat_id,),
            )
            if cur.fetchone() is None:
                raise KeyError(f"chat {chat_id!r} does not exist")
            cur = self._conn.execute(
                """
                INSERT INTO messages (chat_id, role, content, ts, meta_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, role, content, ts, meta_str),
            )
            self._conn.execute(
                "UPDATE chats SET updated_at = ? WHERE id = ?",
                (ts, chat_id),
            )
        return MessageRecord(
            id=int(cur.lastrowid),
            chat_id=chat_id,
            role=role,
            content=content,
            ts=ts,
            meta=json.loads(meta_str),
        )

    def load_history(self, chat_id: str) -> list[MessageRecord]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT id, chat_id, role, content, ts, meta_json
                FROM messages
                WHERE chat_id = ?
                ORDER BY id ASC
                """,
                (chat_id,),
            )
            rows = cur.fetchall()
        out: list[MessageRecord] = []
        for row in rows:
            try:
                meta = json.loads(row["meta_json"] or "{}")
            except Exception:
                meta = {}
            out.append(
                MessageRecord(
                    id=int(row["id"]),
                    chat_id=row["chat_id"],
                    role=row["role"],
                    content=row["content"],
                    ts=row["ts"],
                    meta=meta,
                )
            )
        return out

    # ------------------------------------------------------------------ utilities

    def count_chats(self, scope: Optional[str] = None) -> int:
        if scope is not None:
            self._validate_scope(scope)
        with self._lock:
            if scope is None:
                cur = self._conn.execute("SELECT COUNT(*) AS c FROM chats")
            else:
                cur = self._conn.execute("SELECT COUNT(*) AS c FROM chats WHERE scope = ?", (scope,))
            return int(cur.fetchone()["c"])

    def count_messages(self, chat_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE chat_id = ?", (chat_id,)
            )
            return int(cur.fetchone()["c"])

    @property
    def db_path(self) -> str:
        return self._db_path


__all__ = [
    "ChatStore",
    "ChatRecord",
    "MessageRecord",
    "DEFAULT_DB_PATH",
    "VALID_SCOPES",
    "VALID_ROLES",
]
