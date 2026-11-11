"""Planner accuracy: real project APIs, retryable planning, no-progress repair."""

from __future__ import annotations

import json

import pytest

from giscode.agentic.loop import CodingAgentLoop
from giscode.agentic.planner import (
    PlanningError,
    PlanningOutputParser,
    Planner,
    _user_prompt_for,
    _validate_planned_contract,
)
from giscode.agentic.repo_inventory import render_api_block
from giscode.agentic.types import (
    ActionType,
    ExecutionPlan,
    ExecutionStep,
    TaskRequest,
)
from giscode.inference.api import InferenceAPI
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace


def _loop(tmp_path, planner=None) -> CodingAgentLoop:
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    return CodingAgentLoop(
        workspace,
        inference,
        TaskManager(),
        planner=planner,
        approval_callback=lambda *_: True,
    )


def _plan_doc(steps: list[dict], files: list[str]) -> str:
    return json.dumps(
        {
            "objective": "fix",
            "assumptions": [],
            "files_expected_to_change": files,
            "steps": steps,
            "verification_steps": ["run the tests"],
        }
    )


def test_api_block_lists_real_definitions_and_imports(tmp_path):
    (tmp_path / "geo.py").write_text(
        "def area_rect(width, height):\n    return width * height\n", encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        "from geo import area_rect\n\n\ndef total(width, height):\n"
        "    return area_rect(width, height)\n",
        encoding="utf-8",
    )

    block = render_api_block(tmp_path)

    assert "geo.py: def area_rect" in block
    assert "app.py:" in block
    assert "def total" in block
    assert "from geo import area_rect" in block
    assert "def area_triangle" not in block


def test_api_block_is_bounded_and_never_raises(tmp_path):
    for index in range(60):
        (tmp_path / f"m{index:02d}.py").write_text(
            f"def f{index}():\n    return {index}\n", encoding="utf-8"
        )
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    block = render_api_block(tmp_path, limit_files=24, max_chars=400)

    assert block
    assert len(block) <= 400
    assert "m00.py" in block
    assert render_api_block(tmp_path / "missing") == ""


def test_planner_prompt_carries_the_api_surface(tmp_path):
    (tmp_path / "helper.py").write_text(
        "def calc(value):\n    return value * 2\n", encoding="utf-8"
    )
    (tmp_path / "main.py").write_text(
        "from helper import calc\n", encoding="utf-8"
    )

    prompt = _user_prompt_for(
        TaskRequest(task_id="t", description="Fix main.py so run(4) returns 8."),
        tmp_path,
    )

    assert "Repository API" in prompt
    assert "helper.py: def calc" in prompt


def test_parser_rejects_an_invented_local_helper(tmp_path):
    (tmp_path / "helper.py").write_text(
        "def calc(value):\n    return value * 2\n", encoding="utf-8"
    )
    (tmp_path / "main.py").write_text(
        "from helper import calc\n\ndef run(value):\n    return calc(value)\n",
        encoding="utf-8",
    )
    parser = PlanningOutputParser(workspace_root=tmp_path)
    raw = _plan_doc(
        [
            {
                "step_id": "s1",
                "description": "use a helper that does not exist",
                "action_type": "modify",
                "target_path": "main.py",
                "content": "from helper import double\n\n\ndef run(value):\n    return double(value)\n",
            }
        ],
        ["main.py"],
    )

    with pytest.raises(PlanningError) as caught:
        parser.parse(raw)

    message = str(caught.value)
    assert "double" in message
    assert "calc" in message
    assert caught.value.retryable is True


def test_parser_accepts_an_import_the_module_really_defines(tmp_path):
    (tmp_path / "helper.py").write_text(
        "def calc(value):\n    return value * 2\n", encoding="utf-8"
    )
    (tmp_path / "main.py").write_text(
        "from helper import calc\n\ndef run(value):\n    return calc(value)\n",
        encoding="utf-8",
    )
    parser = PlanningOutputParser(workspace_root=tmp_path)
    raw = _plan_doc(
        [
            {
                "step_id": "s1",
                "description": "repair the import",
                "action_type": "modify",
                "target_path": "main.py",
                "content": "from helper import calc\n\n\ndef run(value):\n    return calc(value)\n",
            }
        ],
        ["main.py"],
    )

    plan = parser.parse(raw)

    assert plan.steps[0].target_path == "main.py"


