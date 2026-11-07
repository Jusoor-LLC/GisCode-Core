"""Failure semantics for bootstrap()'s optional GisAI import.

A genuinely absent ``giscode.gisai`` leaves slot 18 as ``None``. A present but
incomplete GisAI, a missing adapter, or a failing registration is a broken
install and must surface instead of silently degrading to core-only.

Every case runs in a fresh interpreter: once ``giscode.gisai`` has been
imported, a meta-path finder can no longer simulate absence, and bootstrap
writes settings/state for its environment.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tests.conftest import requires_gisai

REPO_ROOT = Path(__file__).resolve().parents[1]

_HARNESS = "\n".join([
    "import sys, importlib.abc",
    "class _Block(importlib.abc.MetaPathFinder):",
    "    def find_spec(self, name, path=None, target=None):",
    "{body}",
    "        return None",
    "sys.meta_path.insert(0, _Block())",
])

_BOOT = "\n".join([
    "import giscode.main as main_module",
    "boot = main_module.bootstrap()",
    "print('RESULT', len(boot), type(boot[18]).__name__)",
])


def _run(body_lines, script, home):
    body = "\n".join("        " + line for line in body_lines) if body_lines else "        pass"
    env = dict(os.environ)
    env["GISCODE_TEST_MODE"] = "1"
    env["GISCODE_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", _HARNESS.format(body=body) + "\n" + script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=280,
    )


def test_gisai_genuinely_absent_yields_none_slot(tmp_path):
    result = _run(
        [
            'if name == "giscode.gisai" or name.startswith("giscode.gisai."):',
            '    raise ModuleNotFoundError("No module named giscode.gisai", name="giscode.gisai")',
        ],
        _BOOT,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert "RESULT 19 NoneType" in result.stdout


@requires_gisai
def test_missing_internal_gisai_module_propagates(tmp_path):
    result = _run(
        [
            'if name == "giscode.gisai.llm_planner":',
            '    raise ModuleNotFoundError("No module named giscode.gisai.llm_planner", name="giscode.gisai.llm_planner")',
        ],
        _BOOT,
        tmp_path,
    )
    assert result.returncode != 0, "a broken GisAI install must not silently disable GisAI"
    assert "ModuleNotFoundError" in result.stderr
    assert "giscode.gisai.llm_planner" in result.stderr


@requires_gisai
def test_missing_adapter_module_propagates(tmp_path):
    result = _run(
        [
            'if name == "giscode.gisai.tools.adapters":',
            '    raise ModuleNotFoundError("No module named giscode.gisai.tools.adapters", name="giscode.gisai.tools.adapters")',
        ],
        _BOOT,
        tmp_path,
    )
    assert result.returncode != 0, "the adapters are required whenever the engine exists"
    assert "ModuleNotFoundError" in result.stderr
    assert "giscode.gisai.tools.adapters" in result.stderr


@requires_gisai
def test_registration_failure_propagates(tmp_path):
    result = _run(
        [],
        "\n".join([
            "import giscode.gisai.engine as engine_module",
            "",
            "def _boom(*args, **kwargs):",
            "    raise RuntimeError('registration exploded')",
            "",
            "engine_module.GisAiEngine.set_workspace_action_executor = _boom",
            "import giscode.main as main_module",
            "main_module.bootstrap()",
        ]),
        tmp_path,
    )
    assert result.returncode != 0, "a failing registration must not be swallowed"
    assert "RuntimeError" in result.stderr
    assert "registration exploded" in result.stderr


@requires_gisai
def test_healthy_gisai_still_initializes(tmp_path):
    result = _run([], _BOOT, tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    assert "RESULT 19 GisAiEngine" in result.stdout
