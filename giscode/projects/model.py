"""Project data model.

The :class:`Project` dataclass is the in-memory representation of a
project. Serialization round-trips through :meth:`to_dict` and
:meth:`from_dict` and is consumed by :class:`ProjectStore`.

Stable identity: every project has a UUID4 ``id`` that never changes
even if the project is renamed. Display name is a separate, mutable
field. The :attr:`root_path` is normalized to an absolute path on
Windows.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from giscode.projects.lifecycle import ProjectLifecycle, TERMINAL_STATES


_NAME_RE = re.compile(r"[^A-Za-z0-9 _.\-()]+")
_MAX_NAME_LEN = 100

# Valid development modes
VALID_MODES = frozenset({"traditional", "visual_app_design"})
DEFAULT_MODE = "traditional"


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time()%1)*1000):03d}Z"


def normalize_project_name(name: str) -> str:
    """Sanitize a display name: trim, collapse whitespace, strip
    characters that are awkward on Windows filesystems, fall back to
    ``"Untitled Project"`` for an empty/invalid result."""
    if name is None:
        return "Untitled Project"
    cleaned = _NAME_RE.sub("", re.sub(r"\s", " ", str(name))).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return "Untitled Project"
    return cleaned[:_MAX_NAME_LEN]


def normalize_project_path(path: str) -> str:
    """Resolve to an absolute, normalized Windows-safe path."""
    if not path:
        raise ValueError("path must be a non-empty string")
    p = Path(os.path.abspath(os.path.expanduser(str(path))))
    return os.path.normpath(str(p))


@dataclass
class Project:
    id: str
    name: str
    root_path: str
    lifecycle: ProjectLifecycle
    created_at: str
    updated_at: str
    description: str = ""
    plan: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ dunder

    def __post_init__(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise ValueError("id must be a non-empty string")
        if not self.name or not isinstance(self.name, str):
            raise ValueError("name must be a non-empty string")
        if not self.root_path or not isinstance(self.root_path, str):
            raise ValueError("root_path must be a non-empty string")
        if not isinstance(self.lifecycle, ProjectLifecycle):
            self.lifecycle = ProjectLifecycle(self.lifecycle)
        if not isinstance(self.plan, dict):
            self.plan = dict(self.plan or {})

    # ------------------------------------------------------------------ helpers

    @property
    def mode(self) -> str:
        """Return the development mode. Defaults to 'traditional' if not set."""
        return self.plan.get("mode", DEFAULT_MODE)

    @property
    def is_terminal(self) -> bool:
        return self.lifecycle in TERMINAL_STATES

    @property
    def chat_scope(self) -> str:
        """The scope string used by :class:`giscode.storage.ChatStore`.

        The format ``project:<id>`` is the contract documented in
        :mod:`giscode.storage.chat_store`. Using the stable project
        id (not the display name) guarantees that renaming a project
        does not orphan its chats.
        """
        return f"project:{self.id}"

    # ------------------------------------------------------------------ serde

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "root_path": self.root_path,
            "lifecycle": self.lifecycle.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "description": self.description,
            "plan": dict(self.plan),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        if not isinstance(data, dict):
            raise TypeError("project data must be a dict")
        d = dict(data)
        d.setdefault("description", "")
        d.setdefault("plan", {})
        return cls(
            id=str(d["id"]),
            name=str(d["name"]),
            root_path=str(d["root_path"]),
            lifecycle=ProjectLifecycle(d["lifecycle"]),
            created_at=str(d["created_at"]),
            updated_at=str(d["updated_at"]),
            description=str(d.get("description", "")),
            plan=dict(d.get("plan", {}) or {}),
        )

    # ------------------------------------------------------------------ factory

    @classmethod
    def create(
        cls,
        name: str,
        root_path: str,
        description: str = "",
        plan: Optional[dict] = None,
        lifecycle: ProjectLifecycle = ProjectLifecycle.PLANNING,
        project_id: Optional[str] = None,
        mode: str = DEFAULT_MODE,
    ) -> "Project":
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid mode: {mode!r}. Valid modes: {sorted(VALID_MODES)}")
        pid = project_id or f"proj-{uuid.uuid4().hex}"
        now = _utc_now_iso()
        plan_dict = dict(plan or {})
        plan_dict.setdefault("mode", mode)
        plan_dict.setdefault("visual_design", {})
        return cls(
            id=pid,
            name=normalize_project_name(name),
            root_path=normalize_project_path(root_path),
            lifecycle=lifecycle,
            created_at=now,
            updated_at=now,
            description=str(description or ""),
            plan=plan_dict,
        )
