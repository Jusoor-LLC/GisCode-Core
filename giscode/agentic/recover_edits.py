"""Native recovery for discarded and interrupted Core coding transactions.

A failed coding task restores the project to its pre-task bytes. The version
the task had produced is kept in the profile's ``recovery/<task>`` directory,
and an unfinished task leaves a durable ``transactions/<task>.json`` record.
This module is the Core-side owner of both flows:

* ``restore`` re-applies a deliberately discarded task version, after checking
  every digest, the project boundary and explicit approval;
* ``recover-pending`` rolls back a task transaction left ``open`` by a crash,
  using the same snapshot/rollback engine the coding loop uses.

Nothing here imports ``giscode.gisai``. The command is part of the public Core
package and works in a fresh interpreter with every GisAI import blocked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from giscode.agentic import recovery_store
from giscode.agentic.rollback import (
    REASON_CHANGED_AFTER,
    RollbackOutcome,
    normalise_relative,
    remove_file,
    restore_file,
    rollback_task_edits,
)
from giscode.agentic.write_safety import (
    PENDING_TRANSACTION_STATUSES,
    SnapshotManager,
    atomic_write_text,
)
from giscode.config.paths import giscode_home

__all__ = [
    "RecoveryOutcome",
    "PendingRecovery",
    "restore_recovery_directory",
    "recover_pending_transactions",
    "find_recoveries",
    "pending_transactions",
    "scan_transactions",
    "main",
]

_TRANSACTION_FORMAT = 1
_DEFAULT_RECOVERY_DIR = "recovery"
_DEFAULT_SNAPSHOT_DIR = "snapshots"
_DEFAULT_TRANSACTION_DIR = "transactions"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_task(task: str) -> str:
    text = str(task or "").strip()
    if not text or text in {".", ".."}:
        return ""
    if Path(text).name != text or "/" in text or "\\" in text:
        return ""
    return text


def _norm(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve()).lower()
    except (OSError, TypeError, ValueError):
        return ""


def _target(root: Path, relative: str) -> Path | None:
    text = normalise_relative(relative)
    if not text or text.startswith("/") or ".." in Path(text).parts:
        return None
    if Path(text).is_absolute() or (Path(text).parts and ":" in Path(text).parts[0]):
        return None
    try:
        target = (root / text).resolve(strict=False)
        target.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return target


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _profile(profile_root: str | Path | None) -> Path:
    return Path(profile_root).expanduser().resolve() if profile_root else giscode_home()


def _recovery_root(profile_root: str | Path | None = None) -> Path:
    return _profile(profile_root) / _DEFAULT_RECOVERY_DIR


def _snapshot_root(directory: Path, task: str, profile_root: str | Path | None = None) -> Path:
    root = _profile(profile_root)
    if directory.parent.parent == root:
        return root / _DEFAULT_SNAPSHOT_DIR / task
    return directory.parent.parent / _DEFAULT_SNAPSHOT_DIR / task


@dataclass
class RecoveryOutcome:
    """What a native re-apply actually changed, never what it intended."""

    directory: Path
    task: str = ""
    project_root: str = ""
    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    blocked: str = ""
    dry_run: bool = False
    audit_path: str = ""
    audit_error: str = ""

    @property
    def success(self) -> bool:
        return not self.blocked and not self.conflicts

    def as_dict(self) -> dict:
        return {
            "directory": str(self.directory),
            "task": self.task,
            "project_root": self.project_root,
            "status": (
                "blocked"
                if self.blocked
                else "partial"
                if self.conflicts
                else "restored"
                if self.restored or self.removed
                else "nothing_to_do"
            ),
            "dry_run": self.dry_run,
            "success": self.success,
            "restored": list(self.restored),
            "removed": list(self.removed),
            "planned": list(self.planned),
            "skipped": list(self.skipped),
            "conflicts": [
                {"path": path, "reason": reason} for path, reason in self.conflicts
            ],
            "blocked": self.blocked,
            "audit_path": self.audit_path,
            "audit_error": self.audit_error,
            "updated_at": _now(),
        }

    def report(self) -> str:
        if self.blocked:
            return f"No change could be made: {self.blocked}."
        lines = []
        if self.dry_run:
            lines.append(
                "Dry run: no project file was changed."
                if not self.planned
                else "Dry run: would restore " + ", ".join(self.planned) + "."
            )
        elif self.restored or self.removed:
            lines.append(
                "Restored "
                + ", ".join(self.restored + self.removed)
                + " from the recovery record."
            )
        else:
            lines.append("Nothing needed restoring.")
        if self.skipped:
            lines.append("Already safe: " + ", ".join(self.skipped) + ".")
        if self.conflicts:
            lines.append(
                "Left untouched: "
                + "; ".join(f"{path} ({reason})" for path, reason in self.conflicts)
                + "."
            )
        return " ".join(lines)


@dataclass
class PendingRecovery:
    """One durable ``open`` transaction found after a restart."""

    task: str
    project_root: str
    status: str
    outcome: RollbackOutcome | None = None
    blocked: str = ""

    def as_dict(self) -> dict:
        payload = {
            "task": self.task,
            "project_root": self.project_root,
            "status": self.status,
            "blocked": self.blocked,
        }
        if self.outcome is not None:
            payload["result"] = self.outcome.as_dict()
        return payload


def _write_audit(outcome: RecoveryOutcome) -> None:
    if outcome.dry_run:
        return
    target = outcome.directory / f"restore-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    try:
        atomic_write_text(
            target,
            json.dumps(outcome.as_dict(), indent=2, sort_keys=True, default=str),
        )
    except OSError as exc:
        outcome.audit_error = f"the recovery audit could not be written ({type(exc).__name__})"
        return
    outcome.audit_path = str(target)


def _entry_diff(entry: recovery_store.DiscardedEdit) -> str:
    text = entry.diff_text()
    if text:
        return text
    return f"Recovery copy for {entry.relative} (diff unavailable)"


def restore_recovery_directory(
    directory: str | Path,
    *,
    project_root: str | Path | None = None,
    approve: Callable[[str, str], bool] | None = None,
    dry_run: bool = False,
    profile_root: str | Path | None = None,
) -> RecoveryOutcome:
    """Re-apply one task's discarded version, refusing unsafe or changed files.

    ``approve(path, diff)`` is required for a real restore. It is called once
    per file, before anything is changed. A missing callback fails closed.
    """
    directory = Path(directory).expanduser().resolve()
    manifest = recovery_store.load(directory)
    out = RecoveryOutcome(directory=directory, task=manifest.task, dry_run=dry_run)
    if manifest.errors:
        out.blocked = "; ".join(manifest.errors)
        return out
    if not manifest.entries:
        out.blocked = "there is no usable discarded edit in this recovery record"
        return out

    task = _safe_task(manifest.task)
    if not task:
        out.blocked = "the recovery record does not name a safe task"
        return out

    root_text = str(project_root or manifest.project_root or "").strip()
    if not root_text:
        out.blocked = "the recovery record does not name a project root"
        return out
    try:
        root = Path(root_text).expanduser().resolve()
    except (OSError, ValueError):
        out.blocked = "the recovered project root cannot be resolved"
        return out
    if project_root is not None and _norm(root) != _norm(project_root):
        out.blocked = "the recovery record belongs to a different project"
        return out
    if not root.is_dir():
        out.blocked = "the recovered project root is not an existing directory"
        return out
    out.project_root = str(root)
    if not dry_run and not callable(approve):
        out.blocked = "explicit approval is required before restoring discarded edits"
        return out

    snapshots = _snapshot_root(directory, task, profile_root)
    for entry in manifest.entries:
        if entry.restored:
            out.skipped.append(entry.relative)
            continue
        relative = normalise_relative(entry.relative)
        target = _target(root, relative)
        if target is None:
            out.conflicts.append((entry.relative, "the path is outside the project"))
            continue
        try:
            blob = entry.blob.resolve(strict=True)
            blob.relative_to(directory.resolve())
        except (OSError, ValueError):
            out.conflicts.append((entry.relative, "its saved recovery copy is missing or unsafe"))
            continue
        try:
            saved = blob.read_bytes()
        except OSError as exc:
            out.conflicts.append(
                (entry.relative, f"its saved recovery copy cannot be read ({type(exc).__name__})")
            )
            continue
        if hashlib.sha256(saved).hexdigest() != entry.produced:
            out.conflicts.append(
                (entry.relative, "its saved recovery copy does not match the recorded digest")
            )
            continue

        try:
            current = _digest(target)
        except OSError as exc:
            if entry.created and not target.exists():
                out.skipped.append(entry.relative)
                continue
            out.conflicts.append(
                (
                    entry.relative,
                    "it is missing" if not target.exists() else f"it cannot be read ({type(exc).__name__})",
                )
            )
            continue

        if entry.created:
            if current == entry.produced:
                if dry_run:
                    out.planned.append(entry.relative)
                    continue
                if not _approved(approve, entry.relative, _entry_diff(entry)):
                    out.conflicts.append((entry.relative, "approval was declined"))
                    continue
                result = remove_file(target, entry.produced)
                if result.status == "removed":
                    out.removed.append(entry.relative)
                    _mark_restored(directory, entry.relative, out)
                else:
                    out.conflicts.append((entry.relative, result.reason))
                continue
            out.conflicts.append((entry.relative, REASON_CHANGED_AFTER))
            continue

        if current == entry.produced and not entry.restored:
            # The task's discarded version is already on disk. There is
            # nothing sensitive to do; remembering it prevents a later
            # invocation from treating that state as a fresh re-apply.
            out.skipped.append(entry.relative)
            _mark_restored(directory, entry.relative, out)
            continue
        if current != entry.baseline and current != entry.produced:
            out.conflicts.append((entry.relative, REASON_CHANGED_AFTER))
            continue

        baseline_path = _target(snapshots, relative)
        if baseline_path is None or not baseline_path.is_file():
            out.conflicts.append((entry.relative, "no pre-edit copy of it is available"))
            continue
        try:
            baseline = baseline_path.read_bytes()
        except OSError as exc:
            out.conflicts.append(
                (entry.relative, f"its pre-edit copy cannot be read ({type(exc).__name__})")
            )
            continue
        if not entry.baseline:
            out.conflicts.append((entry.relative, "no pre-edit digest of it was recorded"))
            continue
        if hashlib.sha256(baseline).hexdigest() != entry.baseline:
            out.conflicts.append(
                (entry.relative, "its pre-edit copy does not match the recorded digest")
            )
            continue
        if dry_run:
            out.planned.append(entry.relative)
            continue
        if not _approved(approve, entry.relative, _entry_diff(entry)):
            out.conflicts.append((entry.relative, "approval was declined"))
            continue
        expected = entry.baseline if current == entry.baseline else entry.produced
        result = restore_file(target, expected, saved)
        if result.status == "restored":
            out.restored.append(entry.relative)
            _mark_restored(directory, entry.relative, out)
        else:
            out.conflicts.append((entry.relative, result.reason))

    _write_audit(out)
    return out


def _mark_restored(
    directory: Path, relative: str, outcome: RecoveryOutcome
) -> None:
    """Persist idempotency state; an audit failure never claims a lost restore."""
    try:
        if not recovery_store.mark_restored(directory, relative):
            outcome.audit_error = outcome.audit_error or (
                f"the recovery state for {relative} could not be updated"
            )
    except OSError as exc:
        outcome.audit_error = outcome.audit_error or (
            f"the recovery state for {relative} could not be updated ({type(exc).__name__})"
        )


def _approved(
    approve: Callable[[str, str], bool] | None, relative: str, diff: str
) -> bool:
    if not callable(approve):
        return False
    try:
        return bool(approve(relative, diff))
    except Exception:  # noqa: BLE001 - a throwing host is a rejection
        return False


def find_recoveries(
    *, profile_root: str | Path | None = None, project_root: str | Path | None = None
) -> list[recovery_store.Manifest]:
    """Every recovery record in the profile, optionally for one project."""
    root = _recovery_root(profile_root)
    if not root.is_dir():
        return []
    wanted = _norm(project_root) if project_root is not None else ""
    found: list[recovery_store.Manifest] = []
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return []
    for child in children:
        if not child.is_dir() or not _safe_task(child.name):
            continue
        manifest = recovery_store.load(child)
        if not manifest.entries and not manifest.errors:
            continue
        if wanted and _norm(manifest.project_root) != wanted:
            continue
        found.append(manifest)
    return found


def _scan_transaction_records(
    *, profile_root: str | Path | None = None, project_root: str | Path | None = None
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Pending transaction records, plus every record that cannot be read.

    A record that cannot be parsed is not silently ignored: Core cannot know
    what it described, so it is reported as still requiring recovery rather
    than letting a damaged file look like "nothing to do".
    """
    root = _profile(profile_root) / _DEFAULT_TRANSACTION_DIR
    if not root.is_dir():
        return [], []
    wanted = _norm(project_root) if project_root is not None else ""
    found: list[dict] = []
    problems: list[tuple[str, str]] = []
    try:
        paths = sorted(root.glob("*.json"), key=lambda item: item.name.lower())
    except OSError:
        return [], []
    for path in paths:
        name = _safe_task(path.stem)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(
                (
                    name,
                    f"the transaction record {path.name} could not be read "
                    f"({type(exc).__name__})",
                )
            )
            continue
        if not isinstance(payload, dict):
            problems.append(
                (name, f"the transaction record {path.name} is not in a known format")
            )
            continue
        if payload.get("status") not in PENDING_TRANSACTION_STATUSES:
            # Completed, cancelled or otherwise resolved work needs nothing.
            continue
        if payload.get("format") != _TRANSACTION_FORMAT:
            problems.append(
                (name, f"the transaction record {path.name} is not in a known format")
            )
            continue
        if wanted and _norm(payload.get("project_root", "")) != wanted:
            continue
        task = _safe_task(payload.get("task", "")) or name
        if not task:
            problems.append(
                (name, f"the transaction record {path.name} does not name a safe task")
            )
            continue
        payload = dict(payload)
        payload["task"] = task
        found.append(payload)
    return found, problems


