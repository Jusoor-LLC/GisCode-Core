"""Regression tests for the verifier's Python syntax check and UTF-8 BOMs.

CPython accepts a UTF-8 BOM at the start of a source file and strips it
before parsing. The verifier used to read ``.py`` targets with
``encoding="utf-8"``, so a BOM-prefixed file decoded to a leading
``\\ufeff`` and ``ast.parse`` rejected it with a bogus syntax error. That
error short-circuits ``Verifier.verify`` before the test phase, which left
``test_execution_status`` at its default ``"unknown"`` and made
``GisAiRealWorkExecutor`` roll back otherwise correct edits.

These tests pin the behaviour both ways: a BOM must not cause a failure,
and real syntax errors must still be reported.
"""

from __future__ import annotations

from pathlib import Path

from giscode.agentic.types import (
    ActionType,
    ExecutionPlan,
    ExecutionStep,
)
from giscode.agentic.verifier import TEST_STATUS_PASSED, Verifier

BOM = "\ufeff"

VALID_SOURCE = '"""Tiny helpers."""\n\n\ndef average(values):\n    return sum(values) / len(values)\n'
INVALID_SOURCE = "def average(values):\n    return sum(values) / len(values\n"


def _plan(target: str) -> ExecutionPlan:
    return ExecutionPlan(
        objective="BOM syntax check",
        steps=[
            ExecutionStep(
                step_id="1",
                description="Modify module",
                action_type=ActionType.MODIFY,
                target_path=target,
            )
        ],
    )


def _plan_with_test_run(target: str) -> ExecutionPlan:
    return ExecutionPlan(
        objective="BOM syntax check with tests",
        steps=[
            ExecutionStep(
                step_id="1",
                description="Modify module",
                action_type=ActionType.MODIFY,
                target_path=target,
            ),
            ExecutionStep(
                step_id="2",
                description="Run the tests",
                action_type=ActionType.RUN_COMMAND,
                target_path=target,
            ),
        ],
    )


def _edit_action() -> dict:
    return {"tool": "replace_symbol", "args": {}, "success": True, "error": None}


def _pytest_action(success: bool = True) -> dict:
    return {
        "tool": "run_command",
        "args": {"command": "pytest", "arguments": ["tests/test_counters.py", "-v"]},
        "success": success,
        "output": "4 passed",
        "error": None if success else "Command failed with exit code 1",
    }


def _write_bom(path: Path, source: str) -> None:
    path.write_text(BOM + source, encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. A valid BOM-prefixed module is accepted.
# ---------------------------------------------------------------------------


def test_bom_prefixed_valid_python_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "counters.py").write_text(BOM + VALID_SOURCE, encoding="utf-8")

    result = Verifier(tmp_path).verify(_plan("counters.py"), [_edit_action()])

    assert not [e for e in result.errors if "syntax error" in e], result.errors
    assert any("Python file parses" in d for d in result.details), result.details


def test_bom_prefixed_valid_python_reports_passed_with_test_command(
    tmp_path: Path,
) -> None:
    """The regression that caused correct edits to be rolled back.

    With no bogus syntax error, ``verify`` reaches the test phase and
    records a real ``passed`` status instead of the default ``unknown``.
    """
    (tmp_path / "counters.py").write_text(BOM + VALID_SOURCE, encoding="utf-8")

    result = Verifier(tmp_path).verify(
        _plan_with_test_run("counters.py"), [_edit_action(), _pytest_action(success=True)]
    )

    assert result.errors == [], result.errors
    assert result.success is True
    assert result.test_execution_status == TEST_STATUS_PASSED


# ---------------------------------------------------------------------------
# 2. Genuinely invalid source is still rejected.
# ---------------------------------------------------------------------------


def test_invalid_python_is_still_rejected(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text(INVALID_SOURCE, encoding="utf-8")

    result = Verifier(tmp_path).verify(_plan("broken.py"), [_edit_action()])

    assert result.success is False
    assert any("Python syntax error in broken.py" in e for e in result.errors), result.errors


def test_bom_prefixed_invalid_python_is_still_rejected(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text(BOM + INVALID_SOURCE, encoding="utf-8")

    result = Verifier(tmp_path).verify(_plan("broken.py"), [_edit_action()])

    assert result.success is False
    assert any("Python syntax error in broken.py" in e for e in result.errors), result.errors


# ---------------------------------------------------------------------------
# 3. Validation is not weakened: only a single leading BOM is tolerated,
#    which is exactly what CPython itself does.
# ---------------------------------------------------------------------------


def test_second_leading_bom_is_still_rejected(tmp_path: Path) -> None:
    (tmp_path / "double_bom.py").write_text(
        BOM + BOM + VALID_SOURCE, encoding="utf-8"
    )

    result = Verifier(tmp_path).verify(_plan("double_bom.py"), [_edit_action()])

    assert result.success is False
    assert any(
        "Python syntax error in double_bom.py" in e for e in result.errors
    ), result.errors


def test_bom_failure_does_not_become_success_through_the_test_path(
    tmp_path: Path,
) -> None:
    """A syntax error must still fail even when a test command passed."""
    (tmp_path / "broken.py").write_text(BOM + INVALID_SOURCE, encoding="utf-8")

    result = Verifier(tmp_path).verify(
        _plan_with_test_run("broken.py"), [_edit_action(), _pytest_action(success=True)]
    )

    assert result.success is False
    assert any("Python syntax error in broken.py" in e for e in result.errors), result.errors
    assert result.test_execution_status != TEST_STATUS_PASSED