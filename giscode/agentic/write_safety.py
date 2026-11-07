"""Guarded write support shared by GisAi file tools and executors."""
from __future__ import annotations

import difflib
import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from giscode.config.paths import giscode_home


DENIED_DIRECTORY_NAMES = frozenset({
    ".git", ".giscode", "node_modules", "__pycache__", "pycache",
    "venv", ".venv", "env", ".env", "build", "dist",
})


def denied_path_reason(path: Path, project_root: Path) -> str | None:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError:
        return f"Path escapes workspace: {path}"
    for part in (item.lower() for item in resolved.parts):
        if part in DENIED_DIRECTORY_NAMES:
            return f"Writes are denied under protected path component: {part}"
    if resolved.name.lower().startswith(".env"):
        return "Writes to .env files are denied"
    return None


def invalidate_bytecode(path: Path) -> None:
    """Drop cached bytecode for a Python file that was just written or restored.

    CPython validates a ``.pyc`` against the source's size and its mtime
    truncated to whole seconds, so an edit that lands in the same second and
    keeps the file the same size is judged "unchanged" and the next test run
    executes the *old* bytecode - certifying a broken edit as passing. Dropping
    the cache entries for the written file removes that class of false result.
    """
    if path.suffix != ".py":
        return
    cache_dir = path.parent / "__pycache__"
    if not cache_dir.is_dir():
        return
    prefix = f"{path.stem}."
    try:
        for cached in cache_dir.iterdir():
            if cached.is_file() and cached.name.startswith(prefix) and cached.suffix in {".pyc", ".pyo"}:
                cached.unlink()
    except OSError:
        pass  # A cache we cannot clean is not a reason to fail the write.


