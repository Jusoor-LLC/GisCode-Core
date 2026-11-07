"""Tests for the LLM-backed production planner and the planner
wiring in :class:`CodingAgentLoop`.

These tests use a *scripted* chat runtime so the planner's
real LLM-calling code path is exercised end-to-end with a
deterministic canned response. They do not require the real
``llama-server.exe`` subprocess and do not use any production
``MockPlanner`` to fake the planner's output.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List, Optional

import pytest

from giscode.agentic.actions import WorkspaceActionExecutor
from giscode.agentic.loop import CodingAgentLoop
from giscode.agentic.planner import (
    MockPlanner,
    Planner,
    PlanningError,
    PlanningOutputParser,
)
from giscode.agentic.types import (
    ActionType,
    ExecutionPhase,
    ExecutionStep,
    TaskRequest,
)
from giscode.inference.api import InferenceAPI
from giscode.inference.lifecycle import ModelInfo, ModelState
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.inference.runtime import (
    ChatRequest,
    ChatResponse,
    GenerationRequest,
    GenerationResponse,
    LlamaCppRuntime,
)
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace


def _ready_api(runtime: LlamaCppRuntime) -> InferenceAPI:
    """Return an :class:`InferenceAPI` whose ``_model`` is in the
    :attr:`ModelState.READY` state, so the planner's health check
    and the API's ``chat_generate`` call both pass.
    """
    api = InferenceAPI(runtime=runtime)
    api._model = ModelInfo(
        name="test-model",
        path="C:/test/model.gguf",
        state=ModelState.READY,
    )
    return api


# ----------------------------------------------------------------- scripted runtime


class _ScriptedChatRuntime(LlamaCppRuntime):
    """A tiny :class:`LlamaCppRuntime` that returns a pre-canned
    response to every chat_generate call. The LLM planner is
    exercised with this runtime so the production code path
    (system prompt -> inference call -> parse) runs end-to-end
    with a deterministic response.
    """

    def __init__(self, canned: str) -> None:
        self._canned = canned
        self._calls: List[ChatRequest] = []
        self._lock = threading.Lock()
        self._server_running = True

    def is_available(self) -> bool:
        return True

    def start_server(self, model_path: str, port: int = 8080) -> bool:
        return True

    def stop_server(self) -> None:
        self._server_running = False

    def is_running(self, port: int = 8080) -> bool:
        return self._server_running

    def generate(self, request, port: int = 8080) -> GenerationResponse:
        return GenerationResponse(text="", tokens_generated=0)

    def chat_generate(self, request: ChatRequest, port: int = 8080) -> ChatResponse:
        with self._lock:
            self._calls.append(request)
        return ChatResponse(
            text=self._canned,
            tokens_generated=max(1, len(self._canned.split())),
            finish_reason="stop",
        )

    @property
    def calls(self) -> List[ChatRequest]:
        return list(self._calls)


# ----------------------------------------------------------------- parser unit tests


def test_parser_handles_bare_json_object():
    parser = PlanningOutputParser()
    raw = json.dumps(
        {
            "objective": "Build a CLI",
            "steps": [
                {
                    "step_id": "s1",
                    "action_type": "create",
                    "target_path": "src/main.py",
                    "content": "print('hi')\n",
                }
            ],
        }
    )
    plan = parser.parse(raw)
    assert len(plan.steps) == 1
    assert plan.steps[0].action_type == ActionType.CREATE
    assert plan.steps[0].target_path == "src/main.py"
    assert plan.steps[0].content == "print('hi')\n"


def test_parser_handles_markdown_fenced_json():
    parser = PlanningOutputParser()
    raw = (
        "Here is the plan you asked for.\n\n"
        "```json\n"
        + json.dumps(
            {
                "objective": "Build a CLI",
                "steps": [
                    {
                        "action_type": "create",
                        "target_path": "src/main.py",
                        "content": "print('hi')\n",
                    }
                ],
            }
        )
        + "\n```\nDone."
    )
    plan = parser.parse(raw)
    assert len(plan.steps) == 1
    assert plan.steps[0].target_path == "src/main.py"


def test_parser_handles_unfenced_prose_with_embedded_json():
    parser = PlanningOutputParser()
    raw = (
        "Sure! Here is the plan:\n"
        + json.dumps(
            {
                "objective": "Build a CLI",
                "steps": [
                    {
                        "action_type": "create",
                        "target_path": "src/main.py",
                        "content": "print('hi')\n",
                    }
                ],
            }
        )
        + "\nThat should do it."
    )
    plan = parser.parse(raw)
    assert len(plan.steps) == 1


def test_parser_rejects_missing_steps():
    parser = PlanningOutputParser()
    with pytest.raises(PlanningError):
        parser.parse(json.dumps({"objective": "x", "no_steps": True}))


def test_parser_rejects_empty_steps():
    parser = PlanningOutputParser()
    with pytest.raises(PlanningError):
        parser.parse(json.dumps({"objective": "x", "steps": []}))


def test_parser_rejects_unsupported_action_type():
    parser = PlanningOutputParser()
    raw = json.dumps(
        {
            "objective": "x",
            "steps": [
                {
                    "action_type": "delete_system32",
                    "target_path": "C:/Windows/System32",
                }
            ],
        }
    )
    with pytest.raises(PlanningError):
        parser.parse(raw)


def test_parser_rejects_absolute_path():
    parser = PlanningOutputParser()
    raw = json.dumps(
        {
            "objective": "x",
            "steps": [
                {"action_type": "create", "target_path": "/etc/passwd", "content": ""}
            ],
        }
    )
    with pytest.raises(PlanningError):
        parser.parse(raw)


def test_parser_rejects_parent_traversal():
    parser = PlanningOutputParser()
    raw = json.dumps(
        {
            "objective": "x",
            "steps": [
                {"action_type": "create", "target_path": "../escape.py", "content": ""}
            ],
        }
    )
    with pytest.raises(PlanningError):
        parser.parse(raw)


def test_parser_rejects_path_escaping_workspace(tmp_path: Path):
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    parser = PlanningOutputParser(workspace_root=workspace_root)
    raw = json.dumps(
        {
            "objective": "x",
            "steps": [
                {
                    "action_type": "create",
                    "target_path": str(other / "x.py"),
                    "content": "",
                }
            ],
        }
    )
    with pytest.raises(PlanningError):
        parser.parse(raw)


def test_parser_normalizes_absolute_path_inside_workspace(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    parser = PlanningOutputParser(workspace_root=root)
    target = str(root / "setup.py")
    raw = json.dumps(
        {
            "objective": "x",
            "steps": [
                {"action_type": "create", "target_path": target, "content": ""}
            ],
        }
    )
    plan = parser.parse(raw)
    assert plan.steps[0].target_path == "setup.py"


def test_parser_normalizes_absolute_project_root_to_dot(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    parser = PlanningOutputParser(workspace_root=root)
    raw = json.dumps(
        {
            "objective": "x",
            "steps": [
                {"action_type": "read", "target_path": str(root), "content": ""}
            ],
        }
    )
    plan = parser.parse(raw)
    assert plan.steps[0].target_path == "."


def test_parser_accepts_relative_path_inside_workspace(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    parser = PlanningOutputParser(workspace_root=root)
    raw = json.dumps(
        {
            "objective": "x",
            "steps": [
                {"action_type": "create", "target_path": "src/main.py", "content": ""}
            ],
        }
    )
    plan = parser.parse(raw)
    assert plan.steps[0].target_path == "src/main.py"


def test_parser_handles_windows_separators(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    parser = PlanningOutputParser(workspace_root=root)
    abs_target = str(root / "src" / "app.py")
    raw = json.dumps(
        {
            "objective": "x",
            "steps": [
                {"action_type": "create", "target_path": abs_target, "content": ""}
            ],
        }
    )
    plan = parser.parse(raw)
    assert Path(plan.steps[0].target_path).parts == ("src", "app.py")


def test_parser_rejects_absolute_path_outside_workspace(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    parser = PlanningOutputParser(workspace_root=root)
    raw = json.dumps(
        {
            "objective": "x",
            "steps": [
                {
                    "action_type": "create",
                    "target_path": str(other / "outside.py"),
                    "content": "",
                }
            ],
        }
    )
    with pytest.raises(PlanningError):
        parser.parse(raw)


def test_parser_rejects_absolute_path_with_traversal(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    parser = PlanningOutputParser(workspace_root=root)
    raw = json.dumps(
        {
            "objective": "x",
            "steps": [
                {
                    "action_type": "create",
                    "target_path": str(root / ".." / "escape.py"),
                    "content": "",
                }
            ],
        }
    )
    with pytest.raises(PlanningError):
        parser.parse(raw)


def test_parser_keeps_only_recognised_top_keys():
    parser = PlanningOutputParser()
    raw = json.dumps(
        {
            "objective": "x",
            "approve": True,  # model smuggled in
            "steps": [
                {
                    "action_type": "create",
                    "target_path": "src/main.py",
                    "content": "",
                }
            ],
        }
    )
    plan = parser.parse(raw)
    assert len(plan.steps) == 1
    # 'approve' is dropped; we never let a model field influence
    # the actual workflow.
    assert not hasattr(plan, "approve")


# ----------------------------------------------------------------- planner unit tests


def test_planner_calls_inference_and_parses_response(tmp_path: Path):
    canned = json.dumps(
        {
            "objective": "Build hello_giscode",
            "steps": [
                {
                    "step_id": "s1",
                    "action_type": "create",
                    "target_path": "src/hello_giscode.py",
                    "content": "print('Hello GisCode')\n",
                },
                {
                    "step_id": "s2",
                    "action_type": "create",
                    "target_path": "tests/test_hello.py",
                    "content": "import unittest\n",
                },
            ],
            "files_expected_to_change": [
                "src/hello_giscode.py",
                "tests/test_hello.py",
            ],
            "verification_steps": ["Files exist", "Python parses"],
        }
    )
    runtime = _ScriptedChatRuntime(canned)
    api = _ready_api(runtime)
    planner = Planner(
        inference=api, workspace_root=tmp_path / "ws", max_tokens=200
    )
    plan = planner.create_plan(
        TaskRequest(
            task_id="t1",
            description="Create a small Python CLI application called hello_giscode",
        )
    )
    # The planner called the runtime exactly once.
    assert len(runtime.calls) == 1
    # And the produced plan contains both requested files.
    assert len(plan.steps) == 2
    targets = sorted(s.target_path for s in plan.steps)
    assert targets == ["src/hello_giscode.py", "tests/test_hello.py"]
    assert sorted(plan.files_expected_to_change) == [
        "src/hello_giscode.py",
        "tests/test_hello.py",
    ]
    # The contents are preserved verbatim.
    by_target = {s.target_path: s for s in plan.steps}
    assert "Hello GisCode" in (by_target["src/hello_giscode.py"].content or "")


def test_planner_handles_markdown_fenced_response(tmp_path: Path):
    canned = (
        "```json\n"
        + json.dumps(
            {
                "objective": "x",
                "steps": [
                    {
                        "action_type": "create",
                        "target_path": "src/a.py",
                        "content": "x = 1\n",
                    }
                ],
            }
        )
        + "\n```"
    )
    runtime = _ScriptedChatRuntime(canned)
    api = _ready_api(runtime)
    planner = Planner(inference=api, workspace_root=tmp_path / "ws")
    plan = planner.create_plan(TaskRequest(task_id="t2", description="x"))
    assert plan.steps[0].target_path == "src/a.py"


def test_planner_fails_on_empty_model_response(tmp_path: Path):
    runtime = _ScriptedChatRuntime("")
    api = _ready_api(runtime)
    planner = Planner(inference=api, workspace_root=tmp_path / "ws")
    with pytest.raises(PlanningError):
        planner.create_plan(TaskRequest(task_id="t3", description="x"))


def test_planner_fails_on_malformed_model_response(tmp_path: Path):
    runtime = _ScriptedChatRuntime("This is not JSON and not a plan at all.")
    api = _ready_api(runtime)
    planner = Planner(inference=api, workspace_root=tmp_path / "ws")
    with pytest.raises(PlanningError):
        planner.create_plan(TaskRequest(task_id="t4", description="x"))


def test_planner_fails_when_model_not_loaded(tmp_path: Path):
    runtime = MockLlamaCppRuntime(available=True)
    api = InferenceAPI(runtime=runtime)
    planner = Planner(inference=api, workspace_root=tmp_path / "ws")
    with pytest.raises(PlanningError):
        planner.create_plan(TaskRequest(task_id="t5", description="x"))


# ----------------------------------------------------------------- loop integration


def test_loop_executes_planner_steps_via_executor(tmp_path: Path):
    """A non-empty planner result is handed to WorkspaceActionExecutor
    and produces the expected files in the workspace.
    """
    canned = json.dumps(
        {
            "objective": "Build hello_giscode",
            "steps": [
                {
                    "step_id": "s1",
                    "action_type": "create",
                    "target_path": "src/hello_giscode.py",
                    "content": "print('Hello GisCode')\n",
                },
                {
                    "step_id": "s2",
                    "action_type": "create",
                    "target_path": "tests/test_hello.py",
                    "content": "import unittest\n\nclass T(unittest.TestCase):\n    def test_hello(self):\n        self.assertTrue(True)\n",
                },
            ],
            "files_expected_to_change": [
                "src/hello_giscode.py",
                "tests/test_hello.py",
            ],
        }
    )
    runtime = _ScriptedChatRuntime(canned)
    api = _ready_api(runtime)
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, api, task_manager)
    request = TaskRequest(task_id="loop-1", description="Create hello_giscode")
    result = loop.start(request)

    # The LLM was consulted exactly once.
    assert len(runtime.calls) == 1
    # And the files were actually created by WorkspaceActionExecutor.
    assert result.status == ExecutionPhase.COMPLETE
    assert result.plan is not None
    assert len(result.actions) == 2
    assert all(a.success for a in result.actions)
    src = workspace.root_path / "src" / "hello_giscode.py"
    tests = workspace.root_path / "tests" / "test_hello.py"
    assert src.exists()
    assert tests.exists()
    assert "Hello GisCode" in src.read_text(encoding="utf-8")
    # Verification (file existence + Python parses) succeeds.
    assert result.verification is not None
    assert result.verification.success is True


def test_loop_fails_clearly_on_planner_error(tmp_path: Path):
    """A planner that raises PlanningError produces a FAILED
    status with a clear error message â€” never a vacuous COMPLETE.
    """
    runtime = _ScriptedChatRuntime("not json, not a plan")
    api = _ready_api(runtime)
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, api, task_manager)
    request = TaskRequest(task_id="loop-bad", description="Do something")
    result = loop.start(request)
    assert result.status == ExecutionPhase.FAILED
    assert "planner failure" in (result.error or "")
    # No actions taken.
    assert result.actions == []
    # No files created in the workspace.
    assert list(workspace.root_path.rglob("*")) == list(workspace.root_path.iterdir())


def test_loop_fails_clearly_when_inference_unavailable(tmp_path: Path):
    """A planner whose inference has no model loaded produces a
    FAILED status, not a silent COMPLETE.
    """
    runtime = MockLlamaCppRuntime(available=True)
    api = InferenceAPI(runtime=runtime)
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, api, task_manager)
    request = TaskRequest(task_id="loop-no-model", description="x")
    result = loop.start(request)
    assert result.status == ExecutionPhase.FAILED
    assert "planner failure" in (result.error or "")


def test_loop_uses_explicit_mock_planner_when_supplied(tmp_path: Path):
    """Backwards compatibility: passing planner=MockPlanner() still
    produces a known-good plan through the loop.
    """
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    api = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    loop = CodingAgentLoop(
        workspace, api, task_manager, planner=MockPlanner()
    )
    request = TaskRequest(task_id="loop-mock", description="x")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    assert result.plan is not None
    assert any(s.target_path == "src/example.py" for s in result.plan.steps)


def test_default_planner_is_llm_backed(tmp_path: Path):
    """When no planner is passed, the loop defaults to the
    LLM-backed Planner (which requires inference to be available).
    """
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    api = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    loop = CodingAgentLoop(workspace, api, task_manager)
    assert isinstance(loop._planner, Planner)
    # And it is the LLM-backed variant, not the MockPlanner.
    assert not isinstance(loop._planner, MockPlanner)

# ------------------------------------------------- reasoning budget wiring

_PLAN_JSON = json.dumps(
    {
        "objective": "x",
        "steps": [
            {"action_type": "create", "target_path": "src/a.py", "content": "x = 1\n"}
        ],
    }
)


def _budget_of_call(runtime):
    assert len(runtime.calls) == 1
    return runtime.calls[0].reasoning_budget


def test_planner_defaults_reasoning_off(tmp_path: Path):
    runtime = _ScriptedChatRuntime(_PLAN_JSON)
    planner = Planner(inference=_ready_api(runtime), workspace_root=tmp_path / "ws")
    planner.create_plan(TaskRequest(task_id="r1", description="x"))
    assert _budget_of_call(runtime) == "off"


def test_planner_reads_reasoning_budget_fn(tmp_path: Path):
    runtime = _ScriptedChatRuntime(_PLAN_JSON)
    planner = Planner(
        inference=_ready_api(runtime),
        workspace_root=tmp_path / "ws",
        reasoning_budget_fn=lambda: "low",
    )
    planner.create_plan(TaskRequest(task_id="r2", description="x"))
    assert _budget_of_call(runtime) == "low"


def test_planner_rejects_unknown_reasoning_budget(tmp_path: Path):
    runtime = _ScriptedChatRuntime(_PLAN_JSON)
    planner = Planner(
        inference=_ready_api(runtime),
        workspace_root=tmp_path / "ws",
        reasoning_budget_fn=lambda: "banana",
    )
    planner.create_plan(TaskRequest(task_id="r3", description="x"))
    assert _budget_of_call(runtime) == "off"


def test_loop_wires_reasoning_budget_from_settings(tmp_path: Path):
    class _Settings:
        def get(self, key, default=None):
            return {"model.reasoning_budget": "low"}.get(key, default)

    workspace = Workspace(tmp_path / "ws")
    api = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    loop = CodingAgentLoop(workspace, api, TaskManager(), settings=_Settings())
    assert loop._reasoning_budget() == "low"
    assert loop._planner._reasoning_budget() == "low"


def test_loop_reasoning_budget_defaults_off_without_settings(tmp_path: Path):
    workspace = Workspace(tmp_path / "ws")
    api = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    loop = CodingAgentLoop(workspace, api, TaskManager())
    assert loop._reasoning_budget() == "off"

