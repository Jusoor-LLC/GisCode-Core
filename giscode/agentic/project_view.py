"""Bounded, read-only views of an open project for Core's capabilities.

Core must be able to read, list and search a project without a model, and must
do it without becoming a second traversal framework: this module is a thin,
bounded layer over the filesystem that the Core dispatcher calls, reusing the
same denial rules the write path uses (``denied_path_reason``) and the same
excluded-directory names ``repo_inventory`` prunes for planning.

Every function here is read-only, deterministic, and capped: a huge project
produces a truncated but well-formed answer, never an unbounded dump and never a
silent partial view that looks complete.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from giscode.agentic.write_safety import denied_path_reason

#: Directories never descended into. The write path's denial set, plus the
#: build/editor directories the planner already ignores.
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".giscode",
        ".gradle",
        ".idea",
        ".kotlin",
        ".venv",
        ".vs",
        "node_modules",
        "__pycache__",
        "pycache",
        "venv",
        "env",
        ".env",
        "build",
        "dist",
        "out",
        "target",
    }
)

#: A single file larger than this is reported, not read.
DEFAULT_MAX_BYTES = 256 * 1024

#: Default caps for listing and searching.
DEFAULT_LIST_LIMIT = 200
DEFAULT_MAX_MATCHES = 50
DEFAULT_MAX_FILES = 400


class ViewError(Exception):
    """A path the read-only layer refuses to touch."""


def resolve(root: str | Path, relative: str) -> Path:
    """Resolve *relative* under *root* using the write path's denial rules.

    Raises :class:`ViewError` for an escape, a protected path component or an
    absolute path outside the workspace, so a read can never leave the project.
    """
    base = Path(root).resolve()
    raw = str(relative or "").strip()
    if not raw:
        return base
    candidate = Path(raw)
    target = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    denied = denied_path_reason(target, base)
    if denied:
        raise ViewError(denied)
    try:
        target.relative_to(base)
    except ValueError:
        raise ViewError(f"Path escapes workspace: {relative}")
    return target


def is_excluded(path: Path, root: Path) -> bool:
    """True when *path* sits under an excluded directory (relative to *root*)."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in EXCLUDED_DIR_NAMES for part in parts)


