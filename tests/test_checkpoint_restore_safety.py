"""The build/test checkpoint may undo only what the attempt still owns.

The old restore deleted the whole tree and re-copied the checkpoint, which
could not distinguish a fix step's output from a save made while it ran. These
tests pin the evidence rule: a file is put back only while it still holds the
version the attempt produced, a file that appeared afterwards is kept, and an
attempt that cannot be undone says so per file.
"""

import sys
import time
from pathlib import Path

import pytest

from giscode.orchestration.checkpoint import CheckpointError, CheckpointManager
from giscode.orchestration.orchestrator import BuildTestOrchestrator
from giscode.orchestration.types import (
    BuildRequest,
    OrchestrationPhase,
    TestRequest,
)
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace
from tests.conftest import requires_gisai

ORIGINAL = "print('original')\n"
FIX = "print('fixed')\n"
USER_SAVE = "print('user save')\n"


def _workspace(tmp_path) -> Workspace:
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "main.py", content=ORIGINAL)
    return workspace


def _debris(root: Path) -> list[str]:
    return sorted(
        path.name for path in root.rglob("*") if ".giscode-" in path.name
    )


def test_restore_without_a_fingerprint_touches_nothing(tmp_path):
    workspace = _workspace(tmp_path)
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("attempt-1")
    (workspace.root_path / "src" / "main.py").write_text(FIX, encoding="utf-8")

    outcome = manager.restore(checkpoint)

    assert outcome.status == "blocked"
    assert outcome.complete is False
    assert "no record of the versions this attempt produced" in outcome.blocked
    assert (workspace.root_path / "src" / "main.py").read_text(
        encoding="utf-8"
    ) == FIX


def test_a_file_that_still_holds_the_produced_version_is_restored(tmp_path):
    workspace = _workspace(tmp_path)
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("attempt-1")
    (workspace.root_path / "src" / "main.py").write_text(FIX, encoding="utf-8")
    produced = manager.capture_produced()

    outcome = manager.restore(checkpoint, produced=produced)

    assert outcome.status == "rolled_back"
    assert outcome.restored == ["src/main.py"]
    assert (workspace.root_path / "src" / "main.py").read_text(
        encoding="utf-8"
    ) == ORIGINAL
    assert _debris(workspace.root_path) == []


def test_a_file_the_user_saved_after_the_fingerprint_is_kept(tmp_path):
    workspace = _workspace(tmp_path)
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("attempt-1")
    (workspace.root_path / "src" / "main.py").write_text(FIX, encoding="utf-8")
    produced = manager.capture_produced()
    (workspace.root_path / "src" / "main.py").write_text(USER_SAVE, encoding="utf-8")

    outcome = manager.restore(checkpoint, produced=produced)

    assert (workspace.root_path / "src" / "main.py").read_text(
        encoding="utf-8"
    ) == USER_SAVE
    assert outcome.restored == []
    assert outcome.status == "blocked"
    assert outcome.conflicts and outcome.conflicts[0][0] == "src/main.py"
    assert "changed after this task wrote it" in outcome.conflicts[0][1]


def test_a_file_the_user_created_is_kept(tmp_path):
    workspace = _workspace(tmp_path)
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("attempt-1")
    produced = manager.capture_produced()
    # Created after the fingerprint, so it cannot be this attempt's output.
    (workspace.root_path / "notes.txt").write_text("mine\n", encoding="utf-8")

    outcome = manager.restore(checkpoint, produced=produced)

    assert (workspace.root_path / "notes.txt").read_text(encoding="utf-8") == "mine\n"
    assert outcome.removed == []
    assert outcome.conflicts == [
        ("notes.txt", "it was created after this attempt, so it was kept")
    ]


def test_a_file_the_attempt_created_is_removed(tmp_path):
    workspace = _workspace(tmp_path)
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("attempt-1")
    (workspace.root_path / "notes.txt").write_text("attempt\n", encoding="utf-8")
    produced = manager.capture_produced()

    outcome = manager.restore(checkpoint, produced=produced)

    assert not (workspace.root_path / "notes.txt").exists()
    assert outcome.removed == ["notes.txt"]
    assert outcome.status == "rolled_back"


def test_a_created_file_the_user_edited_is_kept(tmp_path):
    workspace = _workspace(tmp_path)
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("attempt-1")
    (workspace.root_path / "notes.txt").write_text("attempt\n", encoding="utf-8")
    produced = manager.capture_produced()
    (workspace.root_path / "notes.txt").write_text("user\n", encoding="utf-8")

    outcome = manager.restore(checkpoint, produced=produced)

    assert (workspace.root_path / "notes.txt").read_text(encoding="utf-8") == "user\n"
    assert outcome.removed == []
    assert outcome.status == "blocked"


def test_a_file_the_attempt_deleted_is_put_back(tmp_path):
    workspace = _workspace(tmp_path)
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("attempt-1")
    (workspace.root_path / "src" / "main.py").unlink()
    produced = manager.capture_produced()

    outcome = manager.restore(checkpoint, produced=produced)

    assert (workspace.root_path / "src" / "main.py").read_text(
        encoding="utf-8"
    ) == ORIGINAL
    assert outcome.restored == ["src/main.py"]


def test_a_file_deleted_after_the_fingerprint_stays_deleted(tmp_path):
    workspace = _workspace(tmp_path)
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("attempt-1")
    produced = manager.capture_produced()
    (workspace.root_path / "src" / "main.py").unlink()

    outcome = manager.restore(checkpoint, produced=produced)

    assert not (workspace.root_path / "src" / "main.py").exists()
    assert outcome.restored == []
    assert outcome.conflicts == [
        ("src/main.py", "it was deleted after this attempt, so it was left deleted")
    ]


