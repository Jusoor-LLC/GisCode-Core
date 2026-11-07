"""Core rollback works with the whole GisAI package absent.

Rollback now lives in ``giscode.agentic``. Each runtime test blocks every
``giscode.gisai`` import in a fresh interpreter and then performs a real
restore: task-level rollback, checkpoint restore, the command-change gate, and
interrupted-quarantine recovery. These replace the 001G assertions that
expected rollback to be reported as unavailable, because it is no longer
optional.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tests.conftest import requires_gisai

REPO_ROOT = Path(__file__).resolve().parents[1]

_BLOCK = "\n".join([
    "import os, sys, importlib.abc",
    "class _Block(importlib.abc.MetaPathFinder):",
    "    def find_spec(self, name, path=None, target=None):",
    '        if name == "giscode.gisai" or name.startswith("giscode.gisai."):',
    '            raise ModuleNotFoundError("blocked: " + name, name=name)',
    "        return None",
    "sys.meta_path.insert(0, _Block())",
    "",
])


def _run(script: str, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GISCODE_TEST_MODE"] = "1"
    env["GISCODE_HOME"] = str(home / "home")
    env["PROBE_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", _BLOCK + script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=280,
    )


def test_task_rollback_restores_a_real_file_without_gisai(tmp_path):
    script = "\n".join([
        "import pathlib, sys",
        "from giscode.agentic.rollback import rollback_task_edits",
        "from giscode.agentic.write_safety import SnapshotManager",
        'home = pathlib.Path(os.environ["PROBE_HOME"])',
        'project = home / "proj"',
        "project.mkdir(parents=True, exist_ok=True)",
        'target = project / "calc.py"',
        'target.write_text("VALUE = 1\\n", encoding="utf-8")',
        'snapshot = SnapshotManager("task-1", profile_root=home / "profile")',
        "snapshot.snapshot(target, project)",
        'target.write_text("VALUE = 2\\n", encoding="utf-8")',
        "snapshot.record_written(target, project)",
        "outcome = rollback_task_edits(snapshot, project)",
        'print("STATUS", outcome.status)',
        'print("RESTORED", outcome.restored)',
        'print("CONTENT", target.read_text(encoding="utf-8").strip())',
        'print("GISAI_MODULES", [m for m in sys.modules if m.startswith("giscode.gisai")])',
        "",
    ])
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "STATUS rolled_back" in result.stdout
    assert "RESTORED ['calc.py']" in result.stdout
    assert "CONTENT VALUE = 1" in result.stdout
    assert "GISAI_MODULES []" in result.stdout


def test_checkpoint_restore_performs_real_rollback_without_gisai(tmp_path):
    script = "\n".join([
        "import pathlib",
        "from giscode.orchestration.checkpoint import CheckpointManager",
        "from giscode.workspace.workspace import Workspace",
        'home = pathlib.Path(os.environ["PROBE_HOME"])',
        'ws = Workspace(home / "ws")',
        'ws.write("src", "main.py", content="print(\'original\')\\n")',
        'manager = CheckpointManager(ws)',
        'checkpoint = manager.create("attempt-1")',
        '(ws.root_path / "src" / "main.py").write_text("print(\'fixed\')\\n", encoding="utf-8")',
        "produced = manager.capture_produced()",
        "outcome = manager.restore(checkpoint, produced=produced)",
        'print("STATUS", outcome.status)',
        'print("RESTORED", outcome.restored)',
        '(print("CONTENT", (ws.root_path / "src" / "main.py").read_text(encoding="utf-8").strip()))',
        "",
    ])
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "STATUS rolled_back" in result.stdout
    assert "RESTORED ['src/main.py']" in result.stdout
    assert "CONTENT print('original')" in result.stdout


def test_project_file_guard_restores_without_gisai(tmp_path):
    script = "\n".join([
        "import pathlib",
        "from giscode.agentic.write_safety import ProjectFileGuard",
        'home = pathlib.Path(os.environ["PROBE_HOME"])',
        'project = home / "guard"',
        "project.mkdir(parents=True, exist_ok=True)",
        'target = project / "settings.py"',
        'target.write_text("VALUE = 1\\n", encoding="utf-8")',
        "guard = ProjectFileGuard(project)",
        "before = guard.capture()",
        'target.write_text("VALUE = 2\\n", encoding="utf-8")',
        "outcome = guard.gate(guard.detect(before))",
        'print("RESTORED", [change.relative for change in outcome.restored])',
        'print("CONTENT", target.read_text(encoding="utf-8").strip())',
        "",
    ])
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "RESTORED ['settings.py']" in result.stdout
    assert "CONTENT VALUE = 1" in result.stdout


def test_interrupted_quarantine_recovery_works_without_gisai(tmp_path):
    script = "\n".join([
        "import hashlib, pathlib",
        "from giscode.agentic.rollback import _quarantine, recover_interrupted_rollbacks",
        'home = pathlib.Path(os.environ["PROBE_HOME"])',
        'project = home / "recover"',
        "project.mkdir(parents=True, exist_ok=True)",
        'target = project / "keep.py"',
        'target.write_text("TASK = 2\\n", encoding="utf-8")',
        "digest = hashlib.sha256(target.read_bytes()).hexdigest()",
        'sidecar, problem = _quarantine(target, operation="restore", digest=digest)',
        'print("QUARANTINED", sidecar is not None, "GONE", not target.exists())',
        "actions = recover_interrupted_rollbacks(project)",
        'print("ACTIONS", [(action.status, action.operation) for action in actions])',
        'print("CONTENT", target.read_text(encoding="utf-8").strip())',
        "",
    ])
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "QUARANTINED True GONE True" in result.stdout
    assert "('recovered', 'restore')" in result.stdout
    assert "CONTENT TASK = 2" in result.stdout


@requires_gisai
def test_gisai_compatibility_shims_are_the_core_modules():
    import giscode.agentic.recovery_store as core_store
    import giscode.agentic.rollback as core_rollback
    import giscode.gisai.recovery_store as legacy_store
    import giscode.gisai.safe_rollback as legacy_rollback

    assert legacy_rollback is core_rollback
    assert legacy_store is core_store
    assert (
        sys.modules["giscode.gisai.safe_rollback"]
        is sys.modules["giscode.agentic.rollback"]
    )
    assert (
        sys.modules["giscode.gisai.recovery_store"]
        is sys.modules["giscode.agentic.recovery_store"]
    )
