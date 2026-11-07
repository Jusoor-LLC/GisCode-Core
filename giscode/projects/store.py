"""SQLite-backed persistent store for :class:`Project` records.

The store is intentionally independent of the existing
:class:`giscode.workspace.Workspace` and the existing
:class:`giscode.storage.ChatStore`. It is a focused, single-table,
single-responsibility persistence layer.

Compatibility contract with :class:`giscode.storage.ChatStore`:
    A :class:`Project`'s :attr:`Project.chat_scope` returns the
    ``project:<id>`` string the chat store already accepts. The
    project store does **not** write into the chat database.

SQLite is used (rather than reusing the existing knowledge base)
because the project lifecycle is a write-heavy, read-light,
machine-generated dataset with strict consistency requirements and
no need for the knowledge store's retrieval features. The
``knowledge.db`` is left untouched.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

from giscode.config.paths import giscode_home
from giscode.projects.lifecycle import (
    LifecycleError,
    ProjectLifecycle,
    assert_transition,
    can_transition,
)
from giscode.projects.model import Project


def _default_db_path() -> str:
    return str(giscode_home() / "projects.db")


def __getattr__(name: str):
    """Lazy ``DEFAULT_DB_PATH`` so it follows ``GISCODE_HOME``."""
    if name == "DEFAULT_DB_PATH":
        return giscode_home() / "projects.db"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ProjectStore:
    """Thread-safe SQLite CRUD + lifecycle transitions for projects.

    All SQL is parameterized. Lifecycle transitions are validated
    through :func:`assert_transition` so the :attr:`Project.lifecycle`
    field can never enter an illegal state via this store.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _default_db_path()
        path = Path(self._db_path)
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(self._db_path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()
        self._recover_if_corrupt()

    @property
    def db_path(self) -> str:
        return self._db_path

    def close(self) -> None:
        """Close the connection, serialized against any in-flight statement.

        A worker thread finishing a build (and verifying it) may still be reading
        or writing a lifecycle transition. Closing the SQLite handle underneath
        that thread is a C-level race rather than a Python error, so this takes
        the same lock every query takes: close waits for the current statement,
        and calls that arrive afterwards fail cleanly instead of crashing.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self) -> "ProjectStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ schema

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    root_path   TEXT NOT NULL,
                    lifecycle   TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    plan_json   TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_lifecycle ON projects (lifecycle, updated_at DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_name ON projects (name COLLATE NOCASE)"
            )

    # ------------------------------------------------------------------ integrity

    def _recover_if_corrupt(self) -> None:
        """Detect a partially-corrupt SQLite file and rebuild it.

        A file that is not SQLite at all raises ``sqlite3.DatabaseError``
        earlier (during ``journal_mode``/schema statements), preserving
        the existing contract for junk files. A genuinely SQLite file
        whose b-tree pages are damaged (e.g. ``btreeInitPage`` failure)
        opens and reads its schema fine, but silently returns empty
        scans while failing indexed access and inserts. That silent
        failure is what made the GUI show an empty project list. When
        ``quick_check`` reports corruption, the damaged file is moved
        aside (``<db>.corrupt-<timestamp>``) and rebuilt fresh so the
        store is usable and nothing is silently lost.
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
        self._conn = sqlite3.connect(self._db_path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()
        self.recovery_note = f"corrupt database rebuilt; backup at {stem} ({note})"

    # ------------------------------------------------------------------ helpers

    def _row_to_project(self, row: sqlite3.Row) -> Project:
        import json as _json

        try:
            plan = _json.loads(row["plan_json"] or "{}")
        except Exception:
            plan = {}
        return Project(
            id=row["id"],
            name=row["name"],
            root_path=row["root_path"],
            lifecycle=ProjectLifecycle(row["lifecycle"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            description=row["description"] or "",
            plan=plan if isinstance(plan, dict) else {},
        )

    # ------------------------------------------------------------------ CRUD

    def create_project(self, project: Project) -> Project:
        import json as _json

        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM projects WHERE id = ?", (project.id,)
            ).fetchone()
            if existing is not None:
                raise ValueError(f"project id {project.id!r} already exists")
            self._conn.execute(
                """
                INSERT INTO projects
                    (id, name, root_path, lifecycle, created_at, updated_at, description, plan_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project.id,
                    project.name,
                    project.root_path,
                    project.lifecycle.value,
                    project.created_at,
                    project.updated_at,
                    project.description or "",
                    _json.dumps(project.plan, ensure_ascii=False, sort_keys=True),
                ),
            )
        return project

    def get_project(self, project_id: str) -> Optional[Project]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return self._row_to_project(row) if row is not None else None

    def get_project_plan_json_raw(self, project_id: str) -> Optional[str]:
        """Return only one project's stored plan JSON without decoding it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT plan_json FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if row is None:
            return None
        value = row["plan_json"]
        return value if isinstance(value, str) else str(value)

    def find_by_name(self, name: str) -> Optional[Project]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE name = ? COLLATE NOCASE LIMIT 1",
                (name,),
            ).fetchone()
        return self._row_to_project(row) if row is not None else None

    def list_projects(
        self,
        lifecycle: Optional[ProjectLifecycle] = None,
        limit: Optional[int] = None,
    ) -> list[Project]:
        sql = "SELECT * FROM projects"
        params: list = []
        if lifecycle is not None:
            sql += " WHERE lifecycle = ?"
            params.append(lifecycle.value)
        # `rowid` is SQLite's monotonically-increasing internal id and
        # is the most reliable tiebreaker when two records share the
        # same millisecond-precision timestamp.
        sql += " ORDER BY updated_at DESC, created_at DESC, rowid DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
        return [self._row_to_project(r) for r in rows]

    def update_project(
        self,
        project_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        plan: Optional[dict] = None,
        root_path: Optional[str] = None,
    ) -> Optional[Project]:
        from giscode.projects.model import _utc_now_iso, normalize_project_name, normalize_project_path
        import json as _json

        current = self.get_project(project_id)
        if current is None:
            return None

        new_name = normalize_project_name(name) if name is not None else current.name
        new_root = normalize_project_path(root_path) if root_path is not None else current.root_path
        new_desc = description if description is not None else current.description
        new_plan = dict(plan) if plan is not None else current.plan
        new_updated = _utc_now_iso()

        with self._lock:
            self._conn.execute(
                """
                UPDATE projects
                SET name = ?, root_path = ?, description = ?, plan_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_name,
                    new_root,
                    new_desc,
                    _json.dumps(new_plan, ensure_ascii=False, sort_keys=True),
                    new_updated,
                    project_id,
                ),
            )
        return self.get_project(project_id)

    def rename_project(self, project_id: str, new_name: str) -> Optional[Project]:
        return self.update_project(project_id, name=new_name)

    def set_plan(self, project_id: str, plan: dict) -> Optional[Project]:
        return self.update_project(project_id, plan=plan)

    def set_description(self, project_id: str, description: str) -> Optional[Project]:
        return self.update_project(project_id, description=description)

    def delete_project(self, project_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------ lifecycle

    def transition_lifecycle(
        self, project_id: str, target: ProjectLifecycle
    ) -> Optional[Project]:
        """Atomically move a project to ``target`` if and only if the
        transition is allowed. The lifecycle field is never changed
        silently — APPROVED is never auto-entered from PLANNING or
        REVIEW, and BUILDING is never auto-entered from APPROVED by
        this method.
        """
        from giscode.projects.model import _utc_now_iso
        import json as _json

        if not isinstance(target, ProjectLifecycle):
            target = ProjectLifecycle(target)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                return None
            current = self._row_to_project(row)
            assert_transition(current.lifecycle, target)
            self._conn.execute(
                """
                UPDATE projects
                SET lifecycle = ?, updated_at = ?
                WHERE id = ?
                """,
                (target.value, _utc_now_iso(), project_id),
            )
        return self.get_project(project_id)

    def approve(self, project_id: str) -> Optional[Project]:
        """Explicitly move a project to APPROVED. The caller (the
        future Project Workspace GUI's "APPROVE & START" button) is
        the *only* legitimate path to this state."""
        return self.transition_lifecycle(project_id, ProjectLifecycle.APPROVED)

    def start_building(self, project_id: str) -> Optional[Project]:
        """Move from APPROVED to BUILDING. Building only starts when
        this is explicitly called — never implicitly."""
        return self.transition_lifecycle(project_id, ProjectLifecycle.BUILDING)

    def fail(self, project_id: str) -> Optional[Project]:
        return self.transition_lifecycle(project_id, ProjectLifecycle.FAILED)

    def retry(self, project_id: str) -> Optional[Project]:
        """Explicit retry: reopen an eligible project for approval.

        Only ``FAILED`` (a build or its verification did not pass) and
        ``APPROVED`` (a build that never actually started) may be retried, and
        the only target is ``REVIEW``: the project has to cross the approval
        boundary again, exactly as the first attempt did. Every other state is
        refused by :func:`assert_transition`, so ``BUILDING`` (work may be
        running), ``COMPLETE`` and ``CANCELLED`` can never be reopened here.

        The plan, the name, the root and the project's files are untouched.
        """
        return self.transition_lifecycle(project_id, ProjectLifecycle.REVIEW)

    def cancel(self, project_id: str) -> Optional[Project]:
        return self.transition_lifecycle(project_id, ProjectLifecycle.CANCELLED)

    def can_transition(
        self, project_id: str, target: ProjectLifecycle
    ) -> bool:
        proj = self.get_project(project_id)
        if proj is None:
            return False
        return can_transition(proj.lifecycle, target)

    # ------------------------------------------------------------------ utilities

    def count(self, lifecycle: Optional[ProjectLifecycle] = None) -> int:
        with self._lock:
            if lifecycle is None:
                cur = self._conn.execute("SELECT COUNT(*) AS c FROM projects")
            else:
                cur = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM projects WHERE lifecycle = ?",
                    (lifecycle.value,),
                )
        return int(cur.fetchone()["c"])

    def exists(self, project_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM projects WHERE id = ?", (project_id,)
            )
            return cur.fetchone() is not None
