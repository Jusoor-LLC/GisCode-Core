"""Release regressions for the defects found by the independent audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from giscode.agentic.planner import (
    PlanningError,
    _repair_missing_definitions,
    _validate_plan_file_modes,
    _validate_preserved_symbols,
)
from giscode.agentic.types import ActionType, ExecutionPlan, ExecutionStep
from giscode.config.paths import giscode_home
from giscode.inference.runtime import ChatMessage, ChatRequest


def _plan(action: ActionType, path: str, content: str) -> ExecutionPlan:
    return ExecutionPlan(
        objective="change one file",
        steps=[
            ExecutionStep(
                step_id="step-1",
                description="change it",
                action_type=action,
                target_path=path,
                content=content,
            )
        ],
    )


def test_edition_profiles_are_distinct(monkeypatch):
    monkeypatch.delenv("GISCODE_HOME", raising=False)
    monkeypatch.setenv("GISCODE_EDITION", "core")
    core = giscode_home()
    monkeypatch.setenv("GISCODE_EDITION", "pro")
    pro = giscode_home()
    assert core != pro
    assert core.name == "core"
    assert pro.name == "pro"


def test_create_of_existing_file_becomes_guarded_modify(tmp_path):
    target = tmp_path / "maths.py"
    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    plan = _plan(
        ActionType.CREATE, "maths.py", "def add(a, b):\n    return a + b\n"
    )
    _validate_plan_file_modes(plan, tmp_path)
    assert plan.steps[0].action_type is ActionType.MODIFY


def test_modify_of_missing_file_is_rejected(tmp_path):
    with pytest.raises(PlanningError, match="does not exist"):
        _validate_plan_file_modes(
            _plan(ActionType.MODIFY, "missing.py", "x = 1\n"),
            tmp_path,
        )


def test_omitted_existing_definition_is_restored(tmp_path):
    target = tmp_path / "maths.py"
    target.write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    plan = _plan(
        ActionType.MODIFY,
        "maths.py",
        "def subtract(a, b):\n    return a - b\n",
    )
    _repair_missing_definitions(plan, tmp_path, "add subtract without removing add")
    assert "def add(a, b):" in plan.steps[0].content
    assert "def subtract(a, b):" in plan.steps[0].content
    _validate_preserved_symbols(
        plan, tmp_path, "add subtract without removing add"
    )


def test_explicit_removal_is_not_restored(tmp_path):
    target = tmp_path / "maths.py"
    target.write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    plan = _plan(ActionType.MODIFY, "maths.py", "x = 1\n")
    _repair_missing_definitions(plan, tmp_path, "remove add")
    assert "def add(a, b):" not in plan.steps[0].content


def test_chat_request_carries_response_schema():
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="return json")],
        response_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )
    payload = request.to_payload()
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["schema"]["type"] == "object"
