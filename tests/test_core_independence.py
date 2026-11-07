"""The complete core runs with the whole GisAI package blocked.

One fresh subprocess blocks every ``giscode.gisai`` import, then exercises
bootstrap, command execution, planning, rollback, deadlines, attachments and
project integrity inspection for real. A second test proves the compatibility
shims are the same module objects as the core modules.
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


def test_core_application_runs_entirely_without_gisai(tmp_path):
    script = "\n".join([
        "import inspect, pathlib, sys, time",
        "home = pathlib.Path(os.environ['PROBE_HOME'])",
        "os.environ.setdefault('GISCODE_HOME', str(home / 'home'))",
        "from giscode.main import bootstrap",
        "values = bootstrap()",
        "print('SLOTS', len(values), 'NONE', [i for i, v in enumerate(values) if v is None])",
        "ws = values[1]",
        "from giscode.terminal import executor as _ex",
        "Request = getattr(_ex, 'CommandRequest', None)",
        "if Request is None:",
        "    from giscode.terminal.types import CommandRequest as Request",
        "res = values[6].execute(Request(command='python', arguments=['-c', \"print('core-ok')\"], working_directory=str(ws.root_path)))",
        "print('CMD_REPR', repr(res)[:400])",
        "proj = home / 'proj'",
        "(proj / 'src').mkdir(parents=True)",
        "(proj / 'src' / 'main.py').write_text('print(1)\\n', encoding='utf-8')",
        "from giscode.agentic.planner import Planner, _source_block, _existing_candidates",
        "print('PLANNER', type(Planner()).__name__, '| BLOCK_HAS_SRC', 'src/main.py' in _source_block(proj), '| CAND', 'src/main.py' in _existing_candidates(proj))",
        "from giscode.agentic.rollback import rollback_task_edits",
        "from giscode.agentic.write_safety import SnapshotManager",
        "target = proj / 'src' / 'calc.py'",
        "target.write_text('VALUE = 1\\n', encoding='utf-8')",
        "snap = SnapshotManager('t1', profile_root=home / 'profile')",
        "snap.snapshot(target, proj)",
        "target.write_text('VALUE = 2\\n', encoding='utf-8')",
        "snap.record_written(target, proj)",
        "outcome = rollback_task_edits(snap, proj)",
        "print('ROLLBACK', outcome.status, outcome.restored, target.read_text(encoding='utf-8').strip())",
        "from giscode.agentic.execution_deadline import ExecutionDeadline",
        "paused = ExecutionDeadline(10.0)",
        "paused.pause(); time.sleep(0.05); paused.resume()",
        "print('DEADLINE_PAUSED_EXPIRED', paused.expired(), 'REMAINING', round(paused.remaining, 1))",
        "quick = ExecutionDeadline(0.01); time.sleep(0.04)",
        "print('DEADLINE_EXPIRED', quick.expired())",
        "from giscode.agentic.attachments import Attachment",
        "att = Attachment(path=str(home / 'x.png'), mime_type='image/png', size_bytes=3)",
        "print('ATTACHMENT', att.path.endswith('x.png'), att.size_bytes, att.attached_at is not None)",
        "from giscode.agentic.integrity import ProjectIntegrityInspector, format_integrity_report, format_full_integrity_report",
        "(proj / 'broken.json').write_text('{ not json', encoding='utf-8')",
        "report = ProjectIntegrityInspector(proj).inspect()",
        "print('INTEGRITY', type(report).__name__, getattr(report, 'status', None), len(getattr(report, 'findings', [])), 'FOUND_BROKEN', 'broken.json' in format_full_integrity_report(report))",
        "print('REPORTING', len(format_integrity_report(report)) > 0, len(format_full_integrity_report(report)) > 0)",
        'print("GISAI_MODULES", [m for m in sys.modules if m.startswith("giscode.gisai")])',
        "",
    ])
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr[-4000:]
    out = result.stdout
    assert "SLOTS 19 NONE [18]" in out
    assert "core-ok" in out
    assert "PLANNER Planner | BLOCK_HAS_SRC True | CAND True" in out
    assert "ROLLBACK rolled_back ['src/calc.py'] VALUE = 1" in out
    assert "DEADLINE_PAUSED_EXPIRED False" in out
    assert "DEADLINE_EXPIRED True" in out
    assert "ATTACHMENT True 3 True" in out
    assert "INTEGRITY ProjectIntegrity" in out
    assert "FOUND_BROKEN True" in out
    assert "REPORTING True True" in out
    assert "GISAI_MODULES []" in out


@requires_gisai
def test_core_and_compatibility_modules_are_the_same_objects():
    import giscode.agentic.execution_deadline as core_deadline
    import giscode.agentic.integrity as core_integrity
    import giscode.agentic.repo_inventory as core_inventory
    import giscode.agentic.rollback as core_rollback
    import giscode.gisai.execution_deadline as legacy_deadline
    import giscode.gisai.inspection.integrity as legacy_integrity
    import giscode.gisai.repo_inventory as legacy_inventory
    import giscode.gisai.safe_rollback as legacy_rollback
    from giscode.agentic.attachments import Attachment as CoreAttachment
    from giscode.gisai.types import Attachment as LegacyAttachment

    assert legacy_deadline is core_deadline
    assert legacy_integrity is core_integrity
    assert legacy_inventory is core_inventory
    assert legacy_rollback is core_rollback
    assert LegacyAttachment is CoreAttachment
    assert legacy_deadline.ExecutionDeadline is core_deadline.ExecutionDeadline
    assert (
        sys.modules["giscode.gisai.execution_deadline"]
        is sys.modules["giscode.agentic.execution_deadline"]
    )
    assert (
        sys.modules["giscode.gisai.inspection.integrity"]
        is sys.modules["giscode.agentic.integrity"]
    )
