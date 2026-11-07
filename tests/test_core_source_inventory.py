"""Core planning lists real repository sources without GisAI.

``repo_inventory`` moved to ``giscode.agentic``. These tests block every
``giscode.gisai`` import in a fresh interpreter and then check that the core
planner prompt is grounded in the real source tree, that the old path is the
same module object, and that the rendered block keeps its exact contract.
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


def test_core_planner_is_grounded_in_real_sources_without_gisai(tmp_path):
    script = "\n".join([
        "import pathlib, sys",
        "from giscode.agentic.planner import Planner, _source_block, _existing_candidates",
        "from giscode.agentic.repo_inventory import list_source_files, render_source_block",
        'root = pathlib.Path(os.environ["PROBE_HOME"]) / "proj"',
        '(root / "src").mkdir(parents=True)',
        '(root / "src" / "main.py").write_text("print(1)\\n", encoding="utf-8")',
        '(root / "src" / "Widget.kt").write_text("class Widget\\n", encoding="utf-8")',
        '(root / "node_modules").mkdir()',
        '(root / "node_modules" / "junk.js").write_text("x\\n", encoding="utf-8")',
        "print('FILES', list_source_files(root))",
        "print('BLOCK', repr(_source_block(root)))",
        "print('CANDIDATES', _existing_candidates(root))",
        "print('MODULE', list_source_files.__module__)",
        "print('PLANNER', type(Planner()).__name__)",
        "print('RENDER_EQ', render_source_block(root) == _source_block(root))",
        'print("GISAI_MODULES", [m for m in sys.modules if m.startswith("giscode.gisai")])',
        "",
    ])
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "FILES ['src/Widget.kt', 'src/main.py']" in result.stdout
    assert "src/main.py" in result.stdout and "src/Widget.kt" in result.stdout
    assert "node_modules" not in result.stdout.split("GISAI_MODULES")[0]
    assert "Project source files (relative paths, read from disk" in result.stdout
    assert "CANDIDATES src/Widget.kt, src/main.py" in result.stdout
    assert "MODULE giscode.agentic.repo_inventory" in result.stdout
    assert "PLANNER Planner" in result.stdout
    assert "RENDER_EQ True" in result.stdout
    assert "GISAI_MODULES []" in result.stdout


def test_source_block_contract_is_unchanged(tmp_path):
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (project / "src" / "Widget.kt").write_text("class Widget\n", encoding="utf-8")
    (project / "build").mkdir()
    (project / "build" / "generated.py").write_text("x = 1\n", encoding="utf-8")

    from giscode.agentic.repo_inventory import render_source_block

    assert render_source_block(project) == (
        "Project source files (relative paths, read from disk; use these exact "
        "paths and do not invent any):\n"
        "- src/Widget.kt\n"
        "- src/main.py\n"
    )


@requires_gisai
def test_gisai_repo_inventory_compatibility_path_is_the_core_module():
    import giscode.agentic.repo_inventory as core
    import giscode.gisai.repo_inventory as legacy

    assert legacy is core
    assert sys.modules["giscode.gisai.repo_inventory"] is sys.modules[
        "giscode.agentic.repo_inventory"
    ]
    assert legacy.list_source_files is core.list_source_files
