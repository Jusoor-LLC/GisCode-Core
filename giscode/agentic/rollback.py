"""Undo only the edits a coding task made, and only when they are still ours.

The coding-agent path can end with an edit applied and the requested tests
failing. Leaving that change in place keeps an unverified edit; restoring the
snapshot blindly can destroy work the user saved in the meantime. This module
does the narrow thing:

* it only considers files this run demonstrably wrote, so a retry can never
  restore a baseline captured by an earlier run;
* it remembers the exact bytes the task produced (its *fingerprint*) before the
  tests run, which is when a user could still save the same file;
* it refuses to touch a file whose content no longer matches that fingerprint,
  preferring a reported conflict over lost work;
* it removes a created file only when the action log and the snapshot agree
  that this task created it and it is unchanged since.

Nothing here infers a change from an action log alone: the snapshot's baselines
are the authority for original content.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from giscode.agentic.write_safety import EPHEMERAL_DIRECTORY_NAMES, invalidate_bytecode
from giscode.agentic import recovery_store

__all__ = [
    "TaskChange",
    "RollbackOutcome",
    "normalise_relative",
    "capture_changes",
    "changes_from_snapshot",
    "apply_rollback",
    "rollback_task_edits",
    "describe_conflict",
    "FileOutcome",
    "restore_file",
    "remove_file",
    "create_file",
    "RecoveryAction",
    "recover_interrupted_rollbacks",
    "REASON_CHANGED_AFTER",
]

# A recovery record is written before the file is moved aside; without it the
# bytes are never treated as something to put back automatically.
_RECOVERY_FORMAT = 1
_RECOVERY_OPERATIONS = ("restore", "remove")

# The reasons a file was left alone. They are user-facing sentences, so a
# report can quote one verbatim instead of turning every conflict into the same
# "it changed" claim - which is false for a missing version record or a failed
# restore.
REASON_CHANGED_AFTER = "it changed after this task wrote it"
REASON_NO_RECORD = "no record of the version this task produced"
REASON_NO_BASELINE = "no pre-edit copy of it was kept"
REASON_NOT_RESTORED = "it could not be restored"
REASON_NOT_REMOVED = "it could not be removed"
REASON_GONE = "it no longer exists"
REASON_OUTSIDE = "it is outside the project directory"


def normalise_relative(relative: str) -> str:
    """A project-relative posix path with no leading ``./`` noise."""
    text = str(relative or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip()


def _inside(root: Path, relative: str) -> Path | None:
    """Resolve *relative* under *root*, or ``None`` if it escapes it."""
    text = normalise_relative(relative)
    if not text or text.startswith("/") or text.startswith("../"):
        return None
    try:
        candidate = (root / text).resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    return candidate


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class FileOutcome:
    """What happened to one file. Never what was intended."""

    status: str  # "restored" | "removed" | "conflict"
    reason: str = ""


REASON_NO_PRE_COMMAND = "no pre-command copy of it was kept"


@dataclass(frozen=True)
class RecoveryAction:
    """What recovery did with one quarantine file, and why."""

    path: str
    quarantine: str
    status: str  # "recovered" | "preserved" | "skipped"
    operation: str = ""
    reason: str = ""

    def describe(self) -> str:
        if self.status == "recovered":
            what = (
                "the file the rollback was removing"
                if self.operation == "remove"
                else "the version the rollback had moved aside"
            )
            return f"recovered {self.path} ({what})"
        if self.status == "skipped":
            return f"{self.path}: {self.reason}"
        return f"left {self.quarantine} in place: {self.reason}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "quarantine": self.quarantine,
            "status": self.status,
            "operation": self.operation,
            "reason": self.reason,
        }


def _recovery_record_path(quarantine: Path) -> Path:
    return quarantine.with_name(quarantine.name + ".json")


def _write_recovery_record(
    quarantine: Path, original: Path, operation: str, digest: str
) -> None:
    """Record what this quarantine is before the file leaves its path.

    Durable and best-effort: a record that cannot be written costs automatic
    recovery (the file is then reported, never guessed at), but must not stop
    the file from being protected right now.
    """
    record = {
        "format": _RECOVERY_FORMAT,
        "original": str(original),
        "operation": operation,
        "digest": digest,
    }
    path = _recovery_record_path(quarantine)
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        _discard(path)


def _sidecar(target: Path, tag: str) -> Path:
    """A unique sibling name: same directory, so the rename cannot cross devices."""
    return target.with_name(
        f".{target.name}.giscode-{tag}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )


def _quarantine(
    target: Path, *, operation: str = "restore", digest: str = ""
) -> tuple[Path | None, str]:
    """Move *target* out of the namespace, taking exclusive ownership of it.

    This is the step that makes the check safe. Whatever is at the path is now
    only reachable through the returned name, so no later writer can change
    those bytes and no later check can be overtaken by a save.

    A recovery record is written first, so a crash before the file is put back
    leaves enough evidence to repair it without guessing at a bare filename.
    """
    sidecar = _sidecar(target, "quarantine")
    if digest:
        _write_recovery_record(sidecar, target, operation, digest)
    try:
        os.rename(target, sidecar)
    except OSError as exc:
        _discard(_recovery_record_path(sidecar))
        return None, f"it could not be moved out of the way ({type(exc).__name__})"
    return sidecar, ""


def _put_back(sidecar: Path, target: Path) -> str:
    """Return displaced bytes to *target*, or preserve them beside it.

    Returns ``""`` when the bytes went back to their path, otherwise the path
    where they were preserved. A file that appeared at the target meanwhile is
    never overwritten: the displaced content is kept under a recovery name so
    the report can point at it.
    """
    try:
        if not target.exists():
            os.rename(sidecar, target)
            _discard(_recovery_record_path(sidecar))
            return ""
    except OSError:
        pass
    recovery = _sidecar(target, "recovered")
    try:
        os.rename(sidecar, recovery)
        _discard(_recovery_record_path(sidecar))
        return str(recovery)
    except OSError:
        return ""


def _discard(sidecar: Path) -> None:
    try:
        sidecar.unlink()
    except OSError:
        pass


def _finish_quarantine(sidecar: Path) -> None:
    """Drop a quarantine copy and its record once it is no longer needed."""
    _discard(sidecar)
    _discard(_recovery_record_path(sidecar))


def _replace_bytes(target: Path, data: bytes) -> None:
    """Create *target* with *data*, never overwriting a file already there.

    ``os.link`` is the primitive that makes "create only if absent" atomic: the
    staged file and the target are the same inode and the link fails if the
    name is taken. A filesystem without hard links falls back to an exclusive
    create, which still refuses to overwrite but writes the bytes in place.
    """
    staged = _sidecar(target, "rollback")
    try:
        staged.write_bytes(data)
        try:
            os.link(staged, target)
        except FileExistsError:
            raise
        except (OSError, AttributeError, NotImplementedError):
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
            handle = os.open(str(target), flags, 0o666)
            try:
                os.write(handle, data)
            finally:
                os.close(handle)
    finally:
        _discard(staged)


def restore_file(target: Path, expected_digest: str, data: bytes) -> FileOutcome:
    """Put *data* back at *target* only if *target* is still the task's output.

    The digest is checked, then the file is renamed out of the namespace and
    the bytes we actually took are checked again - so a save landing between
    the two steps is detected, not overwritten. The baseline is then created
    with an atomic create-if-absent, so a save landing after the rename is kept
    as well.
    """
    try:
        current = _digest(target)
    except OSError as exc:
        return FileOutcome("conflict", f"it cannot be read ({type(exc).__name__})")
    if current != expected_digest:
        return FileOutcome("conflict", REASON_CHANGED_AFTER)

    sidecar, problem = _quarantine(target, operation="restore", digest=expected_digest)
    if sidecar is None:
        return FileOutcome("conflict", problem)
    try:
        taken = _digest(sidecar)
    except OSError:
        taken = ""
    if taken != expected_digest:
        kept = _put_back(sidecar, target)
        detail = REASON_CHANGED_AFTER
        if kept:
            detail += f"; the newer content was kept at {kept}"
        return FileOutcome("conflict", detail)

    try:
        _replace_bytes(target, data)
    except FileExistsError:
        _finish_quarantine(sidecar)
        return FileOutcome(
            "conflict",
            "a newer save appeared while it was being restored, so it was kept",
        )
    except OSError as exc:
        kept = _put_back(sidecar, target)
        detail = f"{REASON_NOT_RESTORED} ({type(exc).__name__})"
        if kept:
            detail += f"; the previous content was kept at {kept}"
        return FileOutcome("conflict", detail)

    try:
        landed = _digest(target) == hashlib.sha256(data).hexdigest()
    except OSError:
        landed = False
    if not landed:
        kept = _put_back(sidecar, target)
        detail = (
            f"{REASON_NOT_RESTORED} "
            "(the file does not hold the pre-edit content afterwards)"
        )
        if kept:
            detail += f"; the previous content was kept at {kept}"
        return FileOutcome("conflict", detail)
    _finish_quarantine(sidecar)
    invalidate_bytecode(target)
    return FileOutcome("restored")


def remove_file(target: Path, expected_digest: str) -> FileOutcome:
    """Delete *target* only if it still holds the version the task produced."""
    try:
        current = _digest(target)
    except OSError as exc:
        return FileOutcome("conflict", f"it cannot be read ({type(exc).__name__})")
    if current != expected_digest:
        return FileOutcome("conflict", REASON_CHANGED_AFTER)

    sidecar, problem = _quarantine(target, operation="remove", digest=expected_digest)
    if sidecar is None:
        return FileOutcome("conflict", problem)
    try:
        taken = _digest(sidecar)
    except OSError:
        taken = ""
    if taken != expected_digest:
        kept = _put_back(sidecar, target)
        detail = REASON_CHANGED_AFTER
        if kept:
            detail += f"; the newer content was kept at {kept}"
        return FileOutcome("conflict", detail)
    try:
        sidecar.unlink()
    except OSError as exc:
        kept = _put_back(sidecar, target)
        detail = f"{REASON_NOT_REMOVED} ({type(exc).__name__})"
        if kept:
            detail += f"; the file was kept at {kept}"
        return FileOutcome("conflict", detail)
    _discard(_recovery_record_path(sidecar))
    return FileOutcome("removed")


def create_file(target: Path, data: bytes) -> FileOutcome:
    """Put *data* at a path that should be absent, never over an existing file."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _replace_bytes(target, data)
    except FileExistsError:
        return FileOutcome(
            "conflict",
            "a file appeared at that path while it was being restored, so it was kept",
        )
    except OSError as exc:
        return FileOutcome("conflict", f"{REASON_NOT_RESTORED} ({type(exc).__name__})")
    try:
        landed = _digest(target) == hashlib.sha256(data).hexdigest()
    except OSError:
        landed = False
    if not landed:
        return FileOutcome(
            "conflict",
            f"{REASON_NOT_RESTORED} (the file does not hold the pre-edit content afterwards)",
        )
    invalidate_bytecode(target)
    return FileOutcome("restored")