def test_parser_checks_a_rename_against_the_plan_not_the_old_disk(tmp_path):
    (tmp_path / "cart.py").write_text(
        "def old_total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    (tmp_path / "invoice.py").write_text(
        "from cart import old_total\n\n\ndef invoice_total(items):\n"
        "    return old_total(items)\n",
        encoding="utf-8",
    )
    parser = PlanningOutputParser(workspace_root=tmp_path)
    raw = _plan_doc(
        [
            {
                "step_id": "s1",
                "description": "rename",
                "action_type": "modify",
                "target_path": "cart.py",
                "content": "def grand_total(items):\n    return sum(items)\n",
            },
            {
                "step_id": "s2",
                "description": "update the caller",
                "action_type": "modify",
                "target_path": "invoice.py",
                "content": "from cart import grand_total\n\n\ndef invoice_total(items):\n"
                "    return grand_total(items)\n",
            },
        ],
        ["cart.py", "invoice.py"],
    )

    plan = parser.parse(raw)

    assert [step.target_path for step in plan.steps] == ["cart.py", "invoice.py"]


def test_parser_rejects_an_import_that_only_the_old_disk_satisfies(tmp_path):
    (tmp_path / "cart.py").write_text(
        "def old_total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    (tmp_path / "invoice.py").write_text(
        "from cart import old_total\n", encoding="utf-8"
    )
    parser = PlanningOutputParser(workspace_root=tmp_path)
    raw = _plan_doc(
        [
            {
                "step_id": "s2",
                "description": "update the caller without renaming",
                "action_type": "modify",
                "target_path": "invoice.py",
                "content": "from cart import grand_total\n",
            }
        ],
        ["invoice.py"],
    )

    with pytest.raises(PlanningError) as caught:
        parser.parse(raw)

    assert "grand_total" in str(caught.value)
    assert "old_total" in str(caught.value)


def test_plan_must_keep_a_name_the_project_tests_import(tmp_path):
    (tmp_path / "cart.py").write_text(
        "def old_total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    (tmp_path / "invoice.py").write_text(
        "from cart import old_total\n\n\ndef invoice_total(items):\n"
        "    return old_total(items)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_invoice.py").write_text(
        "from cart import grand_total\n\n\ndef test_total():\n"
        "    assert grand_total([2, 3]) == 5\n",
        encoding="utf-8",
    )
    renamed_wrongly = ExecutionPlan(
        objective="rename",
        assumptions=[],
        files_expected_to_change=["cart.py", "invoice.py"],
        steps=[
            ExecutionStep(
                "s1",
                "rename to a name of my own",
                ActionType.MODIFY,
                "cart.py",
                "def calculate_total(items):\n    return sum(items)\n",
            ),
            ExecutionStep(
                "s2",
                "update the caller",
                ActionType.MODIFY,
                "invoice.py",
                "from cart import calculate_total\n\n\ndef invoice_total(items):\n"
                "    return calculate_total(items)\n",
            ),
        ],
        verification_steps=[],
    )

    with pytest.raises(PlanningError) as caught:
        _validate_planned_contract(
            renamed_wrongly, tmp_path, "Rename old_total to grand_total in cart.py"
        )

    assert "test_invoice.py" in str(caught.value)
    assert "grand_total" in str(caught.value)
    assert caught.value.retryable is True