class SnapshotManager:
    """Task-scoped snapshots stored outside the project tree."""

    def __init__(self, task_id: str, profile_root: Path | None = None):
        root = profile_root or giscode_home()
        self.root = root / "snapshots" / str(task_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self._created: set[str] = set()
        self._snapshotted: set[str] = set()
        # The version each write produced, and the project-relative name for
        # it. A rollback that restores blindly overwrites whatever is on disk;
        # these records are what let it tell its own output apart from a save
        # the user made after the task wrote the file.
        self._produced: dict[str, str] = {}
        self._relative: dict[str, str] = {}

    def snapshot(self, path: Path, project_root: Path) -> None:
        resolved = path.resolve(strict=False)
        key = str(resolved).lower()
        if key in self._snapshotted or not resolved.exists():
            return
        relative = resolved.relative_to(project_root.resolve())
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, destination)
        self._snapshotted.add(key)
        self._relative[key] = str(relative).replace("\\", "/")

    def record_created(self, path: Path) -> None:
        self._created.add(str(path.resolve(strict=False)).lower())

    def record_written(self, path: Path, project_root: Path) -> None:
        """Remember the exact version this task just produced for *path*.

        Called by every write path immediately after the bytes land. A later
        rollback compares the file against this digest and leaves it alone when
        they differ, so a user save during the task is never overwritten. A
        write that never records leaves the file without a known produced
        version; rollback then refuses to touch it rather than guessing.
        """
        resolved = path.resolve(strict=False)
        try:
            relative = resolved.relative_to(project_root.resolve())
        except (OSError, ValueError):
            return
        try:
            data = resolved.read_bytes()
        except OSError:
            return
        key = str(resolved).lower()
        self._produced[key] = hashlib.sha256(data).hexdigest()
        self._relative[key] = str(relative).replace("\\", "/")

    def written_files(self) -> list[tuple[str, str, bool]]:
        """``(relative path, produced digest, created)`` for this task's writes.

        Only files whose post-write bytes were recorded appear here. The
        created flag is true only when the path was registered as created and
        no pre-edit baseline exists, so a copy that failed cannot turn an
        existing file into one a rollback deletes.
        """
        root = self.root
        written: list[tuple[str, str, bool]] = []
        for key, digest in sorted(self._produced.items()):
            relative = self._relative.get(key)
            if not relative:
                continue
            created = key in self._created and not (root / relative).is_file()
            written.append((relative, digest, created))
        return written

    def record_bytes(self, path: Path, project_root: Path, data: bytes) -> None:
        """Store caller-supplied *pre-change* bytes as this path's baseline.

        ``snapshot`` reads the file from disk, so it can only be used before
        the change it must undo. The run_command guard captures the bytes
        around a command and hands them over afterwards, when the file on disk
        already holds the post-command content. One-shot like ``snapshot``: the
        earliest baseline always wins, so existing rollback behaviour is
        unchanged.
        """
        resolved = path.resolve(strict=False)
        key = str(resolved).lower()
        if key in self._snapshotted:
            return
        try:
            relative = resolved.relative_to(project_root.resolve())
        except ValueError:
            return
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        self._snapshotted.add(key)
        self._relative[key] = str(relative).replace("\\", "/")
        # The caller captured these bytes around a command, so the file on disk
        # already holds the post-change content: that is the produced version.
        try:
            current = resolved.read_bytes()
        except OSError:
            current = None
        if current is not None:
            self._produced[key] = hashlib.sha256(current).hexdigest()

    def applied_edits(self, project_root: Path) -> list:
        """Relative paths this task wrote but has not verified.

        Anything still differing from its pre-edit snapshot was modified after
        the snapshot was taken, and a file this task created counts too.
        Verification only ever runs at the end of a completed execution, so a
        difference here means the edit was applied and never verified.
        """
        root = project_root.resolve()
        changed: set[str] = set()
        if self.root.exists():
            for saved in self.root.rglob("*"):
                if not saved.is_file():
                    continue
                relative = saved.relative_to(self.root)
                target = root / relative
                try:
                    if not target.exists() or saved.read_bytes() != target.read_bytes():
                        changed.add(str(relative))
                except OSError:
                    continue
        for created in self._created:
            target = Path(created)
            try:
                relative = target.resolve().relative_to(root)
            except (ValueError, OSError):
                continue
            if target.is_file():
                changed.add(str(relative))
        return sorted(changed)

    def rollback(self, project_root: Path) -> None:
        if self.root.exists():
            for saved in self.root.rglob("*"):
                if saved.is_file():
                    relative = saved.relative_to(self.root)
                    target = project_root.resolve() / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(saved, target)
                    invalidate_bytecode(target)
        for created in self._created:
            target = Path(created)
            try:
                target.resolve().relative_to(project_root.resolve())
            except ValueError:
                continue
            if target.is_file():
                target.unlink()

    undo = rollback


# --------------------------------------------------------------------------
# Project-file guard for run_command (approval-bypass fix)
#
# Gating write_file/edit_file alone leaves a hole: run_command reaches the
# filesystem through anything the shell offers, so a plan can write a helper
# script and run it (J1_a3) or run one `python -c` (J3_a3) and change a
# tracked file with no Apply dialog ever appearing.
#
# The guard closes it by evidence instead of by deny-list: it hashes every
# project file around each command and treats any difference as an edit that
# must clear the same approval path the file tools use. A change that is not
# approved is put back byte-for-byte, so a command can never leave a project
# edit behind that the user did not accept.
# --------------------------------------------------------------------------

# Directories whose contents are build/interpreter scratch, not project source.
# pytest, unittest, git and the compilers all write here during a harmless run.
EPHEMERAL_DIRECTORY_NAMES = frozenset({
    "__pycache__", "pycache", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".git", ".giscode", "node_modules", "venv", ".venv", ".tox", ".eggs",
    "env",
})
EPHEMERAL_FILE_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd"})
EPHEMERAL_FILE_NAMES = frozenset({".coverage", ".DS_Store"})

# Bytes kept in memory for an exact, immediate restore. A file over the
# per-file cap is still detected by hash; its baseline is written to the task
# snapshot at capture time instead, so rollback can still restore it.
_MAX_BUFFERED_FILE_BYTES = 4 * 1024 * 1024
_MAX_BUFFERED_TOTAL_BYTES = 64 * 1024 * 1024