def describe_conflict(path: str, reason: str) -> str:
    """What is actually true of one file that was not undone.

    The reason carries the cause; the sentence carries the consequence, which
    is what a user needs in order to decide what to do next. "Changed after the
    task wrote it" means the newer content is theirs; "no record" means the
    file's provenance is unknown; a failed restore means the task's own edit is
    probably still in it.
    """
    text = str(reason or "")
    if text == REASON_CHANGED_AFTER:
        return (
            f"left {path} untouched: it changed after this task wrote it, so "
            "the newer version was kept"
        )
    if text == REASON_NO_RECORD:
        return (
            f"left {path} untouched: no record of the version this task "
            "produced, so its state is unconfirmed"
        )
    if text == REASON_NO_BASELINE:
        return f"{path} still holds this task's change: no pre-edit copy of it was kept"
    if text.startswith(REASON_NOT_RESTORED):
        return f"{path} still holds this task's change: restoring it failed"
    if text.startswith(REASON_NOT_REMOVED):
        return f"{path} is still there: this task created it but removing it failed"
    if text == REASON_GONE:
        return f"{path} no longer exists; there was nothing to undo"
    if text == REASON_OUTSIDE:
        return f"{path} is outside the project directory and was not touched"
    return f"left {path} untouched: {text}"


