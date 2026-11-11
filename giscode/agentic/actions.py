import ast
from pathlib import Path
from typing import Optional

from giscode.agentic.types import ActionType, ActionResult, ExecutionStep
from giscode.agentic.guarded_writer import GuardedWriter
from giscode.workspace.workspace import Workspace
from giscode.agentic.write_safety import (
    SnapshotManager,
    atomic_write_text,
    denied_path_reason,
)


class ActionError(Exception):
    pass


class PathValidationError(ActionError):
    pass


class WorkspaceActionExecutor:
    def __init__(self, workspace: Workspace, snapshot_manager: SnapshotManager | None = None):
        self._workspace = workspace
        self._snapshot_manager = snapshot_manager
        self._approval_callback = None
        self._auto_approve_existing = False
        self._guarded_writes = False

    def configure_write_safety(
        self,
        snapshot_manager: SnapshotManager | None,
        approval_callback=None,
        *,
        auto_approve_existing: bool = False,
    ) -> None:
        self._snapshot_manager = snapshot_manager
        self._approval_callback = approval_callback
        self._auto_approve_existing = bool(auto_approve_existing)
        self._guarded_writes = True

    def _guarded_writer(self) -> GuardedWriter:
        return GuardedWriter(
            self._workspace.root_path,
            snapshot=self._snapshot_manager,
            approve=self._approval_callback,
            auto_approve_existing=self._auto_approve_existing,
        )

    def _validate_path(self, target_path: str) -> Path:
        path = Path(target_path)
        if path.is_absolute():
            resolved = path.resolve()
        else:
            resolved = (self._workspace.root_path / path).resolve()

        denied = denied_path_reason(resolved, self._workspace.root_path)
        if denied:
            raise PathValidationError(denied)
        try:
            resolved.relative_to(self._workspace.root_path)
        except ValueError:
            raise PathValidationError(f"Path escapes workspace: {target_path}")

        if ".." in Path(target_path).parts:
            raise PathValidationError(f"Path traversal detected: {target_path}")

        return resolved

    def execute(self, step: ExecutionStep) -> ActionResult:
        try:
            resolved = self._validate_path(step.target_path)
            if step.action_type == ActionType.CREATE:
                return self._create(resolved, step)
            elif step.action_type == ActionType.MODIFY:
                return self._modify(resolved, step)
            elif step.action_type == ActionType.READ:
                return self._read(resolved, step)
            else:
                return ActionResult(
                    action_type=step.action_type,
                    target_path=step.target_path,
                    success=False,
                    error=f"Unsupported action type: {step.action_type}",
                )
        except PathValidationError as e:
            return ActionResult(
                action_type=step.action_type,
                target_path=step.target_path,
                success=False,
                error=str(e),
            )
        except Exception as e:
            return ActionResult(
                action_type=step.action_type,
                target_path=step.target_path,
                success=False,
                error=str(e),
            )

    def _create(self, path: Path, step: ExecutionStep) -> ActionResult:
        if self._guarded_writes:
            return self._guarded_writer().write(path, step.content or "", ActionType.CREATE)
        if path.exists():
            return ActionResult(
                action_type=ActionType.CREATE,
                target_path=str(path),
                success=False,
                error=f"File already exists: {path}",
            )
        content = step.content or ""
        if self._snapshot_manager:
            self._snapshot_manager.record_created(path, self._workspace.root_path)
        try:
            atomic_write_text(path, content, create_only=True)
        except FileExistsError:
            return ActionResult(
                action_type=ActionType.CREATE,
                target_path=str(path),
                success=False,
                error=f"File already exists: {path}",
            )
        except OSError as exc:
            return ActionResult(
                action_type=ActionType.CREATE,
                target_path=str(path),
                success=False,
                error=f"File could not be written atomically: {exc}",
            )
        if self._snapshot_manager:
            self._snapshot_manager.record_written(path, self._workspace.root_path)
        return ActionResult(
            action_type=ActionType.CREATE,
            target_path=str(path),
            success=True,
            message=f"Created file: {path}",
        )

    def _modify(self, path: Path, step: ExecutionStep) -> ActionResult:
        if self._guarded_writes:
            return self._guarded_writer().write(path, step.content or "", ActionType.MODIFY)
        if not path.exists():
            return ActionResult(
                action_type=ActionType.MODIFY,
                target_path=str(path),
                success=False,
                error=f"File does not exist: {path}",
            )
        content = step.content or ""
        if self._snapshot_manager:
            self._snapshot_manager.snapshot(path, self._workspace.root_path)
        try:
            atomic_write_text(path, content)
        except OSError as exc:
            return ActionResult(
                action_type=ActionType.MODIFY,
                target_path=str(path),
                success=False,
                error=f"File could not be written atomically: {exc}",
            )
        if self._snapshot_manager:
            self._snapshot_manager.record_written(path, self._workspace.root_path)
        return ActionResult(
            action_type=ActionType.MODIFY,
            target_path=str(path),
            success=True,
            message=f"Modified file: {path}",
        )

    def _read(self, path: Path, step: ExecutionStep) -> ActionResult:
        if not path.exists():
            return ActionResult(
                action_type=ActionType.READ,
                target_path=str(path),
                success=False,
                error=f"File does not exist: {path}",
            )
        if path.is_dir():
            content = "\n".join(sorted(item.name for item in path.iterdir()))
        else:
            content = path.read_text(encoding="utf-8")
        return ActionResult(
            action_type=ActionType.READ,
            target_path=str(path),
            success=True,
            message=f"Read file: {path}",
        )


class MockWorkspaceActionExecutor:
    def __init__(self, workspace: Optional[Workspace] = None):
        self._workspace = workspace
        self._fs: dict[str, str] = {}

    def execute(self, step: ExecutionStep) -> ActionResult:
        if step.action_type == ActionType.CREATE:
            if step.target_path in self._fs:
                return ActionResult(
                    action_type=ActionType.CREATE,
                    target_path=step.target_path,
                    success=False,
                    error=f"File already exists: {step.target_path}",
                )
            self._fs[step.target_path] = step.content or ""
            return ActionResult(
                action_type=ActionType.CREATE,
                target_path=step.target_path,
                success=True,
                message=f"Created file: {step.target_path}",
            )
        elif step.action_type == ActionType.MODIFY:
            if step.target_path not in self._fs:
                return ActionResult(
                    action_type=ActionType.MODIFY,
                    target_path=step.target_path,
                    success=False,
                    error=f"File does not exist: {step.target_path}",
                )
            self._fs[step.target_path] = step.content or ""
            return ActionResult(
                action_type=ActionType.MODIFY,
                target_path=step.target_path,
                success=True,
                message=f"Modified file: {step.target_path}",
            )
        elif step.action_type == ActionType.READ:
            if step.target_path not in self._fs:
                return ActionResult(
                    action_type=ActionType.READ,
                    target_path=step.target_path,
                    success=False,
                    error=f"File does not exist: {step.target_path}",
                )
            return ActionResult(
                action_type=ActionType.READ,
                target_path=step.target_path,
                success=True,
                message=f"Read file: {step.target_path}",
            )
        else:
            return ActionResult(
                action_type=step.action_type,
                target_path=step.target_path,
                success=False,
                error=f"Unsupported action type: {step.action_type}",
            )

    def file_exists(self, path: str) -> bool:
        return path in self._fs

    def get_content(self, path: str) -> Optional[str]:
        return self._fs.get(path)