def read_text_file(
    root: str | Path,
    relative: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict:
    """Read one text file and describe it.

    Returns ``{"status", "path", "text", "bytes", "lines", "sha256",
    "truncated", "error"}``. ``status`` is ``"ok"``, ``"missing"``,
    ``"directory"``, ``"binary"`` or ``"too_large"``; the caller maps those to
    its own request status, so this module never decides product behaviour.
    """
    try:
        target = resolve(root, relative)
    except ViewError as exc:
        return _read_result("denied", relative, error=str(exc))
    if not target.exists():
        return _read_result("missing", relative, error=f"File does not exist: {relative}")
    if target.is_dir():
        return _read_result("directory", relative, error=f"Path is a directory: {relative}")
    try:
        data = target.read_bytes()
    except OSError as exc:
        return _read_result("unreadable", relative, error=str(exc))
    digest = hashlib.sha256(data).hexdigest()
    if len(data) > max_bytes:
        return _read_result(
            "too_large",
            relative,
            bytes_count=len(data),
            sha=digest,
            error=f"File is larger than {max_bytes} bytes; refusing to dump it",
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return _read_result(
            "binary",
            relative,
            bytes_count=len(data),
            sha=digest,
            error="File is not UTF-8 text; Core does not render binary files",
        )
    return _read_result(
        "ok",
        relative,
        bytes_count=len(data),
        sha=digest,
        text=text,
        lines=len(text.splitlines()),
    )


def _read_result(
    status: str,
    relative: str,
    *,
    text: str = "",
    bytes_count: int = 0,
    lines: int = 0,
    sha: str = "",
    error: str = "",
) -> dict:
    return {
        "status": status,
        "path": str(relative),
        "text": text,
        "bytes": bytes_count,
        "lines": lines,
        "sha256": sha,
        "truncated": False,
        "error": error,
    }


def list_files(
    root: str | Path,
    relative: str = "",
    *,
    limit: int = DEFAULT_LIST_LIMIT,
) -> dict:
    """List one directory's entries, sorted, with files before folders.

    Returns ``{"status", "path", "entries", "truncated", "error"}`` where each
    entry is ``{"path", "kind", "bytes"}``. Non-recursive on purpose: a listing
    that silently walks a tree is a dump, not an answer.
    """
    try:
        target = resolve(root, relative)
    except ViewError as exc:
        return _list_result("denied", relative, error=str(exc))
    if not target.exists():
        return _list_result("missing", relative, error=f"Folder does not exist: {relative}")
    if not target.is_dir():
        return _list_result("not_a_directory", relative, error=f"Path is a file: {relative}")
    base = Path(root).resolve()
    try:
        children = sorted(target.iterdir(), key=lambda item: (item.is_dir(), item.name.lower()))
    except OSError as exc:
        return _list_result("unreadable", relative, error=str(exc))
    entries: list[dict] = []
    for child in children:
        if child.name in EXCLUDED_DIR_NAMES:
            continue
        if is_excluded(child, base):
            continue
        kind = "dir" if child.is_dir() else "file"
        size = 0
        if kind == "file":
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
        entries.append(
            {
                "path": child.relative_to(base).as_posix(),
                "kind": kind,
                "bytes": size,
            }
        )
    truncated = len(entries) > max(0, limit)
    return _list_result(
        "ok", relative, entries=entries[: max(0, limit)], truncated=truncated
    )


def _list_result(
    status: str, relative: str, *, entries: list | None = None, truncated: bool = False, error: str = ""
) -> dict:
    return {
        "status": status,
        "path": str(relative),
        "entries": entries or [],
        "truncated": truncated,
        "error": error,
    }


def search_text(
    root: str | Path,
    term: str,
    relative: str = "",
    *,
    max_matches: int = DEFAULT_MAX_MATCHES,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict:
    """Find a literal *term* in the project's UTF-8 text files.

    Literal, case-sensitive and bounded: at most ``max_matches`` hits are
    returned and at most ``max_files`` files are read, so the cost is fixed by
    the caps rather than by the size of the project. Returns ``{"status",
    "term", "path", "matches", "files_scanned", "truncated", "error"}``.
    """
    needle = str(term or "")
    if not needle:
        return _search_result("empty_term", relative, term=needle, error="No search term was supplied")
    try:
        base = resolve(root, relative)
    except ViewError as exc:
        return _search_result("denied", relative, term=needle, error=str(exc))
    if not base.exists():
        return _search_result("missing", relative, term=needle, error=f"Path does not exist: {relative}")
    if not base.is_dir():
        return _search_result("not_a_directory", relative, term=needle, error=f"Path is a file: {relative}")

    project_root = Path(root).resolve()
    matches: list[dict] = []
    files_scanned = 0
    truncated = False
    for directory, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(name for name in dirnames if name not in EXCLUDED_DIR_NAMES)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if is_excluded(path, project_root):
                continue
            if files_scanned >= max_files:
                truncated = True
                break
            try:
                if path.stat().st_size > max_bytes:
                    continue
                raw = path.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            files_scanned += 1
            for number, line in enumerate(text.splitlines(), start=1):
                if needle in line:
                    matches.append(
                        {
                            "path": path.relative_to(project_root).as_posix(),
                            "line": number,
                            "text": line.strip()[:200],
                        }
                    )
                    if len(matches) >= max_matches:
                        truncated = True
                        break
            if len(matches) >= max_matches:
                break
        if len(matches) >= max_matches or files_scanned >= max_files:
            break
    return _search_result(
        "ok",
        relative,
        term=needle,
        matches=matches,
        files_scanned=files_scanned,
        truncated=truncated,
    )


def _search_result(
    status: str,
    relative: str,
    *,
    term: str,
    matches: list | None = None,
    files_scanned: int = 0,
    truncated: bool = False,
    error: str = "",
) -> dict:
    return {
        "status": status,
        "term": term,
        "path": str(relative),
        "matches": matches or [],
        "files_scanned": files_scanned,
        "truncated": truncated,
        "error": error,
    }