@dataclass(frozen=True)
class TaskChange:
    """One file this run wrote, with the version it produced.

    ``digest`` is ``None`` when the task wrote the file through a path that did
    not record its output. Such a file is never restored: without the produced
    version there is no way to tell it from a later save by the user.
    """

    relative: str
    digest: str | None = None
    created: bool = False


@dataclass
class RollbackOutcome:
    """What actually happened to each file, never what was intended."""

    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    blocked: str = ""
    # The edits this rollback discarded, and where their content was kept
    # before the restore removed it. Empty when no copy was saved, so every
    # existing report keeps exactly what it had.
    discarded: list[str] = field(default_factory=list)
    patch_dir: str = ""
    patch_errors: list[str] = field(default_factory=list)

    @property
    def touched(self) -> bool:
        return bool(self.restored or self.removed)

    @property
    def status(self) -> str:
        if self.blocked:
            return "blocked"
        if self.conflicts:
            return "partial" if self.touched else "blocked"
        return "rolled_back" if self.touched else "nothing_to_do"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "restored": list(self.restored),
            "removed": list(self.removed),
            "conflicts": [
                {"path": path, "reason": reason} for path, reason in self.conflicts
            ],
            "blocked": self.blocked,
        }

    @property
    def kept(self) -> list[str]:
        """Files left as they are, in the order they were examined."""
        return [path for path, _ in self.conflicts]

    @property
    def complete(self) -> bool:
        """True only when every task change was really undone."""
        return self.status == "rolled_back"

    def discarded_report(self) -> str:
        """Where the content this rollback dropped was kept, in one line.

        The undo is still an undo; this says the discarded version was not
        destroyed and can be re-applied deliberately, through the command that
        re-checks digests first.
        """
        if not self.discarded:
            return ""
        where = f" at {self.patch_dir}" if self.patch_dir else ""
        return (
            f"Kept the content this rollback discarded{where}: "
            + ", ".join(self.discarded)
            + ". Re-apply it with python -m giscode.gisai.recover_edits."
        )

    def summary(self) -> str:
        """One line that cannot be read as a full undo when it was not one.

        "Not undone" is deliberate: a conflict is not always a file that still
        holds the task's edit. A newer user save does not, and a missing version
        record makes the state unknown. The per-file account says which.
        """
        if self.blocked and not self.conflicts:
            return f"No edit could be undone: {self.blocked}."
        undone = self.restored + self.removed
        if not undone and not self.conflicts:
            return "There was nothing this task could safely undo."
        if not self.conflicts:
            return f"Every task change was undone: {', '.join(undone)}."
        parts: list[str] = []
        parts.append(
            f"Undone: {', '.join(undone)}."
            if undone
            else "No edit was undone."
        )
        parts.append(
            f"Not undone: {len(self.conflicts)} file(s) - "
            + "; ".join(f"{path} ({reason})" for path, reason in self.conflicts)
            + "."
        )
        return " ".join(parts)

    def describe(self) -> str:
        """A short, user-facing account of the file state, file by file."""
        if self.blocked and not self.conflicts:
            return f"The edits were left in place: {self.blocked}."
        lines: list[str] = []
        for path in self.restored:
            lines.append(f"  restored {path} to its pre-task content")
        for path in self.removed:
            lines.append(f"  removed {path} (created by this task)")
        for path, reason in self.conflicts:
            lines.append(f"  {describe_conflict(path, reason)}")
        if self.blocked:
            lines.append(f"  {self.blocked}")
        return "\n".join(lines)


