"""Project model and persistent store for GisCode.

The project model is intentionally decoupled from the existing
:class:`giscode.workspace.Workspace` filesystem wrapper and the
existing chat pipeline. It introduces a stable project identifier
and an explicit project lifecycle that the future Planer / Project
Workspace GUI will drive.

Existing coding/build/test/verification behavior is preserved
unchanged — projects are a *new* layer on top, not a replacement
for the existing :mod:`giscode.agentic` loop.

Public surface:
    * :class:`ProjectLifecycle`        — explicit lifecycle enum
    * :class:`Project`                 — project record (dataclass)
    * :class:`ProjectStore`            — SQLite-backed CRUD + transitions
"""

from giscode.projects.lifecycle import ProjectLifecycle, LifecycleError, ALLOWED_TRANSITIONS
from giscode.projects.model import Project
from giscode.projects.store import ProjectStore
from giscode.projects.visual_design import VisualConnection, VisualDesignDocument, VisualScreen

__all__ = [
    "ALLOWED_TRANSITIONS",
    "LifecycleError",
    "Project",
    "ProjectLifecycle",
    "ProjectStore",
    "VisualConnection",
    "VisualDesignDocument",
    "VisualScreen",
]

# ProjectWorkspaceSession is intentionally NOT re-exported here to
# avoid a circular import: giscode.planer.chat imports from
# giscode.projects.model. Import the session directly from
# giscode.projects.workspace when needed.
