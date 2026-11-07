"""Where a rollback keeps the content it is about to discard.

Restoring a file to its pre-task content removes the version the task wrote.
When a suite failed, that version is not automatically worthless: one edit of
several can be the correct one. This module writes the discarded bytes (and a
readable unified diff) *before* ``safe_rollback`` removes them, so the content
survives the undo.

Nothing here puts a file back. ``giscode.gisai.recover_edits`` does that, and
only after re-checking digests, containment and approval.

Layout, under the profile's ``recovery`` directory::

    <task>/manifest.json          entries keyed by relative path
    <task>/files/<relative>       exact bytes the task produced
    <task>/diffs/<relative>.diff  readable diff against the pre-edit content

The manifest is rewritten atomically and merges, so a second rollback of the
same task (a retry) adds to the account instead of replacing it.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "FORMAT",
    "DiscardedEdit",
    "Manifest",
    "digest_bytes",
    "directory_for_snapshot",
    "task_for_snapshot",
    "record_discarded",
    "load",
]

FORMAT = 1
MANIFEST_NAME = "manifest.json"
FILES_DIR = "files"
DIFFS_DIR = "diffs"


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def task_for_snapshot(snapshot: object) -> str:
    """The task id a snapshot belongs to, or ``""`` when it has none."""
    try:
        return Path(snapshot.root).name
    except (AttributeError, TypeError):
        return ""


def directory_for_snapshot(snapshot: object, override: Path | None = None) -> Path | None:
    """The recovery directory for *snapshot*, beside its ``snapshots`` tree.

    ``SnapshotManager.root`` is ``<profile>/snapshots/<task>``, so discarded
    edits belong in ``<profile>/recovery/<task>``: same profile, outside the
    project, and therefore outside anything a task is allowed to write.
    A snapshot whose root is not laid out that way has no derivable location,
    and reporting that is better than inventing one inside the project.
    """
    if override is not None:
        return Path(override)
    try:
        root = Path(snapshot.root)
    except (AttributeError, TypeError):
        return None
    if root.parent.name != "snapshots":
        return None
    return root.parent.parent / "recovery" / root.name


def _safe_relative(relative: str) -> str | None:
    """A project-relative path that cannot escape the store directory."""
    text = str(relative or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if not text or text.startswith("/"):
        return None
    parts = text.split("/")
    if any(part in {"", "..", "."} for part in parts):
        return None
    if ":" in parts[0]:  # a Windows drive or stream name is not a project path
        return None
    return text


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")
    try:
        staged.write_bytes(data)
        os.replace(staged, path)
    finally:
        try:
            staged.unlink()
        except OSError:
            pass


def _diff_text(relative: str, before: bytes | None, after: bytes) -> str:
    old = (before or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
    new = after.decode("utf-8", errors="replace").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old,
            new,
            fromfile=f"a/{relative}" if before is not None else "/dev/null",
            tofile=f"b/{relative}",
        )
    )


@dataclass(frozen=True)
class DiscardedEdit:
    """One edit a rollback discarded, with the bytes it was made of."""

    relative: str
    produced: str
    baseline: str | None
    created: bool
    blob: Path
    diff: Path

    def read_bytes(self) -> bytes:
        """The saved content, or ``OSError`` when it is gone or was tampered with."""
        data = self.blob.read_bytes()
        if digest_bytes(data) != self.produced:
            raise OSError("the saved copy does not match its recorded digest")
        return data

    def diff_text(self) -> str:
        try:
            return self.diff.read_text(encoding="utf-8")
        except OSError:
            return ""


@dataclass
class Manifest:
    """Every discarded edit one task saved, newest entry per path."""

    directory: Path
    task: str = ""
    project_root: str = ""
    entries: list[DiscardedEdit] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "directory": str(self.directory),
            "task": self.task,
            "project_root": self.project_root,
            "entries": [
                {
                    "path": entry.relative,
                    "created": entry.created,
                    "produced": entry.produced,
                    "baseline": entry.baseline,
                }
                for entry in self.entries
            ],
            "errors": list(self.errors),
        }


def _existing_entries(directory: Path) -> dict[str, dict]:
    path = directory / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        return {}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return {}
    kept: dict[str, dict] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = _safe_relative(item.get("relative", ""))
        produced = item.get("produced")
        if not name or not isinstance(produced, str):
            continue
        kept[name.lower()] = {
            "relative": name,
            "produced": produced,
            "baseline": item.get("baseline") if isinstance(item.get("baseline"), str) else None,
            "created": bool(item.get("created")),
            "saved_at": item.get("saved_at", ""),
        }
    return kept


def record_discarded(
    directory: Path,
    *,
    task: str,
    project_root: str,
    relative: str,
    produced_bytes: bytes,
    baseline_bytes: bytes | None = None,
    created: bool = False,
) -> tuple[DiscardedEdit | None, str]:
    """Save the content a rollback is about to drop. Call before restoring.

    Returns the entry and ``""``, or ``None`` and the reason it could not be
    saved. A failure here never stops the rollback: refusing to undo a broken
    edit because a recovery copy could not be written would leave the worse of
    the two states on disk.
    """
    directory = Path(directory)
    name = _safe_relative(relative)
    if name is None:
        return None, f"{relative}: its path cannot be stored safely"
    produced = digest_bytes(produced_bytes)
    baseline = digest_bytes(baseline_bytes) if baseline_bytes is not None else None
    blob = directory / FILES_DIR / name
    diff = directory / DIFFS_DIR / f"{name}.diff"
    try:
        _atomic_write(blob, produced_bytes)
        _atomic_write(diff, _diff_text(name, baseline_bytes, produced_bytes).encode("utf-8"))
    except OSError as exc:
        return None, f"{name}: the recovery copy could not be written ({type(exc).__name__})"

    entries = _existing_entries(directory)
    entries[name.lower()] = {
        "relative": name,
        "produced": produced,
        "baseline": baseline,
        "created": bool(created),
        "saved_at": _now(),
    }
    payload = {
        "format": FORMAT,
        "task": str(task or ""),
        "project_root": str(project_root or ""),
        "updated_at": _now(),
        "entries": [entries[key] for key in sorted(entries)],
    }
    try:
        _atomic_write(
            directory / MANIFEST_NAME,
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        )
    except OSError as exc:
        return None, f"{name}: saved, but the recovery manifest could not be written ({type(exc).__name__})"
    return (
        DiscardedEdit(
            relative=name,
            produced=produced,
            baseline=baseline,
            created=bool(created),
            blob=blob,
            diff=diff,
        ),
        "",
    )


def load(directory: Path) -> Manifest:
    """Read a recovery directory. Never raises: a broken entry is reported."""
    directory = Path(directory)
    manifest = Manifest(directory=directory)
    path = directory / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return manifest
    except (OSError, ValueError) as exc:
        manifest.errors.append(f"the recovery manifest could not be read ({type(exc).__name__})")
        return manifest
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        manifest.errors.append("the recovery manifest is not in a known format")
        return manifest
    manifest.task = str(payload.get("task") or "")
    manifest.project_root = str(payload.get("project_root") or "")
    for item in payload.get("entries") or []:
        if not isinstance(item, dict):
            continue
        name = _safe_relative(item.get("relative", ""))
        produced = item.get("produced")
        if not name or not isinstance(produced, str) or len(produced) != 64:
            manifest.errors.append("an entry was skipped: its path or digest is not usable")
            continue
        manifest.entries.append(
            DiscardedEdit(
                relative=name,
                produced=produced,
                baseline=item.get("baseline") if isinstance(item.get("baseline"), str) else None,
                created=bool(item.get("created")),
                blob=directory / FILES_DIR / name,
                diff=directory / DIFFS_DIR / f"{name}.diff",
            )
        )
    return manifest