def test_plan_that_keeps_the_imported_name_passes_the_contract(tmp_path):
    (tmp_path / "cart.py").write_text(
        "def old_total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    (tmp_path / "invoice.py").write_text(
        "from cart import old_total\n\n\ndef invoice_total(items):\n"
        "    return old_total(items)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_invoice.py").write_text(
        "from cart import grand_total\n\n\ndef test_total():\n"
        "    assert grand_total([2, 3]) == 5\n",
        encoding="utf-8",
    )
    correct = ExecutionPlan(
        objective="rename",
        assumptions=[],
        files_expected_to_change=["cart.py", "invoice.py"],
        steps=[
            ExecutionStep(
                "s1",
                "rename exactly as asked",
                ActionType.MODIFY,
                "cart.py",
                "def grand_total(items):\n    return sum(items)\n",
            ),
            ExecutionStep(
                "s2",
                "update the caller",
                ActionType.MODIFY,
                "invoice.py",
                "from cart import grand_total\n\n\ndef invoice_total(items):\n"
                "    return grand_total(items)\n",
            ),
        ],
        verification_steps=[],
    )

    _validate_planned_contract(
        correct, tmp_path, "Rename old_total to grand_total in cart.py"
    )


def test_parser_rejects_invalid_python_content(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    parser = PlanningOutputParser(workspace_root=tmp_path)
    raw = _plan_doc(
        [
            {
                "step_id": "s1",
                "description": "broken edit",
                "action_type": "modify",
                "target_path": "app.py",
                "content": "def broken(:\n    return 1\n",
            }
        ],
        ["app.py"],
    )

    with pytest.raises(PlanningError) as caught:
        parser.parse(raw)

    assert "not valid Python" in str(caught.value)
    assert caught.value.retryable is True


def test_planning_retry_asks_once_more_after_an_empty_answer(tmp_path):
    class FlakyPlanner(Planner):
        def __init__(self):
            super().__init__()
            self.calls: list[str] = []

        def create_plan(self, request, cancel_event=None):
            self.calls.append(request.task_id)
            if len(self.calls) == 1:
                raise PlanningError(
                    "planner received empty response from model", retryable=True
                )
            return ExecutionPlan(
                objective="fixed",
                assumptions=[],
                files_expected_to_change=["app.py"],
                steps=[ExecutionStep("s1", "read", ActionType.READ, "app.py")],
                verification_steps=[],
            )

    planner = FlakyPlanner()
    loop = _loop(tmp_path, planner=planner)

    plan = loop._create_plan_with_retry(
        TaskRequest(task_id="task", description="Fix app.py")
    )

    assert planner.calls == ["task", "task-retry"]
    assert plan.objective == "fixed"


def test_non_retryable_planning_failure_is_not_retried(tmp_path):
    class OfflinePlanner(Planner):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def create_plan(self, request, cancel_event=None):
            self.calls += 1
            raise PlanningError(
                "cannot produce an execution plan: the inference backend has no "
                "model loaded"
            )

    planner = OfflinePlanner()
    loop = _loop(tmp_path, planner=planner)

    with pytest.raises(PlanningError):
        loop._create_plan_with_retry(TaskRequest(task_id="task", description="x"))

    assert planner.calls == 1


def test_repeated_repair_plan_is_detected(tmp_path):
    loop = _loop(tmp_path)
    previous = ExecutionPlan(
        objective="first",
        assumptions=[],
        files_expected_to_change=["app.py"],
        steps=[ExecutionStep("s1", "fix", ActionType.MODIFY, "app.py", "VALUE = 2\n")],
        verification_steps=[],
    )
    same = ExecutionPlan(
        objective="second",
        assumptions=[],
        files_expected_to_change=["app.py"],
        steps=[ExecutionStep("s1", "fix again", ActionType.MODIFY, "app.py", "VALUE = 2\n")],
        verification_steps=[],
    )
    different = ExecutionPlan(
        objective="third",
        assumptions=[],
        files_expected_to_change=["app.py"],
        steps=[ExecutionStep("s1", "real fix", ActionType.MODIFY, "app.py", "VALUE = 1\n")],
        verification_steps=[],
    )
    read_only = ExecutionPlan(
        objective="fourth",
        assumptions=[],
        files_expected_to_change=[],
        steps=[ExecutionStep("s1", "inspect", ActionType.READ, "app.py")],
        verification_steps=[],
    )

    assert loop._plans_repeat(previous, same) is True
    assert loop._plans_repeat(previous, different) is False
    assert loop._plans_repeat(previous, read_only) is False