def _project_files(root: Path):
    """Files under *root*, skipping interpreter/VCS/build scratch directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name.lower() not in EPHEMERAL_DIRECTORY_NAMES
        )
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _read_recovery_record(
    meta_path: Path, quarantine: Path, root: Path
) -> tuple[dict[str, Any] | None, str]:
    """The record behind *quarantine*, or ``None`` and the reason it is not usable.

    A filename is not evidence. Every field has to agree with the file it sits
    next to before recovery touches anything.
    """
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "there is no recovery record for it, so it was left for manual recovery"
    except (OSError, ValueError):
        return None, "its recovery record could not be read, so it was left for manual recovery"
    if not isinstance(payload, dict) or payload.get("format") != _RECOVERY_FORMAT:
        return None, "its recovery record is not in a known format"
    original = payload.get("original")
    digest = payload.get("digest")
    operation = payload.get("operation")
    if not isinstance(original, str) or not original.strip():
        return None, "its recovery record does not name the original file"
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest.lower())
    ):
        return None, "its recovery record has no usable content digest"
    if operation not in _RECOVERY_OPERATIONS:
        return None, "its recovery record does not say what the rollback was doing"
    try:
        target = Path(original)
        if not target.is_absolute():
            return None, "its recovery record names a relative path"
        resolved = target.resolve(strict=False)
    except (OSError, ValueError):
        return None, "its recovery record names a path that cannot be resolved"
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, "its recovery record points outside this project"
    try:
        same_directory = resolved.parent == quarantine.parent.resolve(strict=False)
    except OSError:
        same_directory = False
    if not same_directory:
        return None, "its recovery record points at a different directory"
    if not quarantine.name.startswith(f".{resolved.name}.giscode-quarantine-"):
        return None, "its name does not match its recovery record"
    return {
        "original": resolved,
        "digest": digest.lower(),
        "operation": operation,
    }, ""


def _move_absent(source: Path, target: Path) -> tuple[bool, str]:
    """Put *source* at *target* only while *target* is still absent.

    ``os.link`` fails when the name is taken, so a file that appeared after the
    interruption is never overwritten. Without hard links the fallback uses an
    exclusive create, which still refuses to overwrite.
    """
    try:
        os.link(source, target)
    except FileExistsError:
        return False, "the original path already holds a file; both were kept"
    except OSError:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        handle = None
        try:
            handle = os.open(str(target), flags, 0o666)
            os.write(handle, source.read_bytes())
        except FileExistsError:
            return False, "the original path already holds a file; both were kept"
        except OSError as exc:
            return False, f"it could not be put back ({type(exc).__name__})"
        finally:
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass
        try:
            if _digest(target) != _digest(source):
                return False, "it could not be put back (the copy does not match)"
        except OSError:
            return False, "it could not be put back (the copy could not be verified)"
    try:
        source.unlink()
    except OSError:
        # The bytes are at the original path; the duplicate is cleaned next run.
        pass
    return True, ""


def _recover_one(root: Path, quarantine: Path) -> RecoveryAction:
    meta_path = _recovery_record_path(quarantine)
    record, problem = _read_recovery_record(meta_path, quarantine, root)
    if record is None:
        return RecoveryAction(
            path=str(quarantine), quarantine=str(quarantine), status="preserved", reason=problem
        )
    original: Path = record["original"]
    operation: str = record["operation"]
    try:
        digest = _digest(quarantine)
    except OSError as exc:
        return RecoveryAction(
            str(original),
            str(quarantine),
            "preserved",
            operation,
            f"it could not be read ({type(exc).__name__})",
        )
    if digest != record["digest"]:
        return RecoveryAction(
            str(original),
            str(quarantine),
            "preserved",
            operation,
            "it changed after the rollback was interrupted",
        )
    if original.exists():
        try:
            already_back = _digest(original) == digest
        except OSError:
            already_back = False
        if already_back:
            # Recovery landed before the crash and left its own duplicate.
            _finish_quarantine(quarantine)
            return RecoveryAction(
                str(original),
                str(quarantine),
                "recovered",
                operation,
                "it had already been put back; the duplicate copy was removed",
            )
        return RecoveryAction(
            str(original),
            str(quarantine),
            "preserved",
            operation,
            "the original path already holds a different file; both were kept",
        )
    moved, problem = _move_absent(quarantine, original)
    if not moved:
        return RecoveryAction(
            str(original), str(quarantine), "preserved", operation, problem
        )
    _discard(meta_path)
    reason = (
        "the file the rollback was removing was put back"
        if operation == "remove"
        else "the version the rollback had moved aside was put back"
    )
    return RecoveryAction(
        str(original), str(quarantine), "recovered", operation, reason
    )


def recover_interrupted_rollbacks(project_root: str | Path) -> list[RecoveryAction]:
    """Repair files an interrupted rollback left under a quarantine name.

    Runs when a project is opened and before a new rollback, so a crash in the
    middle of one cannot leave a source path permanently empty. It is safe to
    run repeatedly: a recovered file takes both its quarantine copy and record
    with it, and anything ambiguous is left exactly where it is.

    A quarantine file is only considered when its record is present, complete
    and agrees with the file: same directory, matching name, and the same bytes
    that were moved aside. Anything else is reported, never guessed at.
    """
    try:
        root = Path(project_root).resolve()
    except (OSError, TypeError):
        return []
    if not str(project_root or "").strip() or not root.is_dir():
        return []

    quarantines: list[Path] = []
    records: list[Path] = []
    try:
        for path in _project_files(root):
            if ".giscode-quarantine-" not in path.name:
                continue
            if path.name.endswith(".json"):
                records.append(path)
            else:
                quarantines.append(path)
    except OSError:
        return []

    actions: list[RecoveryAction] = []
    for quarantine in quarantines:
        try:
            actions.append(_recover_one(root, quarantine))
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the rest
            actions.append(
                RecoveryAction(
                    str(quarantine),
                    str(quarantine),
                    "preserved",
                    "",
                    f"recovery could not examine it ({type(exc).__name__})",
                )
            )

    known = {str(item) for item in quarantines}
    for record in records:
        base = record.with_name(record.name[: -len(".json")])
        if str(base) in known:
            continue
        # A record with no quarantined file is housekeeping: no file is at risk.
        try:
            record.unlink()
        except OSError:
            continue
        actions.append(
            RecoveryAction(
                str(record),
                str(record),
                "skipped",
                "",
                "the recovery record had no quarantined file",
            )
        )
    return actions


def capture_changes(
    snapshot: Any,
    project_root: str | Path,
    written: Iterable[str],
    created: Iterable[str] = (),
) -> list[TaskChange]:
    """Fingerprint the files this run wrote, just after the edit landed.

    *written* is the run's own successful write-action paths. It is the scope:
    the snapshot directory may still hold baselines from an earlier run with the
    same task id, and those files are not this run's business. A path that no
    longer exists on disk is skipped - there is no produced version to guard.

    *created* names the paths whose action was a creation. A file counts as
    created only when that log *and* the snapshot agree - the action says
    create and no baseline was kept - so a snapshot copy that silently failed
    can never turn an existing file into something this module deletes.
    """
    try:
        root = Path(project_root).resolve()
    except (OSError, TypeError):
        return []
    if not str(project_root or "").strip():
        return []
    baseline_root: Path | None = None
    if snapshot is not None:
        try:
            baseline_root = Path(snapshot.root)
        except (AttributeError, TypeError):
            baseline_root = None

    created_hints = {
        normalise_relative(item).lower() for item in created if normalise_relative(item)
    }
    changes: list[TaskChange] = []
    seen: set[str] = set()
    for raw in written:
        relative = normalise_relative(raw)
        if not relative or relative.lower() in seen:
            continue
        seen.add(relative.lower())
        target = _inside(root, relative)
        if target is None or not target.is_file():
            continue
        try:
            digest = _digest(target)
        except OSError:
            continue
        is_created = False
        if baseline_root is not None and relative.lower() in created_hints:
            saved = _inside(baseline_root, relative)
            is_created = saved is not None and not saved.is_file()
        changes.append(TaskChange(relative, digest, is_created))
    return changes


def _save_discarded(
    directory: Path | None,
    task: str,
    root: Path,
    outcome: RollbackOutcome,
    change: TaskChange,
    target: Path,
    expected_digest: str,
    baseline: bytes | None,
) -> None:
    """Keep the bytes a rollback is about to drop, before it drops them.

    Best effort and never fatal: failing to save a recovery copy must not
    stop an undo that is already needed. A file whose bytes are no longer
    the ones this task produced is skipped - that is the user-save conflict
    case, and their version is not task output to file away.
    """
    if directory is None:
        return
    try:
        produced = target.read_bytes()
    except OSError:
        return
    if hashlib.sha256(produced).hexdigest() != expected_digest:
        return
    entry, problem = recovery_store.record_discarded(
        directory,
        task=task,
        project_root=root,
        relative=change.relative,
        produced_bytes=produced,
        baseline_bytes=baseline,
        created=change.created,
    )
    if entry is None:
        outcome.patch_errors.append(problem)
        return
    if change.relative not in outcome.discarded:
        outcome.discarded.append(change.relative)
    outcome.patch_dir = str(directory)


def apply_rollback(
    snapshot: Any,
    project_root: str | Path,
    changes: Iterable[TaskChange],
) -> RollbackOutcome:
    """Restore the captured changes, refusing anything that is no longer ours.

    A file whose bytes differ from the captured fingerprint was changed after
    this task wrote it - by the user, an editor or another process - so it is
    reported as a conflict and left exactly as it is. A created file is removed
    only under the same guard.

    The restore itself is done by :func:`restore_file` / :func:`remove_file`,
    which quarantine the file and re-check the bytes they actually took, so a
    save landing during the rollback is preserved rather than overwritten.
    """
    outcome = RollbackOutcome()
    remaining = list(changes)
    if not remaining:
        outcome.blocked = "no file written by this task could be identified"
        return outcome
    if snapshot is None:
        outcome.blocked = (
            "no snapshot of the pre-edit files is available, so the change "
            "cannot be undone safely"
        )
        return outcome
    try:
        root = Path(project_root).resolve()
        baseline_root = Path(snapshot.root)
    except (OSError, TypeError, AttributeError):
        outcome.blocked = "the project or snapshot path could not be resolved"
        return outcome

    # Every discarded version is written here *before* the restore below
    # removes it; a save that fails is reported, never allowed to stop the undo.
    directory = recovery_store.directory_for_snapshot(snapshot)
    task = recovery_store.task_for_snapshot(snapshot)
    for change in remaining:
        target = _inside(root, change.relative)
        if target is None:
            outcome.conflicts.append((change.relative, REASON_OUTSIDE))
            continue
        if not target.is_file():
            outcome.conflicts.append((change.relative, REASON_GONE))
            continue
        if change.digest is None:
            outcome.conflicts.append((change.relative, REASON_NO_RECORD))
            continue

        if change.created:
            _save_discarded(directory, task, root, outcome, change, target, change.digest, None)
            result = remove_file(target, change.digest)
            if result.status == "removed":
                outcome.removed.append(change.relative)
            else:
                outcome.conflicts.append((change.relative, result.reason))
            continue

        saved = _inside(baseline_root, change.relative)
        if saved is None or not saved.is_file():
            outcome.conflicts.append((change.relative, REASON_NO_BASELINE))
            continue
        try:
            data = saved.read_bytes()
        except OSError as exc:
            outcome.conflicts.append(
                (change.relative, f"no pre-edit copy of it could be read ({type(exc).__name__})")
            )
            continue
        _save_discarded(directory, task, root, outcome, change, target, change.digest, data)
        result = restore_file(target, change.digest, data)
        if result.status == "restored":
            outcome.restored.append(change.relative)
        else:
            outcome.conflicts.append((change.relative, result.reason))
    return outcome


def changes_from_snapshot(
    snapshot: Any, project_root: str | Path, problems: list[str] | None = None
) -> list[TaskChange]:
    """The changes a snapshot knows about, from its own records.

    Scope comes from ``applied_edits`` - the files the snapshot says still
    differ from their pre-edit state. The produced digest comes from what the
    write path recorded. A file with no recorded output is still listed, with
    no digest, so the caller reports it as un-restorable instead of silently
    leaving it out of the account.
    """
    recorded: dict[str, TaskChange] = {}
    issues = problems if problems is not None else []
    lister = getattr(snapshot, "written_files", None)
    if callable(lister):
        try:
            for relative, digest, created in lister():
                name = normalise_relative(relative)
                if name:
                    recorded[name.lower()] = TaskChange(name, digest, bool(created))
        except Exception as exc:  # noqa: BLE001 - a broken record is no safe undo
            recorded = {}
            issues.append(
                f"the recorded write list could not be read ({type(exc).__name__})"
            )

    applied: list[str] = []
    applied_lister = getattr(snapshot, "applied_edits", None)
    if callable(applied_lister):
        try:
            applied = list(applied_lister(Path(project_root)))
        except Exception as exc:  # noqa: BLE001
            applied = []
            issues.append(
                f"the pre-edit snapshot could not be read ({type(exc).__name__})"
            )

    changes: list[TaskChange] = []
    for relative in applied:
        name = normalise_relative(relative)
        if not name:
            continue
        changes.append(recorded.get(name.lower()) or TaskChange(name))
    return changes


def rollback_task_edits(
    snapshot: Any, project_root: str | Path
) -> RollbackOutcome:
    """Undo a task's edits from its snapshot, with the concurrency guard.

    This is the entry point the existing rollback paths use; it replaces a
    blind ``snapshot.rollback()`` with the same restore, minus the files that
    changed after the task wrote them.

    Three outcomes are kept apart on purpose: nothing was edited (nothing to
    do), the snapshot itself could not be read (blocked, with the reason), and
    the restore ran (its own per-file result). Collapsing the first two into
    "blocked" would tell a task that changed nothing that its edits remain.
    """
    # A crash in an earlier rollback can leave a file quarantined; repair that
    # before deciding what this rollback has to do, or the file stays missing.
    try:
        recover_interrupted_rollbacks(project_root)
    except Exception:  # noqa: BLE001 - recovery must never block a rollback
        pass
    problems: list[str] = []
    changes = changes_from_snapshot(snapshot, project_root, problems)
    if problems and not changes:
        return RollbackOutcome(blocked="; ".join(problems))
    if not changes:
        return RollbackOutcome()
    return apply_rollback(snapshot, project_root, changes)