@requires_gisai
def test_a_failed_restore_is_reported_not_claimed(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("attempt-1")
    (workspace.root_path / "src" / "main.py").write_text(FIX, encoding="utf-8")
    produced = manager.capture_produced()

    import giscode.gisai.safe_rollback as safe_rollback

    monkeypatch.setattr(
        safe_rollback,
        "restore_file",
        lambda target, expected, data: safe_rollback.FileOutcome(
            "conflict", "it could not be restored (OSError)"
        ),
    )

    outcome = manager.restore(checkpoint, produced=produced)

    assert outcome.restored == []
    assert outcome.status == "blocked"
    assert outcome.complete is False
    assert outcome.conflicts[0][1].startswith("it could not be restored")
    assert (workspace.root_path / "src" / "main.py").read_text(
        encoding="utf-8"
    ) == FIX


def test_a_second_restore_changes_nothing_more(tmp_path):
    workspace = _workspace(tmp_path)
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("attempt-1")
    (workspace.root_path / "src" / "main.py").write_text(FIX, encoding="utf-8")
    produced = manager.capture_produced()

    first = manager.restore(checkpoint, produced=produced)
    second = manager.restore(checkpoint, produced=manager.capture_produced())

    assert first.status == "rolled_back"
    assert second.status == "nothing_to_do"
    assert (workspace.root_path / "src" / "main.py").read_text(
        encoding="utf-8"
    ) == ORIGINAL
    assert _debris(workspace.root_path) == []


def test_unrelated_files_and_nested_directories_survive(tmp_path):
    workspace = _workspace(tmp_path)
    workspace.write("docs", "guide.md", content="keep me\n")
    manager = CheckpointManager(workspace)
    checkpoint = manager.create("attempt-1")
    (workspace.root_path / "src" / "main.py").write_text(FIX, encoding="utf-8")
    produced = manager.capture_produced()

    manager.restore(checkpoint, produced=produced)

    assert (workspace.root_path / "docs" / "guide.md").read_text(
        encoding="utf-8"
    ) == "keep me\n"


def test_a_missing_checkpoint_still_raises(tmp_path):
    workspace = _workspace(tmp_path)
    manager = CheckpointManager(workspace)

    with pytest.raises(CheckpointError):
        manager.restore(tmp_path / "ws" / ".giscode" / "checkpoints" / "gone")


class _TimingOutSandbox:
    """First build fails, second applies the fix but outlives the task budget."""

    def __init__(self, fix_path: Path):
        self._fix_path = fix_path
        self.call_count = 0

    def execute(self, request):
        from giscode.sandbox.types import SandboxBackend, SandboxResult, SandboxStatus

        self.call_count += 1
        if self.call_count == 1:
            return SandboxResult(
                sandbox_id="test",
                backend=SandboxBackend.DOCKER,
                command=request.command,
                exit_code=1,
                status=SandboxStatus.FAILED,
                stderr="Build failed",
            )
        time.sleep(0.3)
        self._fix_path.write_text(FIX, encoding="utf-8")
        return SandboxResult(
            sandbox_id="test",
            backend=SandboxBackend.DOCKER,
            command=request.command,
            exit_code=0,
            status=SandboxStatus.COMPLETED,
            stdout="Build passed",
        )

    def create_sandbox(self, *args, **kwargs):
        from giscode.sandbox.types import SandboxBackend, SandboxResult, SandboxStatus

        return SandboxResult(
            sandbox_id="test",
            backend=SandboxBackend.DOCKER,
            command="",
            status=SandboxStatus.RUNNING,
        )

    def get_status(self):
        from giscode.sandbox.types import SandboxStatus

        return SandboxStatus.IDLE

    @property
    def backend(self):
        from giscode.sandbox.types import SandboxBackend

        return SandboxBackend.DOCKER

    @property
    def available(self):
        return True


def test_a_cancelled_attempt_keeps_a_concurrent_user_save(tmp_path):
    workspace = _workspace(tmp_path)
    fix_path = workspace.root_path / "src" / "main.py"
    user_notes = workspace.root_path / "notes.txt"
    sandbox = _TimingOutSandbox(fix_path)
    task_manager = TaskManager()
    orchestrator = BuildTestOrchestrator(
        workspace, sandbox, task_manager=task_manager, max_attempts=2
    )

    manager = orchestrator._checkpoint_manager
    capture = manager.capture_produced

    def capture_then_user_saves():
        produced = capture()
        # Deterministic interleaving: the save lands after the fingerprint was
        # taken and before the restore reads the tree.
        if not user_notes.exists():
            user_notes.write_text("user note\n", encoding="utf-8")
        return produced

    manager.capture_produced = capture_then_user_saves
    task_manager.start_task("cancel-test", max_duration=0.2)

    result = orchestrator.build_and_test(
        "task-1",
        BuildRequest(command="echo", arguments=["build"]),
        TestRequest(command="echo", arguments=["test"]),
    )

    restore = result.failure_info.get("checkpoint_restore") or {}
    assert result.status == OrchestrationPhase.TIMED_OUT
    assert fix_path.read_text(encoding="utf-8") == ORIGINAL
    assert user_notes.read_text(encoding="utf-8") == "user note\n"
    assert restore.get("status") == "partial"
    assert restore.get("restored") == ["src/main.py"]
    assert restore.get("conflicts") == [
        {
            "path": "notes.txt",
            "reason": "it was created after this attempt, so it was kept",
        }
    ]
    assert result.failure_info.get("checkpoint_restore_incomplete") is True
    assert "Not undone" in (result.error or "")
    assert _debris(workspace.root_path) == []
