"""Core must route real editing requests to the existing coding loop.

Chat is read-only. These tests prove the dispatcher selects the loop only for
model-required editing capabilities, and that the loop can adopt the one task
record the GUI already started without creating a second task.
"""

from __future__ import annotations

from types import SimpleNamespace

from giscode.agentic import capabilities
from giscode.agentic.core_tasks import execute_request
from giscode.agentic.loop import CodingAgentLoop
from giscode.agentic.planner import MockPlanner
from giscode.agentic.types import (
    ActionResult,
    ActionType,
    ExecutionPhase,
    ExecutionPlan,
    ExecutionStep,
    TaskRequest,
    TaskResult,
    VerificationResult,
)
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace


class _SelectedRouter:
    def active_target(self):
        return SimpleNamespace(profile_id="local-coding", model="qwen3:14b")


class _FakeCodingAgent:
    def __init__(self):
        self.started = []
        self.callback = None

    def set_edit_approval_callback(self, callback):
        self.callback = callback

    def changed_files_for(self, task_id):
        return ["calculator.py"]

    def start(self, request, **kwargs):
        self.started.append((request, kwargs))
        return TaskResult(
            task_id=request.task_id,
            status=ExecutionPhase.COMPLETE,
            plan=ExecutionPlan(
                objective="fix add",
                files_expected_to_change=["calculator.py"],
                steps=[
                    ExecutionStep(
                        step_id="step-1",
                        description="fix add",
                        action_type=ActionType.MODIFY,
                        target_path="calculator.py",
                        content="def add(a, b):\n    return a + b\n",
                    )
                ],
            ),
            actions=[
                ActionResult(
                    action_type=ActionType.MODIFY,
                    target_path="C:/work/calculator.py",
                    success=True,
                    message="Modified file",
                )
            ],
            verification=VerificationResult(
                success=True,
                details=["tests passed"],
                test_execution_status="passed",
            ),
            response="Fixed add to return a + b.",
        )


def test_common_editing_verbs_are_model_required():
    for request in (
        "Add area_triangle to geo.py and update app.py",
        "Create test_math_utils.py with tests for multiply",
        "Update services.py to return the full name",
        "Implement a subtraction function",
    ):
        status, _capability, _reason = capabilities.classify(request)
        assert status == capabilities.MODEL_REQUIRED, request


def test_model_required_edit_routes_to_coding_agent(tmp_path):
    agent = _FakeCodingAgent()
    result = execute_request(
        "Fix the add function in calculator.py",
        str(tmp_path),
        task_id="core-model-1",
        model_router=_SelectedRouter(),
        coding_agent=agent,
        agent_task_id="gisai-task",
        approve=lambda path, diff: True,
    )

    assert agent.started, "the editing request never reached CodingAgentLoop"
    request, kwargs = agent.started[0]
    assert "Fix the add function" in request.description
    assert kwargs["adopt_active_task"] is True
    assert kwargs["expected_task_id"] == "gisai-task"
    assert agent.callback is not None
    assert result["status"] == "completed"
    assert result["success"] is True
    assert result["work_result"]["changed_files"] == ["calculator.py"]
    assert result["work_result"]["actions"][0]["tool"] == "write_file"
    assert "Verification: passed" in result["response"]


def test_explanation_stays_out_of_the_editing_loop(tmp_path):
    agent = _FakeCodingAgent()
    try:
        execute_request(
            "Explain what calculator.py does",
            str(tmp_path),
            task_id="core-explain",
            model_router=_SelectedRouter(),
            coding_agent=agent,
        )
    except Exception:
        # The read-only Chat adapter is intentionally allowed to report that
        # this fake router cannot answer; the assertion that matters is that
        # the editing loop was not started.
        pass
    assert agent.started == []


class _NoopInference:
    def get_health(self):
        return {"model_loaded": False}

    def get_model_status(self):
        return None


def test_loop_adopts_the_gui_task_without_a_second_task(tmp_path):
    manager = TaskManager()
    manager.start_task("gisai-task")
    loop = CodingAgentLoop(
        Workspace(tmp_path, ensure_structure=False),
        _NoopInference(),
        manager,
        planner=MockPlanner(),
    )

    result = loop.start(
        TaskRequest(task_id="core-task", description="create example"),
        root_path=str(tmp_path),
        adopt_active_task=True,
        expected_task_id="gisai-task",
    )

    assert result.status == ExecutionPhase.COMPLETE
    assert (tmp_path / "src" / "example.py").is_file()
    assert manager.get_active_task() is None


def test_loop_refuses_a_foreign_active_task(tmp_path):
    manager = TaskManager()
    manager.start_task("someone-else")
    loop = CodingAgentLoop(
        Workspace(tmp_path, ensure_structure=False),
        _NoopInference(),
        manager,
        planner=MockPlanner(),
    )

    result = loop.start(
        TaskRequest(task_id="core-task", description="create example"),
        root_path=str(tmp_path),
        adopt_active_task=True,
        expected_task_id="gisai-task",
    )

    assert result.status == ExecutionPhase.FAILED
    assert result.failure_info["reason"] == "task_manager_busy"
    assert not (tmp_path / "src" / "example.py").exists()