def pending_transactions(
    *, profile_root: str | Path | None = None, project_root: str | Path | None = None
) -> list[dict]:
    """Read durable transaction records that still describe an unfinished task."""
    return _scan_transaction_records(
        profile_root=profile_root, project_root=project_root
    )[0]


_STATUS_LABELS = {
    "open": "Interrupted",
    "recovery_required": "Recovery required",
    "recovered": "Recovered",
    "rolled_back": "Rolled back",
    "cancelled": "Cancelled",
    "completed": "Completed",
    "failed": "Failed",
}


def scan_transactions(
    project_root: str | Path,
    *,
    home_root: str | Path | None = None,
) -> list[dict]:
    """Every durable transaction record that belongs to *project_root*.

    Records are written under ``<profile>/transactions`` for the project key
    the writing task used, so a project is matched by the ``project_root`` it
    recorded rather than by guessing a profile id. Pending records come first;
    resolved ones are kept so a caller can distinguish an interruption that was
    already handled from one that is still open. A record that cannot be parsed
    is reported as unreadable instead of being dropped.
    """
    if not str(project_root or "").strip():
        return []
    try:
        root = Path(project_root).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return []
    home = Path(home_root).expanduser().resolve() if home_root else giscode_home()
    projects = home / "projects"
    if not projects.is_dir():
        return []
    wanted = _norm(root)
    records: list[dict] = []
    try:
        profiles = sorted(projects.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return []
    for profile in profiles:
        transactions = profile / _DEFAULT_TRANSACTION_DIR
        if not transactions.is_dir():
            continue
        try:
            paths = sorted(
                transactions.glob("*.json"), key=lambda item: item.name.lower()
            )
        except OSError:
            continue
        for path in paths:
            task = _safe_task(path.stem) or path.stem
            record = {
                "task": task,
                "profile_root": str(profile),
                "path": str(path),
                "project_root": "",
                "status": "unreadable",
                "label": "Unresolved — metadata unreadable",
                "pending": False,
                "unattributed": True,
                "files": [],
                "updated_at": "",
                "problem": "",
            }
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                record["problem"] = (
                    f"the transaction record {path.name} could not be read "
                    f"({type(exc).__name__}); it needs manual attention"
                )
                records.append(record)
                continue
            if not isinstance(payload, dict):
                record["problem"] = (
                    f"the transaction record {path.name} is not in a known format"
                )
                records.append(record)
                continue
            status = str(payload.get("status", ""))
            recorded_root = _norm(payload.get("project_root", ""))
            if payload.get("format") != _TRANSACTION_FORMAT:
                # A damaged record cannot be attributed safely. Report it only
                # when it claims to be unfinished, so a resolved record from a
                # newer format is not turned into a warning.
                if status not in PENDING_TRANSACTION_STATUSES or recorded_root != wanted:
                    continue
                record["project_root"] = str(payload.get("project_root", ""))
                record["unattributed"] = False
                record["problem"] = (
                    f"the transaction record {path.name} is not in a known format"
                )
                records.append(record)
                continue
            if recorded_root != wanted:
                continue
            files = [
                {
                    "path": str(item.get("path", "")),
                    "created": bool(item.get("created")),
                }
                for item in (payload.get("files") or [])
                if isinstance(item, dict) and str(item.get("path", "")).strip()
            ]
            record.update(
                {
                    "project_root": str(payload.get("project_root", "")),
                    "status": status or "unknown",
                    "label": _STATUS_LABELS.get(status, "Unresolved"),
                    "pending": status in PENDING_TRANSACTION_STATUSES,
                    "unattributed": False,
                    "files": files,
                    "updated_at": str(payload.get("updated_at", "")),
                    "problem": "",
                }
            )
            records.append(record)
    # Newest first inside each group, pending records before resolved ones.
    records.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
    records.sort(key=lambda item: 0 if item["pending"] else 1)
    return records


def _pending_files(payload: dict) -> list[str]:
    files = payload.get("files")
    if not isinstance(files, list):
        return []
    return [str(item.get("path", "")) for item in files if isinstance(item, dict)]


def recover_pending_transactions(
    project_root: str | Path,
    *,
    profile_root: str | Path | None = None,
    approve: Callable[[str, list[str]], bool] | None = None,
    dry_run: bool = False,
    tasks: Iterable[str] | None = None,
) -> list[PendingRecovery]:
    """Roll back transactions left ``open`` by a terminated process.

    The caller must pass an approval callback for a real recovery. A dry run
    reports what would be touched without changing a file.
    """
    try:
        root = Path(project_root).expanduser().resolve()
    except (OSError, TypeError, ValueError):
        return []
    if not root.is_dir():
        return []
    profile = _profile(profile_root)
    selected = {str(item) for item in tasks} if tasks is not None else None
    results: list[PendingRecovery] = []
    found, problems = _scan_transaction_records(profile_root=profile, project_root=root)
    if selected is None:
        # Unreadable records cannot be attributed or targeted, so they are
        # reported on a whole-profile pass and skipped when the caller asked
        # for specific tasks.
        for task, reason in problems:
            results.append(PendingRecovery(task, "", "recovery_required", blocked=reason))
    for payload in found:
        task = str(payload["task"])
        if selected is not None and task not in selected:
            continue
        files = _pending_files(payload)
        if dry_run:
            results.append(
                PendingRecovery(task, str(root), "would_recover")
            )
            continue
        if not callable(approve):
            results.append(
                PendingRecovery(
                    task,
                    str(root),
                    "recovery_required",
                    blocked="explicit approval is required before restart recovery",
                )
            )
            continue
        try:
            accepted = bool(approve(task, files))
        except Exception:  # noqa: BLE001 - a throwing host is a rejection
            accepted = False
        if not accepted:
            results.append(
                PendingRecovery(
                    task,
                    str(root),
                    "recovery_required",
                    blocked="approval was declined",
                )
            )
            continue

        snapshot = SnapshotManager(task, profile_root=profile)
        snapshot.bind_project(root)
        try:
            outcome = rollback_task_edits(snapshot, root)
        except Exception as exc:  # noqa: BLE001 - report, never claim success
            outcome = RollbackOutcome(blocked=f"{type(exc).__name__}: {exc}")
        status = (
            "recovered"
            if outcome.status in {"rolled_back", "nothing_to_do"} and not outcome.conflicts
            else "recovery_required"
        )
        result = PendingRecovery(task, str(root), status, outcome=outcome)
        # Only a resolved transaction is closed. A conflict keeps the record
        # pending and distinct from a fresh interruption, so the GUI can say
        # "Recovery required" and repairing the missing piece can finish the
        # rollback on the next attempt.
        snapshot.mark_closed(
            "recovered" if status == "recovered" else "recovery_required",
            result=outcome.as_dict(),
        )
        results.append(result)
    return results


def _prompt_approval(label: str, diff: str) -> bool:
    print(f"\n{label}\n")
    shown = diff if len(diff) <= 6000 else diff[:6000] + "\n... [diff clipped]"
    print(shown)
    try:
        return input("Apply this recovery? [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, OSError):
        return False


def _prompt_task(task: str, files: list[str]) -> bool:
    print(f"\nInterrupted Core task {task} has {len(files)} unverified file(s):")
    for path in files:
        print(f"  {path}")
    try:
        return input("Roll this task back? [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, OSError):
        return False


def _print_pending(results: Iterable[PendingRecovery]) -> None:
    for result in results:
        print(f"{result.task}: {result.status}")
        if result.outcome is not None:
            print(f"  {result.outcome.summary()}")
        if result.blocked:
            print(f"  {result.blocked}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m giscode.agentic.recover_edits",
        description="List or restore Core coding-recovery records without GisAI.",
    )
    parser.add_argument("--profile-root", default="", help="override the GisCode profile")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list discarded edits and interrupted tasks")
    listing.add_argument("--project", default="")

    restore = sub.add_parser("restore", help="re-apply one task's discarded edit version")
    restore.add_argument("--task", required=True)
    restore.add_argument("--project", default="")
    restore.add_argument("--yes", action="store_true", help="explicitly approve the restore")
    restore.add_argument("--dry-run", action="store_true")

    pending = sub.add_parser("recover-pending", help="roll back a crash-interrupted task")
    pending.add_argument("--project", required=True)
    pending.add_argument("--yes", action="store_true", help="explicitly approve the rollback")
    pending.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    profile = Path(args.profile_root).expanduser().resolve() if args.profile_root else None

    if args.command == "list":
        manifests = find_recoveries(profile_root=profile, project_root=args.project or None)
        for manifest in manifests:
            print(
                f"{manifest.task}: {len(manifest.entries)} discarded file(s), "
                f"project={manifest.project_root}"
            )
            for error in manifest.errors:
                print(f"  warning: {error}")
        pending_items, problems = _scan_transaction_records(
            profile_root=profile, project_root=args.project or None
        )
        for item in pending_items:
            print(f"{item.get('task', '')}: interrupted task, project={item.get('project_root', '')}")
        for task, reason in problems:
            print(f"{task}: unreadable transaction record - {reason}")
        if not manifests and not pending_items and not problems:
            print("No recovery records found.")
        return 0

    if args.command == "restore":
        task = _safe_task(args.task)
        if not task:
            print("The task name is not a safe recovery name.", file=sys.stderr)
            return 2
        directory = _recovery_root(profile) / task
        if not directory.is_dir():
            print(f"No recovery record for task {task}.", file=sys.stderr)
            return 2
        approve = (
            (lambda _path, _diff: True)
            if args.yes or args.dry_run
            else _prompt_approval
        )
        outcome = restore_recovery_directory(
            directory,
            project_root=args.project or None,
            approve=approve,
            dry_run=args.dry_run,
            profile_root=profile,
        )
        print(outcome.report())
        if outcome.audit_path:
            print(f"Audit: {outcome.audit_path}")
        if outcome.audit_error:
            print(f"Warning: {outcome.audit_error}", file=sys.stderr)
        return 0 if outcome.success else 1

    approve = (
        (lambda _task, _files: True)
        if args.yes or args.dry_run
        else _prompt_task
    )
    results = recover_pending_transactions(
        args.project,
        profile_root=profile,
        approve=approve,
        dry_run=args.dry_run,
    )
    _print_pending(results)
    if not results:
        print("No interrupted Core transaction was found.")
    return 0 if all(item.status in {"recovered", "would_recover"} for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
