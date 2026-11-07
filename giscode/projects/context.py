"""Project session/context model for GisCode.

A :class:`ProjectContext` captures everything the UI and the
agent chain need to keep a project's session explicit and
resumable: the active project identity, the absolute path, the
active section/work area, the current/final task state, recent
section history and the last activity timestamp.

The model is deliberately dependency-free (pure dataclasses) so
both the GUI (:class:`giscode.gui.MainWindow`) and the agent
planner can import it without coupling to Tk or the agent loop.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

from giscode.config.paths import giscode_home

def default_session_dir() -> Path:
    """Directory holding persisted session contexts (profile aware)."""
    return giscode_home() / "session_contexts"


@dataclass
class ProjectContext:
    """Explicit, persistent context for a single active project.

    Attributes
    ----------
    project_id:
        Stable identifier (defaults to a slug of the absolute
        path).
    name:
        Display name (the project folder's basename).
    root_path:
        Absolute path to the project directory.
    active_section:
        Current work area/section (e.g. "Overview",
        "Backend", "Continue previous work").
    section_history:
        Most-recent-first section breadcrumbs.
    current_task_id:
        The last task id attached to this project.
    task_status:
        The last known task status string.
    last_activity:
        ISO timestamp of the last recorded activity.
    custom_logo:
        Optional user-supplied logo HTML for the app/project the user is
        building. Empty means the official Gis branding is used.
    project_kind:
        Whether the active root was explicitly opened as an existing project
        or created through GisCode's New Project workflow.
    """

    project_id: str
    name: str = ""
    root_path: str = ""
    active_section: str = ""
    section_history: list = field(default_factory=list)
    current_task_id: str = ""
    task_status: str = ""
    last_activity: str = ""
    custom_logo: str = ""
    project_kind: str = "existing"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectContext":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    @classmethod
    def create(
        cls,
        root_path: str,
        *,
        section: str = "",
        name: str = "",
        project_kind: str = "existing",
    ) -> "ProjectContext":
        root_path = os.path.abspath(root_path)
        name = name or os.path.basename(root_path.rstrip(os.sep)) or root_path
        slug = name.lower().replace(" ", "-") or "project"
        return cls(
            project_id=f"{slug}-{abs(hash(root_path)) % 1000000}",
            name=name,
            root_path=root_path,
            active_section=section or "Overview",
            project_kind=project_kind,
        )

    def record_activity(self, note: str = "") -> None:
        self.last_activity = time.strftime("%Y-%m-%dT%H:%M:%S")

    def enter_section(self, section: str) -> None:
        """Move to a new work area, keeping breadcrumbs."""
        section = section.strip()
        if not section:
            return
        prev = self.active_section
        if prev and prev != section:
            self.section_history.insert(0, prev)
            self.section_history = self.section_history[:10]
        self.active_section = section
        self.record_activity()

    def back_to_previous_section(self) -> str:
        """Pop the most recent section; returns the new active
        section (or the unchanged one if there is no history)."""
        if self.section_history:
            self.active_section = self.section_history.pop(0)
            self.record_activity()
        return self.active_section


class ProjectContextStore:
    """Loads and persists project contexts as JSON files."""

    def __init__(self, directory: Optional[Path] = None):
        self._dir = Path(directory) if directory is not None else default_session_dir()

    def _path(self, project_id: str) -> Path:
        return self._dir / f"{project_id}.json"

    def load_all(self) -> dict:
        out: dict = {}
        try:
            if not self._dir.is_dir():
                return out
            for f in self._dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    continue
                ctx = ProjectContext.from_dict(data)
                if ctx.project_id:
                    out[ctx.project_id] = ctx
        except OSError:
            pass
        return out

    def save(self, ctx: ProjectContext) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path(ctx.project_id).write_text(
                json.dumps(ctx.to_dict(), indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def load_active_project_context(self) -> Optional["ProjectContext"]:
        """Return GisCode's active project context at startup, if any.

        This store persists *session histories* (for resumability) and does NOT
        persist an "active project" selection. A recorded session is not an
        active selection and must NEVER be auto-activated: doing so would
        inject whichever project happened to be persisted first - possibly
        another application's project or a stale session - into a fresh
        GisCode engine and its model prompts.

        GisCode's active project identity is established only explicitly, via
        the GUI when the user opens a GisCode project. Whatever ends up in this
        store is therefore never a source of truth for the active project.
        """
        return None

    def _load_one(self, project_id: str) -> Optional[dict]:
        """Raw JSON payload for one project (or None if absent/corrupt)."""
        try:
            raw = self._path(project_id).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def get_custom_logo(self, project_id: str) -> Optional[str]:
        """Per-project custom logo HTML, or None when unset."""
        data = self._load_one(project_id)
        if not data:
            return None
        logo = data.get("custom_logo")
        return str(logo).strip() if isinstance(logo, str) and logo.strip() else None

    def save_custom_logo(self, project_id: str, html: str) -> None:
        """Persist a project's custom logo HTML in its own context record.

        Keyed by the stable ``project_id``, so branding never leaks from one
        project into another and a brand-new project stays on the default
        Gis branding until it gets its own configured logo.
        """
        data = self._load_one(project_id)
        if data is None:
            ctx = ProjectContext(project_id=project_id)
            data = ctx.to_dict()
        data["custom_logo"] = html
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path(project_id).write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
