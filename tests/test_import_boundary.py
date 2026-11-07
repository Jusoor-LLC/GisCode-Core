"""Import boundary: GisCode core must import without the optional GisAI package.

A core-only build ships without ``giscode/gisai``. Each runtime test starts a
fresh interpreter whose meta-path blocks every ``giscode.gisai`` import, so a
module-level GisAI import - present now or added later - fails here instead of
at a user's launch. The static test states the same rule without a process.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DECOUPLED = ("giscode/main.py", "giscode/gui/main_window.py", "giscode/gui/task_report.py")

_BLOCKER = """
import sys, importlib.abc

class _BlockGisAi(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "giscode.gisai" or name.startswith("giscode.gisai."):
            raise ImportError("giscode.gisai is not part of this build: " + name)
        return None

sys.meta_path.insert(0, _BlockGisAi())
"""

# Loaded by file path, not ``from giscode.gui import task_report``:
# ``giscode/gui/__init__.py`` imports tkinter, a GUI prerequisite unrelated to
# GisAI (and absent on headless hosts).
_LOAD_TASK_REPORT = """
import importlib.util, pathlib
_spec = importlib.util.spec_from_file_location(
    "_task_report_probe", pathlib.Path("giscode/gui/task_report.py")
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)
"""


def _run_blocked(body: str, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GISCODE_TEST_MODE"] = "1"
    env["GISCODE_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", _BLOCKER + textwrap.dedent(body)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_giscode_main_imports_without_gisai(tmp_path):
    result = _run_blocked("import giscode.main\nprint('MAIN_IMPORT_OK')\n", tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "MAIN_IMPORT_OK" in result.stdout


def test_task_report_imports_without_gisai(tmp_path):
    result = _run_blocked(
        _LOAD_TASK_REPORT
        + "summary = module.build_change_summary("
        "{'actions': [{'tool': 'read_file', 'success': True, 'args': {}}]}, '')\n"
        "print('TASKREPORT_IMPORT_OK', summary.get('file_count'))\n",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert "TASKREPORT_IMPORT_OK 0" in result.stdout


def test_gui_main_window_imports_without_gisai(tmp_path):
    pytest.importorskip("tkinter")
    result = _run_blocked("import giscode.gui.main_window\nprint('GUI_IMPORT_OK')\n", tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "GUI_IMPORT_OK" in result.stdout


def test_no_module_level_gisai_import_in_decoupled_modules():
    for rel in DECOUPLED:
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8-sig"))
        for node in tree.body:
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            for module in modules:
                assert not (module == "giscode.gisai" or module.startswith("giscode.gisai.")), (
                    f"{rel}:{node.lineno} imports {module} at module scope"
                )


def test_write_action_classification_is_single_and_unchanged():
    from giscode.agentic import verifier
    from giscode.agentic.tool_kinds import WRITE_ACTION_TOOLS

    expected = frozenset(
        {"write_file", "edit_file", "replace_symbol", "add_symbol", "set_docstring"}
    )
    assert WRITE_ACTION_TOOLS == expected
    assert verifier._WRITE_ACTION_TOOLS is WRITE_ACTION_TOOLS