def is_ephemeral(path: Path, root: Path) -> bool:
    """True for interpreter/VCS scratch that a harmless command may rewrite."""
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve())
    except ValueError:
        return True  # outside the project: never the guard's business
    for part in relative.parts[:-1]:
        if part.lower() in EPHEMERAL_DIRECTORY_NAMES:
            return True
    name = relative.name
    if name.lower() in EPHEMERAL_FILE_NAMES:
        return True
    if Path(name).suffix.lower() in EPHEMERAL_FILE_SUFFIXES:
        return True
    return False


def command_change_diff(change: "FileChange") -> str:
    """Unified diff for a command-induced change, in the Apply-dialog format."""
    before = change.before
    after = change.after
    old = before.decode("utf-8", errors="replace").splitlines(keepends=True) if before is not None else []
    new = after.decode("utf-8", errors="replace").splitlines(keepends=True) if after is not None else []
    target = str(change.path)
    return "".join(difflib.unified_diff(old, new, fromfile=target, tofile=target))


@dataclass
class FileChange:
    """One project file a run_command created, changed, or removed."""

    path: Path
    relative: str
    existed_before: bool
    before: bytes | None = None
    after: bytes | None = None

    @property
    def verb(self) -> str:
        if not self.existed_before:
            return "created"
        if self.after is None:
            return "removed"
        return "modified"


@dataclass
class GuardOutcome:
    """What the guard did with every difference a command produced."""

    changes: list[FileChange] = field(default_factory=list)
    approved: list[FileChange] = field(default_factory=list)
    restored: list[FileChange] = field(default_factory=list)
    unrestorable: list[FileChange] = field(default_factory=list)
    # One reason per file the guard refused to touch, so the report can say
    # whether a newer save was kept, the file was locked, or nothing was known.
    conflicts: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    @property
    def blocked(self) -> bool:
        return bool(self.restored or self.unrestorable)

    def report(self) -> str:
        rolled = ", ".join(c.relative for c in self.restored)
        lines = []
        if rolled:
            lines.append(
                f"run_command changed project files outside the approval path; "
                f"those changes were rolled back: {rolled}"
            )
        if self.unrestorable:
            detail = "; ".join(
                f"{path} ({reason})" for path, reason in self.conflicts
            ) or ", ".join(c.relative for c in self.unrestorable)
            lines.append(
                "run_command changed project files outside the approval path "
                "and they were left in place, not undone: " + detail
            )
        lines.append(
            "Propose edits with write_file/edit_file so every change goes "
            "through the Apply proposed edit dialog."
        )
        return "; ".join(lines)


