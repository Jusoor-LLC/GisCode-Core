"""Shared approval, snapshot, and overwrite safety for project writes."""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import Callable, Optional

from giscode.agentic.types import ActionResult, ActionType
from giscode.agentic.write_safety import SnapshotManager, denied_path_reason, invalidate_bytecode


LARGE_SHRINK_MIN_LINES = 50
LARGE_SHRINK_RATIO = 0.5


def large_shrink_reason(before: str, after: str) -> str | None:
    """Return a safety error for a suspicious replacement, if any."""
    before_lines = len(before.splitlines())
    after_lines = len(after.splitlines())
    if after_lines == 0:
        return "Refusing to empty an existing file without explicit approval"
    if (
        before_lines > LARGE_SHRINK_MIN_LINES
        and after_lines < before_lines * LARGE_SHRINK_RATIO
    ):
        return (
            "Refusing to replace an existing file with less than half its line count "
            "without explicit approval"
        )
    return None


def unified_diff(path: Path, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(True),
            after.splitlines(True),
            fromfile=str(path),
            tofile=str(path),
        )
    )


class GuardedWriter:
    """Write files with one shared safety policy.

    New files are intentionally auto-approved. Existing files always require
    either an explicit approval callback or an explicitly supplied auto-approve
    policy, which keeps standalone agentic callers from silently overwriting.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        snapshot: SnapshotManager | None = None,
        approve: Optional[Callable[[Path, str], bool]] = None,
        auto_approve_existing: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.snapshot = snapshot
        self.approve = approve
        self.auto_approve_existing = bool(auto_approve_existing)

    def write(self, path: Path, content: str, action_type: ActionType) -> ActionResult:
        target = self._validate(path)
        exists = target.exists()
        if action_type == ActionType.CREATE and exists:
            return ActionResult(
                action_type=action_type,
                target_path=str(target),
                success=False,
                error=f"File already exists: {target}",
            )
        if action_type == ActionType.MODIFY and not exists:
            return ActionResult(
                action_type=action_type,
                target_path=str(target),
                success=False,
                error=f"File does not exist: {target}",
            )

        before = target.read_text(encoding="utf-8", errors="replace") if exists else ""
        if exists:
            if before == content:
                result = ActionResult(
                    action_type=action_type,
                    target_path=str(target),
                    success=False,
                    message="No change: proposed content matches the existing file",
                )
                setattr(result, "original_content", before)
                setattr(result, "no_change", True)
                result.success = True
                return result
            diff = unified_diff(target, before, content)
            shrink = large_shrink_reason(before, content)
            approved = self.auto_approve_existing
            if not approved and callable(self.approve):
                try:
                    approved = bool(self.approve(target, diff))
                except Exception:
                    approved = False
            if not approved:
                if shrink:
                    return ActionResult(
                        action_type=action_type,
                        target_path=str(target),
                        success=False,
                        error=shrink,
                    )
                if not callable(self.approve) and not self.auto_approve_existing:
                    error = "Existing file overwrite requires an approval callback"
                else:
                    error = "Edit rejected by approval callback"
                return ActionResult(
                    action_type=action_type,
                    target_path=str(target),
                    success=False,
                    error=error,
                )
            if self.snapshot is not None:
                self.snapshot.snapshot(target, self.project_root)
        elif self.snapshot is not None:
            self.snapshot.record_created(target)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        invalidate_bytecode(target)
        if self.snapshot is not None:
            # Record the version this task produced, so a later rollback can
            # tell it apart from a save the user made afterwards.
            self.snapshot.record_written(target, self.project_root)
        result = ActionResult(
            action_type=action_type,
            target_path=str(target),
            success=True,
            message=f"{'Created' if not exists else 'Modified'} file: {target}",
        )
        if exists:
            setattr(result, "original_content", before)
        return result

    def _validate(self, path: Path) -> Path:
        target = path if path.is_absolute() else self.project_root / path
        target = target.resolve()
        denied = denied_path_reason(target, self.project_root)
        if denied:
            raise ValueError(denied)
        return target
