"""Project-root resolution and per-run rooting of the coding loop.

The coding loop edits whatever workspace it was constructed with. In the
application that workspace is the *configured projects folder*, not the
folder of the project the user opened, so a build started from
``Approve & Start`` used to write into the wrong tree. This module holds
the small, shared pieces that make the root explicit:

* :func:`resolve_project_root` -- turn a user/record supplied path into a
  real directory, refusing it unless it lives inside the configured projects
  folder *or* is one of the folders the user explicitly selected. Both sides
  are compared the way Windows compares paths: resolved (short 8.3 names
  expanded, junctions and symlinks followed), case-folded, with trailing
  separators removed.
* :func:`external_root_notice` -- the one wording used by the window and the
  tests to tell the user, before anything runs, which exact directory an
  external project will modify.
* :func:`start_coding_task` -- call a coding agent's ``start`` with the
  root for this run, but only when that agent accepts one, so test doubles
  and the pre-existing single-argument contract keep working.

Nothing here edits files; the only side effects are filesystem queries.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any, Iterable, Optional

from giscode.config.paths import giscode_home


class RootNotAllowedError(Exception):
    """The task's project root is missing, not a directory, or out of bounds."""


def canonical_path(path: "str | os.PathLike[str]") -> Path:
    """The real, case-folded form of *path* used for comparisons.

    ``os.path.realpath`` expands 8.3 short names and follows junctions and
    symlinks; ``os.path.normcase`` folds case on Windows (a no-op elsewhere);
    ``normpath`` drops trailing separators and ``.`` segments.
    """
    text = os.path.expanduser(str(path).strip())
    if not text:
        raise RootNotAllowedError("an empty path cannot be used as a project root")
    real = os.path.realpath(os.path.abspath(text))
    return Path(os.path.normcase(os.path.normpath(real)))


def resolved_path(path: "str | os.PathLike[str]") -> Path:
    """The real form of *path* with its original casing, for actual use."""
    text = os.path.expanduser(str(path).strip())
    if not text:
        raise RootNotAllowedError("an empty path cannot be used as a project root")
    return Path(os.path.realpath(os.path.abspath(text)))


def is_within(allowed_root: "str | os.PathLike[str]", candidate: "str | os.PathLike[str]") -> bool:
    """Whether *candidate* is *allowed_root* itself or lives beneath it."""
    allowed = canonical_path(allowed_root)
    target = canonical_path(candidate)
    if allowed == target:
        return True
    try:
        target.relative_to(allowed)
        return True
    except ValueError:
        return False


def is_external_project_root(
    path: "str | os.PathLike[str]",
    projects_dir: Optional["str | os.PathLike[str]"],
) -> bool:
    """Whether *path* is a usable folder outside *projects_dir*.

    ``False`` when no projects folder is configured (nothing to be external
    to) or when the path cannot be resolved, so callers never treat a broken
    path as an external project that merely needs confirming.
    """
    if projects_dir is None:
        return False
    try:
        candidate = canonical_path(path)
    except RootNotAllowedError:
        return False
    return not is_within(projects_dir, candidate)


def external_root_notice(
    path: "str | os.PathLike[str]",
    projects_dir: Optional["str | os.PathLike[str]"],
) -> str:
    """The exact explanation shown before an external project is opened.

    It names the folder GisCode will modify and the folder it will leave
    alone, so the user is not asked to approve an abstract rule. Kept here,
    not in the window, so a test can pin the wording the user actually sees.
    """
    root = str(resolved_path(path))
    managed = str(resolved_path(projects_dir)) if projects_dir else "its projects folder"
    return (
        f"'{root}' is outside GisCode's projects folder ({managed}).\n\n"
        f"GisCode will create, edit and delete files only inside:\n{root}\n\n"
        f"That folder is the project's own root, so the build runs there and "
        f"nothing in {managed} is modified. Continue with this project?"
    )


def resolve_project_root(
    path: Optional[str],
    *,
    allowed_root: Optional[str] = None,
    allowed_roots: "Iterable[str | os.PathLike[str]]" = (),
) -> Path:
    """Return the usable root for *path*, or raise :class:`RootNotAllowedError`.

    With ``allowed_root`` and/or ``allowed_roots`` set, a root outside every
    one of them is refused. ``allowed_root`` is the configured projects
    folder; ``allowed_roots`` are folders the user explicitly selected in
    this session (an external project). The rule is still fail-closed: a path
    the user never chose can never become a build root.
    """
    if not isinstance(path, str) or not path.strip():
        raise RootNotAllowedError(
            "no project root is known for this task, so nothing was started"
        )
    candidate = resolved_path(path)
    if not candidate.exists():
        raise RootNotAllowedError(
            f"project root does not exist: {path}"
        )
    if not candidate.is_dir():
        raise RootNotAllowedError(
            f"project root is not a directory: {path}"
        )

    approved: list[str] = []
    if allowed_root is not None:
        approved.append(str(canonical_path(allowed_root)))
    for extra in allowed_roots or ():
        try:
            approved.append(str(canonical_path(extra)))
        except RootNotAllowedError:
            continue
    if approved:
        target = str(canonical_path(candidate))
        if not any(is_within(root, target) for root in approved):
            managed = (
                f"the configured projects folder {resolved_path(allowed_root)}"
                if allowed_root is not None
                else "the folders selected in GisCode"
            )
            raise RootNotAllowedError(
                f"project root {candidate} is outside {managed} and was not "
                f"explicitly selected in GisCode; the build was refused before "
                f"any file was touched"
            )
    return candidate


def snapshot_profile_root(project_id: Optional[str]) -> Path:
    """Folder under the app profile that holds a project's task snapshots.

    :class:`giscode.agentic.write_safety.SnapshotManager` appends
    ``snapshots/<task_id>`` itself. Keeping the base under ``GISCODE_HOME``
    means a snapshot never lands inside the project tree, and never beside
    whatever folder happens to contain it.
    """
    key = (project_id or "").strip() or "tasks"
    safe = "".join(
        ch if ch.isalnum() or ch in "-_. " else "_" for ch in key
    ).strip() or "tasks"
    return giscode_home() / "projects" / safe


def supports_root_path(start: Any) -> bool:
    """Whether *start* accepts a ``root_path`` keyword.

    Test doubles and older agents expose ``start(request)`` only. Inspecting
    the signature keeps that call working instead of raising ``TypeError``.
    """
    if not callable(start):
        return False
    try:
        parameters = inspect.signature(start).parameters
    except (TypeError, ValueError):
        return False
    if "root_path" in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def start_coding_task(agent: Any, request: Any, root_path: Optional[Path]) -> Any:
    """Run ``agent.start`` for *request*, rooted at *root_path* when supported."""
    start = getattr(agent, "start", None)
    if not callable(start):
        raise RootNotAllowedError("the coding agent cannot start a task")
    if root_path is not None and supports_root_path(start):
        return start(request, root_path=str(root_path))
    return start(request)