class ProjectFileGuard:
    """Hashes the project around each run_command and gates what changed.

    ``approve`` is the host Apply/Reject callback, identical in shape to the
    one the file tools use (``callback(path, diff) -> bool``). With no host
    bound and no auto-approve flag a task-scoped change is refused, which is
    exactly the contract ``_is_approved`` already applies to file edits.
    """

    def __init__(
        self,
        project_root: Path,
        snapshot: SnapshotManager | None = None,
        approve=None,
        auto_approve: bool = False,
    ) -> None:
        self.root = Path(project_root).resolve()
        self._snapshot = snapshot
        self._approve = approve
        self._auto_approve = bool(auto_approve)
        self._buffer: dict[str, bytes] = {}

    # -- capture / detect ------------------------------------------------

    def _iter_files(self):
        if not self.root.is_dir():
            return
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            if is_ephemeral(path, self.root):
                continue
            yield path

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve(strict=False).relative_to(self.root))
        except ValueError:
            return str(path)

    def capture(self) -> dict[str, str]:
        """Snapshot every project file: hash for detection, bytes for restore."""
        manifest: dict[str, str] = {}
        self._buffer = {}
        budget = _MAX_BUFFERED_TOTAL_BYTES
        for path in self._iter_files():
            try:
                data = path.read_bytes()
            except OSError:
                continue
            relative = self._relative(path)
            manifest[relative] = hashlib.sha256(data).hexdigest()
            if len(data) <= _MAX_BUFFERED_FILE_BYTES and len(data) <= budget:
                self._buffer[relative] = data
                budget -= len(data)
            elif self._snapshot is not None:
                # Too large to hold: keep an on-disk pre-command baseline so
                # task-level rollback can still put this file back.
                self._snapshot.snapshot(path, self.root)
        return manifest

    def detect(self, before: dict[str, str]) -> list[FileChange]:
        """Every project file whose bytes differ from the capture."""
        changes: list[FileChange] = []
        seen: set[str] = set()
        for path in self._iter_files():
            relative = self._relative(path)
            seen.add(relative)
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if before.get(relative) == hashlib.sha256(data).hexdigest():
                continue
            existed = relative in before
            changes.append(FileChange(
                path=path,
                relative=relative,
                existed_before=existed,
                before=self._buffer.get(relative) if existed else None,
                after=data,
            ))
        for relative in sorted(set(before) - seen):
            changes.append(FileChange(
                path=self.root / relative,
                relative=relative,
                existed_before=True,
                before=self._buffer.get(relative),
                after=None,
            ))
        return changes

    # -- gate ------------------------------------------------------------

    def gate(self, changes: list[FileChange]) -> GuardOutcome:
        outcome = GuardOutcome(changes=list(changes))
        for change in sorted(changes, key=lambda item: item.relative.lower()):
            if self._is_approved(change):
                outcome.approved.append(change)
                self._record_approved(change)
                continue
            restored, reason = self._restore(change)
            if restored:
                outcome.restored.append(change)
            else:
                outcome.unrestorable.append(change)
                outcome.conflicts.append((change.relative, reason))
        return outcome

    def _is_approved(self, change: FileChange) -> bool:
        if self._auto_approve:
            return True
        if not callable(self._approve):
            return False  # a task with no host callback never gets a free pass
        try:
            return bool(self._approve(change.path, command_change_diff(change)))
        except Exception:  # noqa: BLE001 - a host that throws is a rejection
            return False

    def _record_approved(self, change: FileChange) -> None:
        """Register an approved command change with the task snapshot.

        Task-level rollback keeps working exactly as before: modified files
        get their pre-command bytes, created files go through record_created.
        """
        if self._snapshot is None:
            return
        if not change.existed_before:
            self._snapshot.record_created(change.path)
        elif change.before is not None:
            self._snapshot.record_bytes(change.path, self.root, change.before)
        # else: the pre-command baseline was already written at capture time.

    def _restore(self, change: FileChange) -> tuple[bool, str]:
        """Undo one command change only while the file is still its output.

        ``change.after`` is the bytes the command left, read immediately after
        it ran. That is the produced-version record: the restore is safe only
        while the file still holds exactly those bytes, because the approval
        dialog can stay open for minutes and the user may save meanwhile. The
        actual put-back and removal go through ``safe_rollback``, which also
        protects the window inside the restore itself.
        """
        from giscode.agentic.rollback import (
            REASON_NO_PRE_COMMAND,
            create_file,
            remove_file,
            restore_file,
        )

        produced = (
            hashlib.sha256(change.after).hexdigest()
            if change.after is not None
            else None
        )
        if not change.existed_before:
            # Created by this command inside this same tool call. Removing it
            # is the rollback the snapshot already performs for files the task
            # created; no file that existed before is deleted.
            if produced is None:
                return False, "it was already gone before it could be removed"
            result = remove_file(change.path, produced)
            return result.status == "removed", result.reason

        data = change.before
        if data is None and self._snapshot is not None:
            saved = self._snapshot.root / Path(change.relative)
            if saved.is_file():
                try:
                    data = saved.read_bytes()
                except OSError:
                    data = None
        if data is None:
            return False, REASON_NO_PRE_COMMAND

        if change.after is None:
            # The command deleted the file; put the pre-command bytes back.
            result = create_file(change.path, data)
            return result.status == "restored", result.reason

        result = restore_file(change.path, produced, data)
        if result.status != "restored":
            return False, result.reason
        invalidate_bytecode(change.path)
        return True, ""

