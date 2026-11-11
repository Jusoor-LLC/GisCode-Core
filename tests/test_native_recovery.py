"""Native Core recovery: snapshots, transaction restart, and re-apply safety."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from giscode.agentic import recover_edits, recovery_store
from giscode.agentic.guarded_writer import GuardedWriter
from giscode.agentic.rollback import (
    _quarantine,
    recover_interrupted_rollbacks,
    rollback_task_edits,
)
from giscode.agentic.types import ActionType
from giscode.agentic.write_safety import SnapshotManager, atomic_write_text

REPO_ROOT = Path(__file__).resolve().parents[1]

_BLOCK = "\n".join(
    [
        "import importlib.abc, sys",
        "class _Block(importlib.abc.MetaPathFinder):",
        "    def find_spec(self, name, path=None, target=None):",
        '        if name == "giscode.gisai" or name.startswith("giscode.gisai."):',
        '            raise ModuleNotFoundError("blocked: " + name, name=name)',
        "        return None",
        "sys.meta_path.insert(0, _Block())",
        "",
    ]
)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _profile(tmp_path: Path) -> Path:
    return tmp_path / "profile"


def _snapshot(tmp_path: Path, task: str = "task-1") -> SnapshotManager:
    return SnapshotManager(task, profile_root=_profile(tmp_path))


def _modify(snapshot: SnapshotManager, target: Path, project: Path, content: str) -> None:
    snapshot.snapshot(target, project)
    atomic_write_text(target, content)
    snapshot.record_written(target, project)


def _create(snapshot: SnapshotManager, target: Path, project: Path, content: str) -> None:
    snapshot.record_created(target, project)
    atomic_write_text(target, content, create_only=True)
    snapshot.record_written(target, project)


def _failed_task(tmp_path: Path) -> tuple[Path, Path, SnapshotManager]:
    project = _project(tmp_path)
    target = project / "calc.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path)
    _modify(snapshot, target, project, "VALUE = 2\n")
    outcome = rollback_task_edits(snapshot, project)
    assert outcome.status == "rolled_back"
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    return project, target, snapshot


def test_single_file_rollback_and_native_reapply_is_idempotent(tmp_path):
    project, target, snapshot = _failed_task(tmp_path)
    recovery_dir = recovery_store.directory_for_snapshot(snapshot)
    assert recovery_dir is not None
    assert (recovery_dir / "manifest.json").is_file()
    assert (recovery_dir / "files" / "calc.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    restored = recover_edits.restore_recovery_directory(
        recovery_dir,
        project_root=project,
        approve=lambda _path, _diff: True,
        profile_root=_profile(tmp_path),
    )
    assert restored.success
    assert restored.restored == ["calc.py"]
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert restored.audit_path and Path(restored.audit_path).is_file()

    second = recover_edits.restore_recovery_directory(
        recovery_dir,
        project_root=project,
        approve=lambda _path, _diff: True,
        profile_root=_profile(tmp_path),
    )
    assert second.success
    assert second.skipped == ["calc.py"]
    assert second.restored == []
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"


def test_multi_file_rollback_and_reapply_restore_both_files(tmp_path):
    project = _project(tmp_path)
    first = project / "a.py"
    second = project / "b.py"
    first.write_text("A = 1\n", encoding="utf-8")
    second.write_text("B = 1\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path)
    _modify(snapshot, first, project, "A = 2\n")
    _modify(snapshot, second, project, "B = 2\n")

    outcome = rollback_task_edits(snapshot, project)
    assert outcome.status == "rolled_back"
    assert set(outcome.restored) == {"a.py", "b.py"}
    assert first.read_text(encoding="utf-8") == "A = 1\n"
    assert second.read_text(encoding="utf-8") == "B = 1\n"

    recovery_dir = recovery_store.directory_for_snapshot(snapshot)
    restored = recover_edits.restore_recovery_directory(
        recovery_dir,
        project_root=project,
        approve=lambda _path, _diff: True,
        profile_root=_profile(tmp_path),
    )
    assert restored.success
    assert set(restored.restored) == {"a.py", "b.py"}
    assert first.read_text(encoding="utf-8") == "A = 2\n"
    assert second.read_text(encoding="utf-8") == "B = 2\n"


def test_partial_write_is_recovered_from_quarantine(tmp_path):
    project = _project(tmp_path)
    target = project / "keep.py"
    target.write_text("TASK = 2\n", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    sidecar, problem = _quarantine(target, operation="restore", digest=digest)
    assert problem == ""
    assert sidecar is not None and not target.exists()

    actions = recover_interrupted_rollbacks(project)
    assert any(action.status == "recovered" for action in actions)
    assert target.read_text(encoding="utf-8") == "TASK = 2\n"


def test_restart_recovers_an_open_transaction(tmp_path):
    project = _project(tmp_path)
    target = project / "main.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path)
    _modify(snapshot, target, project, "VALUE = 2\n")

    pending = recover_edits.pending_transactions(
        profile_root=_profile(tmp_path), project_root=project
    )
    assert len(pending) == 1
    assert pending[0]["task"] == "task-1"

    results = recover_edits.recover_pending_transactions(
        project,
        profile_root=_profile(tmp_path),
        approve=lambda _task, _files: True,
    )
    assert len(results) == 1
    assert results[0].status == "recovered"
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    transaction = json.loads(
        (snapshot.transaction_path).read_text(encoding="utf-8")
    )
    assert transaction["status"] == "recovered"


def test_cancelled_transaction_is_preserved_until_explicitly_recovered(tmp_path):
    project = _project(tmp_path)
    target = project / "cancel.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path)
    _modify(snapshot, target, project, "VALUE = 2\n")
    snapshot.mark_closed("cancelled")

    assert recover_edits.pending_transactions(
        profile_root=_profile(tmp_path), project_root=project
    ) == []
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"

    outcome = rollback_task_edits(snapshot, project)
    assert outcome.status == "rolled_back"
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_repeated_rollback_changes_nothing(tmp_path):
    project, target, snapshot = _failed_task(tmp_path)
    before = target.read_text(encoding="utf-8")
    second = rollback_task_edits(snapshot, project)
    assert second.status == "nothing_to_do"
    assert second.touched is False
    assert target.read_text(encoding="utf-8") == before


def test_corrupt_manifest_blocks_recovery(tmp_path):
    project, target, snapshot = _failed_task(tmp_path)
    recovery_dir = recovery_store.directory_for_snapshot(snapshot)
    manifest = recovery_dir / "manifest.json"
    manifest.write_text("{not json", encoding="utf-8")
    result = recover_edits.restore_recovery_directory(
        recovery_dir,
        project_root=project,
        approve=lambda _path, _diff: True,
        profile_root=_profile(tmp_path),
    )
    assert not result.success
    assert result.blocked
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_missing_recovery_blob_or_baseline_fails_closed(tmp_path):
    project, target, snapshot = _failed_task(tmp_path)
    recovery_dir = recovery_store.directory_for_snapshot(snapshot)
    (recovery_dir / "files" / "calc.py").unlink()
    result = recover_edits.restore_recovery_directory(
        recovery_dir,
        project_root=project,
        approve=lambda _path, _diff: True,
        profile_root=_profile(tmp_path),
    )
    assert not result.success
    assert result.conflicts

    # A fresh failed task with its baseline removed cannot be restored either.
    project2 = tmp_path / "project2"
    project2.mkdir()
    target2 = project2 / "calc.py"
    target2.write_text("VALUE = 1\n", encoding="utf-8")
    snapshot2 = SnapshotManager("task-2", profile_root=_profile(tmp_path))
    _modify(snapshot2, target2, project2, "VALUE = 2\n")
    rollback_task_edits(snapshot2, project2)
    recovery2 = recovery_store.directory_for_snapshot(snapshot2)
    (snapshot2.root / "calc.py").unlink()
    result2 = recover_edits.restore_recovery_directory(
        recovery2,
        project_root=project2,
        approve=lambda _path, _diff: True,
        profile_root=_profile(tmp_path),
    )
    assert not result2.success
    assert any("pre-edit" in reason for _, reason in result2.conflicts)


def test_unexpected_external_modification_is_never_overwritten(tmp_path):
    project = _project(tmp_path)
    target = project / "user.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path)
    _modify(snapshot, target, project, "VALUE = 2\n")
    target.write_text("VALUE = 99\n", encoding="utf-8")

    outcome = rollback_task_edits(snapshot, project)
    assert outcome.restored == []
    assert target.read_text(encoding="utf-8") == "VALUE = 99\n"
    assert outcome.conflicts


def test_atomic_writer_leaves_no_temporary_file(tmp_path):
    project = _project(tmp_path)
    target = project / "app.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path)
    writer = GuardedWriter(project, snapshot=snapshot, approve=lambda _path, _diff: True)
    result = writer.write(target, "VALUE = 2\n", ActionType.MODIFY)
    assert result.success
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert list(project.glob("*.giscode-*")) == []
    assert list(project.glob(".*.tmp")) == []


def test_native_recovery_works_with_gisai_blocked(tmp_path):
    script = "\n".join(
        [
            "import hashlib, os, pathlib, sys",
            "from giscode.agentic.recover_edits import restore_recovery_directory",
            "from giscode.agentic.rollback import rollback_task_edits",
            "from giscode.agentic.write_safety import SnapshotManager, atomic_write_text",
            'home = pathlib.Path(os.environ["RECOVERY_HOME"])',
            'project = home / "project"',
            "project.mkdir(parents=True, exist_ok=True)",
            'target = project / "calc.py"',
            'target.write_text("VALUE = 1\\\\n", encoding="utf-8")',
            'snapshot = SnapshotManager("task-1", profile_root=home / "profile")',
            "snapshot.snapshot(target, project)",
            'atomic_write_text(target, "VALUE = 2\\\\n")',
            "snapshot.record_written(target, project)",
            "rollback_task_edits(snapshot, project)",
            "from giscode.agentic.recovery_store import directory_for_snapshot",
            "outcome = restore_recovery_directory(directory_for_snapshot(snapshot), project_root=project, approve=lambda _p, _d: True, profile_root=home / 'profile')",
            'print("STATUS", outcome.success)',
            'print("CONTENT", target.read_text(encoding="utf-8").strip())',
            'print("GISAI_MODULES", [m for m in sys.modules if m.startswith("giscode.gisai")])',
            "",
        ]
    )
    env = dict(os.environ)
    env.update(
        {
            "GISCODE_HOME": str(tmp_path / "home"),
            "GISCODE_TEST_MODE": "1",
            "RECOVERY_HOME": str(tmp_path),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", _BLOCK + script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert "STATUS True" in result.stdout
    assert "CONTENT VALUE = 2" in result.stdout
    assert "GISAI_MODULES []" in result.stdout


def test_cli_lists_and_restores_with_explicit_approval(tmp_path, capsys):
    project, target, _snapshot = _failed_task(tmp_path)
    code = recover_edits.main(
        [
            "--profile-root",
            str(_profile(tmp_path)),
            "list",
            "--project",
            str(project),
        ]
    )
    assert code == 0
    assert "task-1" in capsys.readouterr().out

    code = recover_edits.main(
        [
            "--profile-root",
            str(_profile(tmp_path)),
            "restore",
            "--task",
            "task-1",
            "--project",
            str(project),
            "--yes",
        ]
    )
    assert code == 0
    assert "Restored calc.py" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"


def test_missing_baseline_is_reported_instead_of_silently_skipped(tmp_path):
    project = _project(tmp_path)
    target = project / "calc.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path)
    _modify(snapshot, target, project, "VALUE = 2\n")
    (snapshot.root / "calc.py").unlink()

    outcome = rollback_task_edits(snapshot, project)
    assert outcome.status != "rolled_back"
    assert outcome.restored == []
    assert any(path == "calc.py" for path, _reason in outcome.conflicts)
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"


def test_failed_recovery_stays_pending_and_can_be_retried(tmp_path):
    project = _project(tmp_path)
    target = project / "calc.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path)
    _modify(snapshot, target, project, "VALUE = 2\n")
    saved = (snapshot.root / "calc.py").read_bytes()
    (snapshot.root / "calc.py").unlink()

    first = recover_edits.recover_pending_transactions(
        project, profile_root=_profile(tmp_path), approve=lambda _task, _files: True
    )
    assert [item.status for item in first] == ["recovery_required"]
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    record = json.loads(snapshot.transaction_path.read_text(encoding="utf-8"))
    assert record["status"] == "recovery_required"

    # The same record must survive a restart and still be discoverable; a
    # second attempt is what finishes the rollback once the baseline is back.
    snapshot.transaction_path.write_text(json.dumps(record), encoding="utf-8")
    (snapshot.root / "calc.py").write_bytes(saved)

    second = recover_edits.recover_pending_transactions(
        project, profile_root=_profile(tmp_path), approve=lambda _task, _files: True
    )
    assert [item.status for item in second] == ["recovered"]
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_unreadable_transaction_record_requires_recovery(tmp_path):
    project = _project(tmp_path)
    snapshot = _snapshot(tmp_path)
    snapshot.bind_project(project)
    snapshot.transaction_path.write_text("{not json", encoding="utf-8")

    results = recover_edits.recover_pending_transactions(
        project, profile_root=_profile(tmp_path), approve=lambda _task, _files: True
    )
    assert [item.status for item in results] == ["recovery_required"]
    assert results[0].blocked
    assert snapshot.transaction_path.read_text(encoding="utf-8") == "{not json"
