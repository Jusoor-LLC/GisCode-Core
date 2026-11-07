"""Whole-project checkpoints for the build/test repair loop.

A checkpoint is a copy of every project file taken before a fix attempt. The
restore is *not* a whole-tree replacement: a build step is an arbitrary
command, so the checkpoint alone cannot tell what the command wrote from a save
the user made while it ran. Restoring from the baseline copy alone would delete
files the user created and overwrite files they saved.

The restore therefore requires a *produced fingerprint* - the digests the tree
held immediately after the attempt finished - and then behaves like every other
rollback in this codebase: a file is put back only while it still holds the
version this attempt produced, a file that appeared afterwards is kept, and
anything ambiguous is reported instead of touched. With no fingerprint the
restore fails closed and names that as the blocker.

The actual per-file work is `giscode.agentic.rollback`, imported lazily so
this module keeps its old, light import surface.
"""

import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from giscode.workspace.workspace import Workspace


class CheckpointError(Exception):
    pass



def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CheckpointManager:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace
        self._checkpoints_dir = workspace.root_path / ".giscode" / "checkpoints"
        self._checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self._active_checkpoint: Optional[Path] = None

    def create(self, label: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        checkpoint_name = f"{label}_{timestamp}"
        checkpoint_path = self._checkpoints_dir / checkpoint_name

        if checkpoint_path.exists():
            shutil.rmtree(checkpoint_path)

        checkpoint_path.mkdir(parents=True, exist_ok=True)

        for item in self._workspace.root_path.iterdir():
            if item.name == ".giscode":
                continue
            if item.is_dir():
                shutil.copytree(item, checkpoint_path / item.name)
            else:
                shutil.copy2(item, checkpoint_path / item.name)

        self._active_checkpoint = checkpoint_path
        return checkpoint_path

    # -- evidence ---------------------------------------------------------

    def _project_files(self, root: Path) -> dict[str, Path]:
        """Every project file by relative POSIX path, minus scratch dirs.

        The same walk is used for the checkpoint copy and the live tree, so the
        two sides are always comparable key for key.
        """
        from giscode.agentic.write_safety import EPHEMERAL_DIRECTORY_NAMES

        files: dict[str, Path] = {}
        if not root.is_dir():
            return files
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name.lower() not in EPHEMERAL_DIRECTORY_NAMES
            )
            for name in sorted(filenames):
                path = Path(dirpath) / name
                try:
                    files[path.relative_to(root).as_posix()] = path
                except ValueError:
                    continue
        return files

    def capture_produced(self) -> dict[str, str]:
        """Fingerprint the tree now: the versions this attempt left behind.

        Called immediately before a restore, so this is the attempt's output.
        A save that lands after this call is therefore detectable, which is the
        entire point of taking it.
        """
        produced: dict[str, str] = {}
        for relative, path in self._project_files(self._workspace.root_path).items():
            try:
                produced[relative] = _digest(path)
            except OSError:
                continue
        return produced

    # -- restore ----------------------------------------------------------

    def restore(
        self,
        checkpoint_path: Optional[Path] = None,
        produced: Optional[dict[str, str]] = None,
    ) -> Any:
        """Undo this attempt's edits from *checkpoint_path*.

        Returns a ``rollback.RollbackOutcome``: per-file account of what
        was restored, removed, or deliberately left alone. ``produced`` is the
        evidence that a file still holds the attempt's own output; without it
        nothing is touched (``status == "blocked"``).
        """
        if checkpoint_path is None:
            checkpoint_path = self._active_checkpoint

        if checkpoint_path is None or not checkpoint_path.exists():
            raise CheckpointError("No checkpoint available for restore")

        from giscode.agentic.rollback import (
            REASON_CHANGED_AFTER,
            RollbackOutcome,
            create_file,
            remove_file,
            restore_file,
        )

        outcome = RollbackOutcome()
        if produced is None:
            outcome.blocked = (
                "no record of the versions this attempt produced, so nothing "
                "was restored or deleted"
            )
            return outcome

        root = self._workspace.root_path
        before = self._project_files(Path(checkpoint_path))
        after = self._project_files(root)

        for relative in sorted(set(before) | set(after), key=str.lower):
            saved = before.get(relative)
            target = after.get(relative)
            recorded = produced.get(relative)

            current: Optional[str] = None
            if target is not None:
                try:
                    current = _digest(target)
                except OSError:
                    outcome.conflicts.append((relative, "it cannot be read"))
                    continue

            if saved is None:
                # The file did not exist when the checkpoint was taken, so the
                # attempt created it - and only an unchanged copy is removed.
                if target is None:
                    continue
                if recorded is None:
                    outcome.conflicts.append(
                        (relative, "it was created after this attempt, so it was kept")
                    )
                elif recorded != current:
                    outcome.conflicts.append((relative, REASON_CHANGED_AFTER))
                else:
                    result = remove_file(target, recorded)
                    if result.status == "removed":
                        outcome.removed.append(relative)
                    else:
                        outcome.conflicts.append((relative, result.reason))
                continue

            try:
                baseline = saved.read_bytes()
            except OSError:
                outcome.conflicts.append(
                    (relative, "its checkpoint copy could not be read")
                )
                continue

            if target is None:
                # Existed at checkpoint time and is gone now. Only a file that
                # was already absent at the fingerprint can be the attempt's
                # own deletion; one that was still there afterwards was deleted
                # later, by someone else.
                if recorded is None:
                    result = create_file(root / relative, baseline)
                    if result.status == "restored":
                        outcome.restored.append(relative)
                    else:
                        outcome.conflicts.append((relative, result.reason))
                else:
                    outcome.conflicts.append(
                        (
                            relative,
                            "it was deleted after this attempt, so it was left deleted",
                        )
                    )
                continue

            if current == hashlib.sha256(baseline).hexdigest():
                continue  # untouched since the checkpoint: nothing to undo

            if recorded is None:
                outcome.conflicts.append(
                    (relative, "no record of the version this attempt produced")
                )
            elif recorded != current:
                outcome.conflicts.append((relative, REASON_CHANGED_AFTER))
            else:
                result = restore_file(target, recorded, baseline)
                if result.status == "restored":
                    outcome.restored.append(relative)
                else:
                    outcome.conflicts.append((relative, result.reason))

        return outcome

    def cleanup(self, checkpoint_path: Optional[Path] = None) -> None:
        if checkpoint_path is None:
            checkpoint_path = self._active_checkpoint

        if checkpoint_path is not None and checkpoint_path.exists():
            shutil.rmtree(checkpoint_path)
            if self._active_checkpoint == checkpoint_path:
                self._active_checkpoint = None

    @property
    def active_checkpoint(self) -> Optional[Path]:
        return self._active_checkpoint
