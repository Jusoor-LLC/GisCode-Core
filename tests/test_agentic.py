import threading
import time
from unittest.mock import MagicMock, call

import pytest

from giscode.agentic.actions import MockWorkspaceActionExecutor, PathValidationError, WorkspaceActionExecutor
from giscode.agentic.loop import CodingAgentLoop
from giscode.agentic.planner import MockPlanner, Planner, PlanningError
from giscode.agentic.types import ActionType, ExecutionPhase, ExecutionPlan, ExecutionStep, TaskRequest, TaskResult, ActionResult, VerificationResult, LoopState
from giscode.agentic.verifier import MockVerifier, Verifier, TEST_STATUS_FAILED, TEST_STATUS_PASSED
from giscode.config.settings import Settings
from giscode.inference.api import InferenceAPI
from giscode.inference.lifecycle import ModelInfo, ModelState
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.models.types import ModelMetadata, ModelStatus
from giscode.task_control.manager import TaskManager
from giscode.terminal.executor import CommandExecutor
from giscode.workspace.workspace import Workspace


class FakeModelManager:
    def __init__(self, inference: InferenceAPI):
        self._inference = inference
        self._loaded = None
        self.load_calls = []
        self.unload_calls = []
        self._registry = {
            "plan-model": ModelMetadata(name="plan-model", family="planning", path="/models/plan-model.gguf", status=ModelStatus.READY),
            "code-model": ModelMetadata(name="code-model", family="coding", path="/models/code-model.gguf", status=ModelStatus.READY),
            "code-model-v1": ModelMetadata(name="code-model-v1", family="coding", path="/models/code-model-v1.gguf", status=ModelStatus.READY),
            "code-model-v2": ModelMetadata(name="code-model-v2", family="coding", path="/models/code-model-v2.gguf", status=ModelStatus.READY),
        }

    def load_model(self, name: str):
        self.load_calls.append(name)
        self._loaded = name
        model_info = ModelInfo(
            name=name,
            path=f"/models/{name}.gguf",
            state=ModelState.READY,
            size_bytes=0,
            context_length=4096,
        )
        self._inference._model = model_info
        # Production load goes through InferenceAPI.load_model -> start_server.
        # Acknowledge the load on the runtime so liveness is consistent.
        runtime = getattr(self._inference, "_runtime", None)
        if runtime is not None and hasattr(runtime, "start_server"):
            try:
                runtime.start_server(model_info.path)
            except Exception:
                pass

    def unload_model(self, name: str):
        self.unload_calls.append(name)
        if self._loaded == name:
            self._loaded = None
            runtime = getattr(self._inference, "_runtime", None)
            if runtime is not None and hasattr(runtime, "stop_server"):
                try:
                    runtime.stop_server()
                except Exception:
                    pass
            self._inference._model = None

    def get_model_status(self, name: str):
        return self._registry.get(name)

    def get_current_model(self):
        return self._loaded

    def check_budget(self, path):
        return True

    def get_registry(self):
        from giscode.models.types import ModelRegistry
        registry = ModelRegistry()
        for meta in self._registry.values():
            registry.register(meta)
        return registry


def test_task_submission(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    request = TaskRequest(task_id="task-001", description="Test task")
    result = loop.submit(request)
    assert result.task_id == "task-001"
    assert result.status == ExecutionPhase.INTAKE


def test_planning_phase_creates_structured_plan(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner = MockPlanner()
    loop = CodingAgentLoop(workspace, inference, task_manager, planner=planner)

    request = TaskRequest(task_id="task-001", description="Test task")
    result = loop.start(request)
    assert result.plan is not None
    assert result.plan.objective == "Mock plan for: Test task"
    assert len(result.plan.steps) > 0


def test_planning_routes_to_registered_planning_model(tmp_path):
    from giscode.routing.types import (
        ContextBundle,
        ModelAvailability,
        ModelRole,
        RoutingRequest,
        RoutingResult,
    )

    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner = MockPlanner()

    model_manager = FakeModelManager(inference)
    routing_service = MagicMock()
    routing_service._model_manager = model_manager
    routing_service.route_and_assemble.side_effect = [
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.CODING,
                selected_model="code-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.CODING,
                selected_model="code-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
    ]

    loop = CodingAgentLoop(
        workspace, inference, task_manager, planner=planner, routing_service=routing_service
    )
    request = TaskRequest(task_id="t1", description="Test planning routing")
    result = loop.start(request)

    assert result.status == ExecutionPhase.COMPLETE
    assert result.plan is not None
    routing_service.route_and_assemble.assert_called()
    planning_call = routing_service.route_and_assemble.call_args_list[0][0][0]
    assert planning_call.requested_role == ModelRole.PLANNING
    impl_call = routing_service.route_and_assemble.call_args_list[1][0][0]
    assert impl_call.requested_role == ModelRole.CODING
    assert model_manager.load_calls == ["plan-model", "code-model"]
    assert model_manager.unload_calls == ["plan-model"]


def test_planning_unloads_model_after_success(tmp_path):
    from giscode.routing.types import (
        ContextBundle,
        ModelAvailability,
        ModelRole,
        RoutingRequest,
        RoutingResult,
    )

    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner = MockPlanner()

    model_manager = FakeModelManager(inference)
    routing_service = MagicMock()
    routing_service._model_manager = model_manager
    routing_service.route_and_assemble.side_effect = [
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.CODING,
                selected_model="code-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.CODING,
                selected_model="code-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
    ]

    loop = CodingAgentLoop(
        workspace, inference, task_manager, planner=planner, routing_service=routing_service
    )
    request = TaskRequest(task_id="t1", description="Test planning routing")
    loop.start(request)

    assert model_manager.load_calls == ["plan-model", "code-model"]
    assert model_manager.unload_calls == ["plan-model"]


def test_planning_unloads_model_after_failure(tmp_path):
    from giscode.routing.types import (
        ContextBundle,
        ModelAvailability,
        ModelRole,
        RoutingRequest,
        RoutingResult,
    )

    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class FailingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            raise PlanningError("planning failed")

    model_manager = FakeModelManager(inference)
    routing_service = MagicMock()
    routing_service._model_manager = model_manager
    routing_service.route_and_assemble.side_effect = [
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.CODING,
                selected_model="code-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.CODING,
                selected_model="code-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
    ]

    loop = CodingAgentLoop(
        workspace,
        inference,
        task_manager,
        planner=FailingPlanner(inference=inference),
        routing_service=routing_service,
    )
    request = TaskRequest(task_id="t1", description="Test planning failure")
    result = loop.start(request)

    assert result.status == ExecutionPhase.FAILED
    assert model_manager.load_calls == ["plan-model"]
    assert model_manager.unload_calls == ["plan-model"]


def test_missing_planning_model_fails_clearly(tmp_path):
    from giscode.routing.types import (
        ContextBundle,
        ModelAvailability,
        ModelRole,
        RoutingRequest,
        RoutingResult,
    )

    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner = MockPlanner()

    model_manager = FakeModelManager(inference)
    routing_service = MagicMock()
    routing_service._model_manager = model_manager
    routing_service.route_and_assemble.return_value = (
        RoutingResult(
            task_id="t1",
            selected_role=ModelRole.PLANNING,
            selected_model=None,
            availability=ModelAvailability.UNAVAILABLE,
            reason="no planning model",
        ),
        ContextBundle(
            task_id="t1",
            task_text="test",
            selected_role=ModelRole.PLANNING,
            selected_model=None,
            namespace="default",
            project_id=None,
            retrieved_records=[],
            formatted_context="",
            record_count=0,
            truncation_occurred=False,
            truncation_reason=None,
            context_char_count=0,
        ),
    )

    loop = CodingAgentLoop(
        workspace, inference, task_manager, planner=planner, routing_service=routing_service
    )
    request = TaskRequest(task_id="t1", description="Test missing planning model")
    result = loop.start(request)

    assert result.status == ExecutionPhase.FAILED
    assert "no planning model available" in result.error


def test_routing_service_none_preserves_behavior(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner = MockPlanner()
    loop = CodingAgentLoop(workspace, inference, task_manager, planner=planner)

    request = TaskRequest(task_id="t1", description="Test no routing")
    result = loop.start(request)

    assert result.status == ExecutionPhase.COMPLETE
    assert result.plan is not None


def test_inference_call(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner = MockPlanner()
    loop = CodingAgentLoop(workspace, inference, task_manager, planner=planner)

    request = TaskRequest(task_id="task-001", description="Test task")
    result = loop.start(request)
    assert result.status in (ExecutionPhase.COMPLETE, ExecutionPhase.FAILED)


def test_implementation_action(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner = MockPlanner()
    loop = CodingAgentLoop(workspace, inference, task_manager, planner=planner)

    request = TaskRequest(task_id="task-001", description="Test task")
    result = loop.start(request)
    if result.actions:
        assert result.actions[0].success is True


def test_workspace_path_validation(tmp_path):
    executor = WorkspaceActionExecutor(Workspace(tmp_path / "ws"))
    with pytest.raises(PathValidationError):
        executor._validate_path("../../../etc/passwd")


def test_path_traversal_rejection(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    step = ExecutionStep(
        step_id="bad-step",
        description="Traversal attempt",
        action_type=ActionType.CREATE,
        target_path="../../../etc/passwd",
        content="malicious",
    )
    result = loop._executor.execute(step)
    assert result.success is False
    assert "escapes workspace" in result.error or "traversal" in result.error


def test_file_creation(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    step = ExecutionStep(
        step_id="create-step",
        description="Create file",
        action_type=ActionType.CREATE,
        target_path="src/new_file.py",
        content="# new file\n",
    )
    result = loop._executor.execute(step)
    assert result.success is True
    assert workspace.exists("src", "new_file.py")


def test_file_modification(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "existing.py", content="old content")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    step = ExecutionStep(
        step_id="modify-step",
        description="Modify file",
        action_type=ActionType.MODIFY,
        target_path="src/existing.py",
        content="new content",
    )
    result = loop._executor.execute(step)
    assert result.success is True
    assert workspace.read("src", "existing.py") == "new content"


def test_verification_success(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "ok.py", content="# ok\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)
    verifier = Verifier(workspace.root_path)

    plan = type("Plan", (), {
        "objective": "test",
        "assumptions": [],
        "files_expected_to_change": ["src/ok.py"],
        "steps": [ExecutionStep("s1", "create", ActionType.CREATE, "src/ok.py", "# ok\n")],
        "verification_steps": [],
    })()
    result = verifier.verify(plan, [])
    assert result.success is True


def test_verification_failure(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)
    verifier = Verifier(workspace.root_path)

    plan = type("Plan", (), {
        "objective": "test",
        "assumptions": [],
        "files_expected_to_change": ["src/missing.py"],
        "steps": [ExecutionStep("s1", "create", ActionType.CREATE, "src/missing.py", "# missing\n")],
        "verification_steps": [],
    })()
    result = verifier.verify(plan, [])
    assert result.success is False
    assert len(result.errors) > 0


def test_cancellation(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    request = TaskRequest(task_id="cancel-task", description="Test cancellation")
    result = loop.submit(request)
    cancelled = loop.cancel()
    assert cancelled is not None
    assert cancelled.status == ExecutionPhase.CANCELLED


def test_timeout(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    for i in range(30):
        workspace.write("src", f"file{i}.py", content="# file\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class SlowPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            steps = [
                ExecutionStep(
                    step_id=f"delay-{i}",
                    description=f"Delay step {i}",
                    action_type=ActionType.READ,
                    target_path=f"src/file{i}.py",
                )
                for i in range(30)
            ]
            return ExecutionPlan(
                objective="Slow timeout test",
                assumptions=[],
                files_expected_to_change=[],
                steps=steps,
                verification_steps=[],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=SlowPlanner(inference=inference))
    request = TaskRequest(task_id="timeout-task", description="Test timeout", max_duration=0.05)

    result = loop.start(request)
    assert result.status == ExecutionPhase.TIMED_OUT


def test_failure_reporting(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    request = TaskRequest(task_id="fail-task", description="Test failure")
    result = loop.submit(request)
    result.status = ExecutionPhase.FAILED
    result.error = "Simulated failure"
    result.failure_info = {"reason": "test"}
    assert result.status == ExecutionPhase.FAILED
    assert result.error == "Simulated failure"


def test_successful_complete_state(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner = MockPlanner()
    loop = CodingAgentLoop(workspace, inference, task_manager, planner=planner)

    request = TaskRequest(task_id="complete-task", description="Test complete")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    assert result.verification is not None
    assert result.verification.success is True


def test_previous_tasks_remain_intact():
    from giscode.config.manager import ConfigManager
    from giscode.task_control.manager import TaskManager

    settings = ConfigManager()
    manager = TaskManager()
    task = manager.start_task("preserve-test")
    assert task.status.value == "running"
    manager.complete(success=True)
    assert manager.get_last_task().status.value == "completed"


def test_mock_planner_creates_deterministic_plan():
    planner = MockPlanner()
    request = TaskRequest(task_id="mock-1", description="Mock planning")
    plan = planner.create_plan(request)
    assert plan.objective == "Mock plan for: Mock planning"
    assert len(plan.steps) == 1
    assert plan.steps[0].action_type == ActionType.CREATE


def test_mock_verifier_success():
    verifier = MockVerifier(file_exists=True, parse_success=True)
    plan = type("Plan", (), {
        "steps": [ExecutionStep("s1", "create", ActionType.CREATE, "src/a.py", "# a\n")],
    })()
    result = verifier.verify(plan, [])
    assert result.success is True


def test_mock_verifier_failure():
    verifier = MockVerifier(file_exists=False, parse_success=False)
    plan = type("Plan", (), {
        "steps": [ExecutionStep("s1", "create", ActionType.CREATE, "src/a.py", "# a\n")],
    })()
    result = verifier.verify(plan, [])
    assert result.success is False


def test_action_result_records_details():
    action = ActionResult(
        action_type=ActionType.CREATE,
        target_path="src/test.py",
        success=True,
        message="Created successfully",
    )
    assert action.success is True
    assert action.target_path == "src/test.py"


def test_execution_phase_enum():
    assert ExecutionPhase.INTAKE.value == "intake"
    assert ExecutionPhase.PLANNING.value == "planning"
    assert ExecutionPhase.IMPLEMENTATION.value == "implementation"
    assert ExecutionPhase.VERIFICATION.value == "verification"
    assert ExecutionPhase.COMPLETE.value == "complete"
    assert ExecutionPhase.CANCELLED.value == "cancelled"
    assert ExecutionPhase.FAILED.value == "failed"
    assert ExecutionPhase.TIMED_OUT.value == "timed_out"


def test_verification_runtime_failure(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "ok.py", content="# ok\n")
    workspace.write("tests", "test_bad.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertEqual(1, 2)\n")
    terminal_executor = CommandExecutor(workspace)
    verifier = Verifier(workspace.root_path, terminal_executor=terminal_executor)

    plan = type("Plan", (), {
        "objective": "test",
        "assumptions": [],
        "files_expected_to_change": ["src/ok.py", "tests/test_bad.py"],
        "steps": [
            ExecutionStep("s1", "create", ActionType.CREATE, "src/ok.py", "# ok\n"),
            ExecutionStep("s2", "create", ActionType.CREATE, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertEqual(1, 2)\n"),
        ],
        "verification_steps": [],
    })()
    result = verifier.verify(plan, [])
    assert result.success is False
    assert result.test_execution_status == TEST_STATUS_FAILED
    assert any("Tests failed" in e for e in result.errors)
    assert any("exit_code: 1" in d for d in result.details)


def test_verification_runtime_success(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "ok.py", content="# ok\n")
    workspace.write("tests", "test_good.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_pass(self):\n        self.assertTrue(True)\n")
    terminal_executor = CommandExecutor(workspace)
    verifier = Verifier(workspace.root_path, terminal_executor=terminal_executor)

    plan = type("Plan", (), {
        "objective": "test",
        "assumptions": [],
        "files_expected_to_change": ["src/ok.py", "tests/test_good.py"],
        "steps": [
            ExecutionStep("s1", "create", ActionType.CREATE, "src/ok.py", "# ok\n"),
            ExecutionStep("s2", "create", ActionType.CREATE, "tests/test_good.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_pass(self):\n        self.assertTrue(True)\n"),
        ],
        "verification_steps": [],
    })()
    result = verifier.verify(plan, [])
    assert result.success is True
    assert result.test_execution_status == TEST_STATUS_PASSED
    assert any("exit_code: 0" in d for d in result.details)


def test_verification_no_tests_dir_skips_runtime(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "ok.py", content="# ok\n")
    terminal_executor = CommandExecutor(workspace)
    verifier = Verifier(workspace.root_path, terminal_executor=terminal_executor)

    plan = type("Plan", (), {
        "objective": "test",
        "assumptions": [],
        "files_expected_to_change": ["src/ok.py"],
        "steps": [ExecutionStep("s1", "create", ActionType.CREATE, "src/ok.py", "# ok\n")],
        "verification_steps": [],
    })()
    result = verifier.verify(plan, [])
    assert result.success is True
    assert result.test_execution_status == "unavailable"
    assert "Plan did not write files; test execution was not attempted." in (result.details[2] if len(result.details) > 2 else " ".join(result.details))


def test_loop_verification_failure_prevents_complete(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class FailingTestPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            return ExecutionPlan(
                objective="test",
                assumptions=[],
                files_expected_to_change=["src/example.py", "tests/test_fail.py"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/example.py", "# example\n"),
                    ExecutionStep("s2", "create", ActionType.CREATE, "tests/test_fail.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertEqual(1, 2)\n"),
                ],
                verification_steps=["Verify tests pass"],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=FailingTestPlanner(inference=inference))
    request = TaskRequest(task_id="verify-fail", description="Test verification failure")
    result = loop.start(request)
    assert result.status == ExecutionPhase.FAILED
    assert result.verification is not None
    assert result.verification.success is False


def test_verification_without_terminal_executor_skips_runtime(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("tests", "test_whatever.py", content="import unittest\n")
    verifier = Verifier(workspace.root_path, terminal_executor=None)

    plan = type("Plan", (), {
        "objective": "test",
        "assumptions": [],
        "files_expected_to_change": ["tests/test_whatever.py"],
        "steps": [ExecutionStep("s1", "create", ActionType.CREATE, "tests/test_whatever.py", "import unittest\n")],
        "verification_steps": [],
    })()
    result = verifier.verify(plan, [])
    assert result.success is False
    assert result.test_execution_status == "unavailable"
    assert any("Python test execution unavailable" in d for d in result.details)


def test_repair_triggered_on_verification_failure(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("tests", "test_bad.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertTrue(False)\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class RepairPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            if request.task_id.endswith("-repair"):
                return ExecutionPlan(
                    objective="repair",
                    assumptions=[],
                    files_expected_to_change=["tests/test_bad.py"],
                    steps=[
                        ExecutionStep("fix-1", "fix test", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(True)\n"),
                    ],
                    verification_steps=[],
                )
            return ExecutionPlan(
                objective="original",
                assumptions=[],
                files_expected_to_change=["tests/test_bad.py"],
                steps=[
                    ExecutionStep("s1", "modify", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(False)\n"),
                ],
                verification_steps=[],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=RepairPlanner(inference=inference), approval_callback=lambda *_: True)
    request = TaskRequest(task_id="repair-trigger", description="Test repair trigger")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    assert result.verification is not None
    assert result.verification.success is True


def test_repair_planner_is_called(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("tests", "test_bad.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertTrue(False)\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    calls = []

    class TrackingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            calls.append(request.task_id)
            if request.task_id.endswith("-repair"):
                return ExecutionPlan(
                    objective="repair",
                    assumptions=[],
                    files_expected_to_change=["tests/test_bad.py"],
                    steps=[
                        ExecutionStep("fix-1", "fix test", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(True)\n"),
                    ],
                    verification_steps=[],
                )
            return ExecutionPlan(
                objective="original",
                assumptions=[],
                files_expected_to_change=["tests/test_bad.py"],
                steps=[
                    ExecutionStep("s1", "modify", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(False)\n"),
                ],
                verification_steps=[],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=TrackingPlanner(inference=inference), approval_callback=lambda *_: True)
    request = TaskRequest(task_id="repair-track", description="Test repair tracking")
    result = loop.start(request)
    assert "repair-track" in calls
    assert "repair-track-repair" in calls
    assert result.status == ExecutionPhase.COMPLETE


def test_repair_actions_execute_through_executor(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("tests", "test_bad.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertTrue(False)\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class RepairPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            if request.task_id.endswith("-repair"):
                return ExecutionPlan(
                    objective="repair",
                    assumptions=[],
                    files_expected_to_change=["tests/test_bad.py"],
                    steps=[
                        ExecutionStep("fix-1", "fix test", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(True)\n"),
                    ],
                    verification_steps=[],
                )
            return ExecutionPlan(
                objective="original",
                assumptions=[],
                files_expected_to_change=["tests/test_bad.py"],
                steps=[
                    ExecutionStep("s1", "modify", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(False)\n"),
                ],
                verification_steps=[],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=RepairPlanner(inference=inference), approval_callback=lambda *_: True)
    request = TaskRequest(task_id="repair-execute", description="Test repair execution")
    result = loop.start(request)
    repair_actions = [a for a in result.actions if a.action_type == ActionType.MODIFY]
    assert len(repair_actions) >= 1
    assert any(a.success for a in repair_actions)


def test_successful_repair_results_in_complete(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("tests", "test_bad.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertTrue(False)\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class RepairPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            if request.task_id.endswith("-repair"):
                return ExecutionPlan(
                    objective="repair",
                    assumptions=[],
                    files_expected_to_change=["tests/test_bad.py"],
                    steps=[
                        ExecutionStep("fix-1", "fix test", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(True)\n"),
                    ],
                    verification_steps=[],
                )
            return ExecutionPlan(
                objective="original",
                assumptions=[],
                files_expected_to_change=["tests/test_bad.py"],
                steps=[
                    ExecutionStep("s1", "modify", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(False)\n"),
                ],
                verification_steps=[],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=RepairPlanner(inference=inference), approval_callback=lambda *_: True)
    request = TaskRequest(task_id="repair-complete", description="Test repair complete")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    assert result.verification is not None
    assert result.verification.success is True


def test_failed_repair_results_in_failed(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("tests", "test_bad.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertTrue(False)\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class BrokenRepairPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            if request.task_id.endswith("-repair"):
                return ExecutionPlan(
                    objective="repair",
                    assumptions=[],
                    files_expected_to_change=["tests/test_bad.py"],
                    steps=[
                        ExecutionStep("fix-1", "noop", ActionType.READ, "tests/test_bad.py"),
                    ],
                    verification_steps=[],
                )
            return ExecutionPlan(
                objective="original",
                assumptions=[],
                files_expected_to_change=["tests/test_bad.py"],
                steps=[
                    ExecutionStep("s1", "modify", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(False)\n"),
                ],
                verification_steps=[],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=BrokenRepairPlanner(inference=inference), approval_callback=lambda *_: True)
    request = TaskRequest(task_id="repair-fail", description="Test repair fail")
    result = loop.start(request)
    assert result.status == ExecutionPhase.FAILED
    assert result.failure_info is not None
    assert result.failure_info.get("reason") == "repair_exhausted"


def test_repair_attempt_limit_is_two(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("tests", "test_bad.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertTrue(False)\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner_calls = []

    class CountingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            planner_calls.append(request.task_id)
            if request.task_id.endswith("-repair"):
                return ExecutionPlan(
                    objective="repair",
                    assumptions=[],
                    files_expected_to_change=["tests/test_bad.py"],
                    steps=[
                        ExecutionStep("fix-1", "noop", ActionType.READ, "tests/test_bad.py"),
                    ],
                    verification_steps=[],
                )
            return ExecutionPlan(
                objective="original",
                assumptions=[],
                files_expected_to_change=["tests/test_bad.py"],
                steps=[
                    ExecutionStep("s1", "modify", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(False)\n"),
                ],
                verification_steps=[],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=CountingPlanner(inference=inference), approval_callback=lambda *_: True)
    request = TaskRequest(task_id="repair-limit", description="Test repair limit")
    result = loop.start(request)
    repair_calls = [c for c in planner_calls if c.endswith("-repair")]
    assert len(repair_calls) == 2
    assert result.status == ExecutionPhase.FAILED
    assert result.failure_info.get("attempts") == 2


def test_repair_planning_failure_results_in_failed(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("tests", "test_bad.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertTrue(False)\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class FailingRepairPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            if request.task_id.endswith("-repair"):
                raise PlanningError("repair planner failed")
            return ExecutionPlan(
                objective="original",
                assumptions=[],
                files_expected_to_change=["tests/test_bad.py"],
                steps=[
                    ExecutionStep("s1", "modify", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(False)\n"),
                ],
                verification_steps=[],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=FailingRepairPlanner(inference=inference), approval_callback=lambda *_: True)
    request = TaskRequest(task_id="repair-plan-fail", description="Test repair plan fail")
    result = loop.start(request)
    assert result.status == ExecutionPhase.FAILED
    assert "repair planning failure" in result.error


def test_successful_task_does_not_invoke_repair(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner_calls = []

    class TrackingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            planner_calls.append(request.task_id)
            return ExecutionPlan(
                objective="ok",
                assumptions=[],
                files_expected_to_change=["src/ok.py"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/ok.py", "# ok\n"),
                ],
                verification_steps=[],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=TrackingPlanner(inference=inference))
    request = TaskRequest(task_id="no-repair", description="Test no repair")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    repair_calls = [c for c in planner_calls if c != "no-repair" and c.endswith("-repair")]
    assert len(repair_calls) == 0


def test_build_repair_request_contains_original_task(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    original_request = TaskRequest(
        task_id="orig",
        description="Create hello_giscode",
        max_duration=90,
    )
    result = TaskResult(task_id="orig", status=ExecutionPhase.VERIFICATION)
    result.verification = VerificationResult(
        success=False,
        errors=["test failed"],
        details=["detail1"],
    )
    result.plan = ExecutionPlan(
        objective="test",
        assumptions=[],
        files_expected_to_change=["src/hello_giscode.py", "tests/test_hello.py"],
        steps=[],
        verification_steps=[],
    )

    repair_request = loop._build_repair_request(original_request, result)
    assert "Create hello_giscode" in repair_request.description
    assert "test failed" in repair_request.description
    assert "detail1" in repair_request.description
    assert "src/hello_giscode.py" in repair_request.description
    assert "tests/test_hello.py" in repair_request.description


def test_build_repair_request_identifies_impl_and_test_files(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    original_request = TaskRequest(task_id="orig", description="task")
    result = TaskResult(task_id="orig", status=ExecutionPhase.VERIFICATION)
    result.verification = VerificationResult(success=False, errors=[], details=[])
    result.plan = ExecutionPlan(
        objective="test",
        assumptions=[],
        files_expected_to_change=["src/app.py", "tests/test_app.py", "README.md"],
        steps=[],
        verification_steps=[],
    )

    repair_request = loop._build_repair_request(original_request, result)
    assert "LIKELY IMPLEMENTATION FILES" in repair_request.description
    assert "LIKELY TEST FILES" in repair_request.description
    assert "src/app.py" in repair_request.description
    assert "tests/test_app.py" in repair_request.description


def test_build_repair_request_forbids_weakening_tests(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    original_request = TaskRequest(task_id="orig", description="task")
    result = TaskResult(task_id="orig", status=ExecutionPhase.VERIFICATION)
    result.verification = VerificationResult(success=False, errors=["fail"], details=["detail"])
    result.plan = ExecutionPlan(
        objective="test",
        assumptions=[],
        files_expected_to_change=["src/hello.py"],
        steps=[],
        verification_steps=[],
    )

    repair_request = loop._build_repair_request(original_request, result)
    assert "Do NOT modify or weaken tests" in repair_request.description
    assert "fix the implementation" in repair_request.description
    assert "evidence from the runtime failure" in repair_request.description
    assert "verification will be rerun" in repair_request.description


def test_validate_repair_plan_accepts_implementation_modification(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    repair_plan = ExecutionPlan(
        objective="repair",
        assumptions=[],
        files_expected_to_change=["src/hello_giscode.py"],
        steps=[
            ExecutionStep("s1", "fix", ActionType.MODIFY, "src/hello_giscode.py", "print('Hello GisCode')"),
        ],
        verification_steps=[],
    )
    verification = VerificationResult(
        success=False,
        errors=["Python test suite failed (exit_code=1)"],
        details=["Test stderr: AssertionError: 'Hello GisCod\\n' != 'Hello GisCode\\n'"],
    )
    suspicious, reason = loop._repair_plan_looks_suspicious(
        repair_plan, ["src/hello_giscode.py"], verification
    )
    assert suspicious is False


def test_validate_repair_plan_rejects_suspicious_test_only_plan(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    repair_plan = ExecutionPlan(
        objective="repair",
        assumptions=[],
        files_expected_to_change=["tests/test_hello.py"],
        steps=[
            ExecutionStep("s1", "fix test", ActionType.MODIFY, "tests/test_hello.py", "import unittest\n"),
        ],
        verification_steps=[],
    )
    verification = VerificationResult(
        success=False,
        errors=["Python test suite failed (exit_code=1)"],
        details=["Test stderr: AssertionError: 'Hello GisCod\\n' != 'Hello GisCode\\n'"],
    )
    suspicious, reason = loop._repair_plan_looks_suspicious(
        repair_plan, ["src/hello_giscode.py"], verification
    )
    assert suspicious is True
    assert "implementation" in reason.lower()


def test_validate_repair_plan_allows_legitimate_test_only_repair(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager)

    repair_plan = ExecutionPlan(
        objective="repair",
        assumptions=[],
        files_expected_to_change=["tests/test_hello.py"],
        steps=[
            ExecutionStep("s1", "fix import", ActionType.MODIFY, "tests/test_hello.py", "import unittest\n"),
        ],
        verification_steps=[],
    )
    verification = VerificationResult(
        success=False,
        errors=["Python test suite failed (exit_code=1)"],
        details=["Test stderr: ImportError: Failed to import test module: test_hello"],
    )
    suspicious, reason = loop._repair_plan_looks_suspicious(
        repair_plan, ["tests/test_hello.py"], verification
    )
    assert suspicious is False


def test_repair_loop_triggers_corrective_replan_once_per_attempt(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "hello.py", content="def main():\n    print('Hello')\n")
    workspace.write("tests", "test_hello.py", content="import unittest\nfrom src import hello\n\nclass T(unittest.TestCase):\n    def test_hello(self):\n        self.assertEqual(hello.main(), 'Hello GisCode')\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner_calls = []

    class TrackingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            planner_calls.append(request.task_id)
            if request.task_id.endswith("-repair"):
                return ExecutionPlan(
                    objective="repair",
                    assumptions=[],
                    files_expected_to_change=["tests/test_hello.py"],
                    steps=[
                        ExecutionStep("fix-test", "fix test", ActionType.MODIFY, "tests/test_hello.py", "import unittest\n"),
                    ],
                    verification_steps=[],
                )
            return ExecutionPlan(
                objective="original",
                assumptions=[],
                files_expected_to_change=["src/hello.py", "tests/test_hello.py"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/hello.py", "def main():\n    print('Hello')\n"),
                    ExecutionStep("s2", "create", ActionType.CREATE, "tests/test_hello.py", "import unittest\nfrom src import hello\n\nclass T(unittest.TestCase):\n    def test_hello(self):\n        self.assertEqual(hello.main(), 'Hello GisCode')\n"),
                ],
                verification_steps=[],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=TrackingPlanner(inference=inference))
    request = TaskRequest(task_id="repair-limit", description="Test repair limit")
    result = loop.start(request)
    repair_calls = [c for c in planner_calls if "repair-limit" in c]
    assert len(repair_calls) <= 3
    assert result.status == ExecutionPhase.FAILED


def test_repair_loop_respects_two_attempt_limit(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner_calls = []

    class CountingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            planner_calls.append(request.task_id)
            if request.task_id == "repair-limit":
                return ExecutionPlan(
                    objective="original",
                    assumptions=[],
                    files_expected_to_change=["src/hello.py", "tests/test_hello.py"],
                    steps=[
                        ExecutionStep("s1", "create", ActionType.CREATE, "src/hello.py", "def main():\n    print('Hello')\n"),
                        ExecutionStep("s2", "create", ActionType.CREATE, "tests/test_hello.py", "import unittest\nfrom src import hello\n\nclass T(unittest.TestCase):\n    def test_hello(self):\n        self.assertEqual(hello.main(), 'Hello GisCode')\n"),
                    ],
                    verification_steps=[],
                )
            return ExecutionPlan(
                objective="repair",
                assumptions=[],
                files_expected_to_change=["tests/test_hello.py"],
                steps=[
                    ExecutionStep("fix-test", "fix test", ActionType.MODIFY, "tests/test_hello.py", "import unittest\n"),
                ],
                verification_steps=[],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=CountingPlanner(inference=inference))
    request = TaskRequest(task_id="repair-limit", description="Test repair limit")
    result = loop.start(request)
    repair_calls = [c for c in planner_calls if "repair-limit-repair" in c]
    assert len(repair_calls) == 4
    assert result.status == ExecutionPhase.FAILED
    assert result.failure_info.get("attempts") == 2


def test_successful_verification_skips_repair_validation(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner_calls = []

    class TrackingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            planner_calls.append(request.task_id)
            return ExecutionPlan(
                objective="ok",
                assumptions=[],
                files_expected_to_change=["src/ok.py"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/ok.py", "# ok\n"),
                ],
                verification_steps=[],
            )

    loop = CodingAgentLoop(workspace, inference, task_manager, planner=TrackingPlanner(inference=inference))
    request = TaskRequest(task_id="no-repair", description="Test no repair")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    repair_calls = [c for c in planner_calls if c != "no-repair" and "repair" in c]
    assert len(repair_calls) == 0


def test_packaging_runs_after_successful_verification(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    packaging_calls = []

    class FakePackagingBuilder:
        def __init__(self, *args, **kwargs):
            pass
        def build(self):
            packaging_calls.append("build")
            return {"status": "build_succeeded", "package": "dist/app.msix"}

    class FakeSigningService:
        def __init__(self, *args, **kwargs):
            pass
        def sign(self, package_path):
            return {"status": "signing_completed", "package": str(package_path)}

    class TrackingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            return ExecutionPlan(
                objective="ok",
                assumptions=[],
                files_expected_to_change=["src/ok.py"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/ok.py", "# ok\n"),
                ],
                verification_steps=[],
            )

    settings = Settings(data={"packaging": {"enabled": True}})
    loop = CodingAgentLoop(
        workspace,
        inference,
        task_manager,
        planner=TrackingPlanner(inference=inference),
        packaging_builder=FakePackagingBuilder(),
        signing_service=FakeSigningService(),
        settings=settings,
    )
    request = TaskRequest(task_id="pkg-ok", description="Test packaging")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    assert result.packaging is not None
    assert result.packaging.get("status") == "build_succeeded"
    assert "build" in packaging_calls


def test_packaging_runs_after_successful_repair_and_verification(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "hello.py", content="def main():\n    return 'Hello'\n")
    workspace.write("tests", "test_hello.py", content="import unittest\nfrom src import hello\n\nclass T(unittest.TestCase):\n    def test_hello(self):\n        self.assertEqual(hello.main(), 'Hello')\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    packaging_calls = []

    class FakePackagingBuilder:
        def __init__(self, *args, **kwargs):
            pass
        def build(self):
            packaging_calls.append("build")
            return {"status": "build_succeeded", "package": "dist/app.msix"}

    class FakeSigningService:
        def __init__(self, *args, **kwargs):
            pass
        def sign(self, package_path):
            return {"status": "signing_completed", "package": str(package_path)}

    class RepairPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            if request.task_id.endswith("-repair"):
                return ExecutionPlan(
                    objective="repair",
                    assumptions=[],
                    files_expected_to_change=["src/hello.py", "tests/test_hello.py"],
                    steps=[
                        ExecutionStep("fix-source", "fix source", ActionType.MODIFY, "src/hello.py", "def main():\n    return 'Hello GisCode'\n"),
                        ExecutionStep("fix-test", "fix test", ActionType.MODIFY, "tests/test_hello.py", "import unittest\nfrom src import hello\n\nclass T(unittest.TestCase):\n    def test_hello(self):\n        self.assertEqual(hello.main(), 'Hello GisCode')\n"),
                    ],
                    verification_steps=[],
                )
            return ExecutionPlan(
                objective="original",
                assumptions=[],
                files_expected_to_change=["src/hello.py", "tests/test_hello.py"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/hello.py", "def main():\n    return 'Hello'\n"),
                    ExecutionStep("s2", "create", ActionType.CREATE, "tests/test_hello.py", "import unittest\nfrom src import hello\n\nclass T(unittest.TestCase):\n    def test_hello(self):\n        self.assertEqual(hello.main(), 'Hello')\n"),
                ],
                verification_steps=[],
            )

    settings = Settings(data={"packaging": {"enabled": True}})
    loop = CodingAgentLoop(
        workspace,
        inference,
        task_manager,
        planner=RepairPlanner(inference=inference),
        approval_callback=lambda *_: True,
        packaging_builder=FakePackagingBuilder(),
        signing_service=FakeSigningService(),
        settings=settings,
    )
    request = TaskRequest(task_id="pkg-repair", description="Test packaging after repair")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    assert result.packaging is not None
    assert result.packaging.get("status") == "build_succeeded"
    assert "build" in packaging_calls


def test_packaging_disabled_skips_packaging(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    packaging_calls = []

    class FakePackagingBuilder:
        def __init__(self, *args, **kwargs):
            pass
        def build(self):
            packaging_calls.append("build")
            return {"status": "build_succeeded"}

    class FakeSigningService:
        def __init__(self, *args, **kwargs):
            pass

    class TrackingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            return ExecutionPlan(
                objective="ok",
                assumptions=[],
                files_expected_to_change=["src/ok.py"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/ok.py", "# ok\n"),
                ],
                verification_steps=[],
            )

    settings = Settings(data={"packaging": {"enabled": False}})
    loop = CodingAgentLoop(
        workspace,
        inference,
        task_manager,
        planner=TrackingPlanner(inference=inference),
        packaging_builder=FakePackagingBuilder(),
        signing_service=FakeSigningService(),
        settings=settings,
    )
    request = TaskRequest(task_id="pkg-disabled", description="Test packaging disabled")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    assert result.packaging is not None
    assert result.packaging.get("status") == "skipped"
    assert "build" not in packaging_calls


def test_verification_failure_prevents_packaging(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("tests", "test_bad.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.assertTrue(False)\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    packaging_calls = []

    class FakePackagingBuilder:
        def __init__(self, *args, **kwargs):
            pass
        def build(self):
            packaging_calls.append("build")
            return {"status": "build_succeeded"}

    class FakeSigningService:
        def __init__(self, *args, **kwargs):
            pass

    class BadPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            return ExecutionPlan(
                objective="bad",
                assumptions=[],
                files_expected_to_change=["tests/test_bad.py"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_bad(self):\n                        self.assertTrue(False)\n"),
                ],
                verification_steps=[],
            )

    settings = Settings(data={"packaging": {"enabled": True}})
    loop = CodingAgentLoop(
        workspace,
        inference,
        task_manager,
        planner=BadPlanner(inference=inference),
        packaging_builder=FakePackagingBuilder(),
        signing_service=FakeSigningService(),
        settings=settings,
    )
    request = TaskRequest(task_id="pkg-fail-verify", description="Test verification failure prevents packaging")
    result = loop.start(request)
    assert result.status == ExecutionPhase.FAILED
    assert result.packaging is None
    assert "build" not in packaging_calls


def test_packaging_failure_does_not_invalidate_verification(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class FakePackagingBuilder:
        def __init__(self, *args, **kwargs):
            pass
        def build(self):
            return {"status": "build_failed", "error": "makeappx not found"}

    class FakeSigningService:
        def __init__(self, *args, **kwargs):
            pass

    class TrackingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            return ExecutionPlan(
                objective="ok",
                assumptions=[],
                files_expected_to_change=["src/ok.py"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/ok.py", "# ok\n"),
                ],
                verification_steps=[],
            )

    settings = Settings(data={"packaging": {"enabled": True}})
    loop = CodingAgentLoop(
        workspace,
        inference,
        task_manager,
        planner=TrackingPlanner(inference=inference),
        packaging_builder=FakePackagingBuilder(),
        signing_service=FakeSigningService(),
        settings=settings,
    )
    request = TaskRequest(task_id="pkg-fail", description="Test packaging failure")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    assert result.verification is not None
    assert result.verification.success is True
    assert result.packaging is not None
    assert result.packaging.get("status") == "build_failed"


def test_signing_failure_does_not_block_completion(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class FakePackagingBuilder:
        def __init__(self, *args, **kwargs):
            pass
        def build(self):
            return {"status": "build_succeeded", "package": "dist/app.msix"}

    class FakeSigningService:
        def __init__(self, *args, **kwargs):
            pass
        def sign(self, package_path):
            return {"status": "prerequisites_unavailable", "message": "signtool missing"}

    class TrackingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            return ExecutionPlan(
                objective="ok",
                assumptions=[],
                files_expected_to_change=["src/ok.py"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/ok.py", "# ok\n"),
                ],
                verification_steps=[],
            )

    settings = Settings(data={"packaging": {"enabled": True}})
    loop = CodingAgentLoop(
        workspace,
        inference,
        task_manager,
        planner=TrackingPlanner(inference=inference),
        packaging_builder=FakePackagingBuilder(),
        signing_service=FakeSigningService(),
        settings=settings,
    )
    request = TaskRequest(task_id="pkg-sign-fail", description="Test signing failure")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    assert result.verification is not None
    assert result.verification.success is True
    assert result.packaging is not None
    signing_info = result.packaging.get("signing", {})
    assert signing_info.get("status") == "prerequisites_unavailable"


def test_artifact_staging_failure_does_not_block_completion(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class FakePackagingBuilder:
        def __init__(self, *args, **kwargs):
            pass
        def build(self):
            return {"status": "build_failed", "error": "staging failed"}

    class FakeSigningService:
        def __init__(self, *args, **kwargs):
            pass

    class TrackingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            return ExecutionPlan(
                objective="ok",
                assumptions=[],
                files_expected_to_change=["src/ok.py"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/ok.py", "# ok\n"),
                ],
                verification_steps=[],
            )

    settings = Settings(data={"packaging": {"enabled": True}})
    loop = CodingAgentLoop(
        workspace,
        inference,
        task_manager,
        planner=TrackingPlanner(inference=inference),
        packaging_builder=FakePackagingBuilder(),
        signing_service=FakeSigningService(),
        settings=settings,
    )
    request = TaskRequest(task_id="pkg-stage-fail", description="Test staging failure")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    assert result.verification is not None
    assert result.verification.success is True
    assert result.packaging is not None
    assert result.packaging.get("status") == "build_failed"


def test_packaging_result_attached_to_task_result(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class FakePackagingBuilder:
        def __init__(self, *args, **kwargs):
            pass
        def build(self):
            return {"status": "build_succeeded", "package": "dist/app.msix"}

    class FakeSigningService:
        def __init__(self, *args, **kwargs):
            pass
        def sign(self, package_path):
            return {"status": "signing_completed", "package": str(package_path)}

    class TrackingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            return ExecutionPlan(
                objective="ok",
                assumptions=[],
                files_expected_to_change=["src/ok.py"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/ok.py", "# ok\n"),
                ],
                verification_steps=[],
            )

    settings = Settings(data={"packaging": {"enabled": True}})
    loop = CodingAgentLoop(
        workspace,
        inference,
        task_manager,
        planner=TrackingPlanner(inference=inference),
        packaging_builder=FakePackagingBuilder(),
        signing_service=FakeSigningService(),
        settings=settings,
    )
    request = TaskRequest(task_id="pkg-attach", description="Test packaging attachment")
    result = loop.start(request)
    assert result.status == ExecutionPhase.COMPLETE
    assert result.packaging is not None
    assert "status" in result.packaging
    assert "package" in result.packaging
    assert "signing" in result.packaging


def test_implementation_routes_to_coding_model(tmp_path):
    from giscode.routing.types import (
        ContextBundle,
        ModelAvailability,
        ModelRole,
        RoutingRequest,
        RoutingResult,
    )

    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class CodingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            return ExecutionPlan(
                objective="create file",
                assumptions=[],
                files_expected_to_change=["src/new_file.txt"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/new_file.txt", "# ok\n"),
                ],
                verification_steps=[],
            )

    model_manager = FakeModelManager(inference)
    routing_service = MagicMock()
    routing_service._model_manager = model_manager
    routing_service.route_and_assemble.side_effect = [
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.CODING,
                selected_model="code-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.CODING,
                selected_model="code-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
    ]

    loop = CodingAgentLoop(
        workspace, inference, task_manager, planner=CodingPlanner(inference=inference), routing_service=routing_service
    )
    request = TaskRequest(task_id="t1", description="Test implementation routing")
    result = loop.start(request)

    assert result.status == ExecutionPhase.COMPLETE
    assert model_manager.load_calls == ["plan-model", "code-model"]
    assert model_manager.unload_calls == ["plan-model"]


def test_planning_unload_before_coding_load(tmp_path):
    from giscode.routing.types import (
        ContextBundle,
        ModelAvailability,
        ModelRole,
        RoutingRequest,
        RoutingResult,
    )

    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class CodingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            return ExecutionPlan(
                objective="create file",
                assumptions=[],
                files_expected_to_change=["src/new_file.txt"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/new_file.txt", "# ok\n"),
                ],
                verification_steps=[],
            )

    model_manager = FakeModelManager(inference)
    routing_service = MagicMock()
    routing_service._model_manager = model_manager
    routing_service.route_and_assemble.side_effect = [
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.CODING,
                selected_model="code-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.CODING,
                selected_model="code-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
    ]

    loop = CodingAgentLoop(
        workspace, inference, task_manager, planner=CodingPlanner(inference=inference), routing_service=routing_service
    )
    request = TaskRequest(task_id="t1", description="Test planning to coding order")
    loop.start(request)

    assert model_manager.unload_calls == ["plan-model"]
    assert model_manager.load_calls == ["plan-model", "code-model"]


def test_repair_reuses_loaded_coding_model(tmp_path):
    from giscode.routing.types import (
        ContextBundle,
        ModelAvailability,
        ModelRole,
        RoutingRequest,
        RoutingResult,
    )

    workspace = Workspace(tmp_path / "ws")
    workspace.write("tests", "test_bad.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertTrue(False)\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class RepairPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            if request.task_id.endswith("-repair"):
                return ExecutionPlan(
                    objective="repair",
                    assumptions=[],
                    files_expected_to_change=["tests/test_bad.py"],
                    steps=[
                        ExecutionStep("fix-1", "fix test", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fix(self):\n                        self.assertTrue(True)\n"),
                    ],
                    verification_steps=[],
                )
            return ExecutionPlan(
                objective="original",
                assumptions=[],
                files_expected_to_change=["tests/test_bad.py"],
                steps=[
                    ExecutionStep("s1", "modify", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(False)\n"),
                ],
                verification_steps=[],
            )

    model_manager = FakeModelManager(inference)
    routing_service = MagicMock()
    routing_service._model_manager = model_manager
    call_count = [0]
    def route_side_effect(request):
        call_count[0] += 1
        if call_count[0] == 1:
            return (
                RoutingResult(task_id="t1", selected_role=ModelRole.PLANNING, selected_model="plan-model", availability=ModelAvailability.AVAILABLE, reason="matched"),
                ContextBundle(task_id="t1", task_text="test", selected_role=ModelRole.PLANNING, selected_model="plan-model", namespace="default", project_id=None, retrieved_records=[], formatted_context="", record_count=0, truncation_occurred=False, truncation_reason=None, context_char_count=0),
            )
        if call_count[0] == 2:
            return (
                RoutingResult(task_id="t1", selected_role=ModelRole.CODING, selected_model="code-model", availability=ModelAvailability.AVAILABLE, reason="matched"),
                ContextBundle(task_id="t1", task_text="test", selected_role=ModelRole.CODING, selected_model="code-model", namespace="default", project_id=None, retrieved_records=[], formatted_context="", record_count=0, truncation_occurred=False, truncation_reason=None, context_char_count=0),
            )
        return (
            RoutingResult(task_id="t1-repair", selected_role=ModelRole.CODING, selected_model="code-model", availability=ModelAvailability.AVAILABLE, reason="matched"),
            ContextBundle(task_id="t1-repair", task_text="test", selected_role=ModelRole.CODING, selected_model="code-model", namespace="default", project_id=None, retrieved_records=[], formatted_context="", record_count=0, truncation_occurred=False, truncation_reason=None, context_char_count=0),
        )
    routing_service.route_and_assemble.side_effect = route_side_effect

    loop = CodingAgentLoop(
        workspace, inference, task_manager, planner=RepairPlanner(inference=inference), routing_service=routing_service,
        approval_callback=lambda *_: True,
    )
    request = TaskRequest(task_id="t1", description="Test repair reuse")
    result = loop.start(request)

    assert result.status == ExecutionPhase.COMPLETE
    assert model_manager.load_calls.count("code-model") == 1


def test_repair_loads_different_coding_model_when_required(tmp_path):
    from giscode.routing.types import (
        ContextBundle,
        ModelAvailability,
        ModelRole,
        RoutingRequest,
        RoutingResult,
    )

    workspace = Workspace(tmp_path / "ws")
    workspace.write("tests", "test_bad.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertTrue(False)\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class RepairPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            if request.task_id.endswith("-repair"):
                return ExecutionPlan(
                    objective="repair",
                    assumptions=[],
                    files_expected_to_change=["tests/test_bad.py"],
                    steps=[
                        ExecutionStep("fix-1", "fix test", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fix(self):\n                        self.assertTrue(True)\n"),
                    ],
                    verification_steps=[],
                )
            return ExecutionPlan(
                objective="original",
                assumptions=[],
                files_expected_to_change=["tests/test_bad.py"],
                steps=[
                    ExecutionStep("s1", "modify", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(False)\n"),
                ],
                verification_steps=[],
            )

    model_manager = FakeModelManager(inference)
    routing_service = MagicMock()
    routing_service._model_manager = model_manager
    call_count = [0]
    def route_side_effect(request):
        call_count[0] += 1
        if call_count[0] == 1:
            result = (
                RoutingResult(task_id="t1", selected_role=ModelRole.PLANNING, selected_model="plan-model", availability=ModelAvailability.AVAILABLE, reason="matched"),
                ContextBundle(task_id="t1", task_text="test", selected_role=ModelRole.PLANNING, selected_model="plan-model", namespace="default", project_id=None, retrieved_records=[], formatted_context="", record_count=0, truncation_occurred=False, truncation_reason=None, context_char_count=0),
            )
        elif call_count[0] == 2:
            result = (
                RoutingResult(task_id="t1", selected_role=ModelRole.CODING, selected_model="code-model-v1", availability=ModelAvailability.AVAILABLE, reason="matched"),
                ContextBundle(task_id="t1", task_text="test", selected_role=ModelRole.CODING, selected_model="code-model-v1", namespace="default", project_id=None, retrieved_records=[], formatted_context="", record_count=0, truncation_occurred=False, truncation_reason=None, context_char_count=0),
            )
        elif call_count[0] == 3:
            result = (
                RoutingResult(task_id="t1-repair", selected_role=ModelRole.CODING, selected_model="code-model-v2", availability=ModelAvailability.AVAILABLE, reason="matched"),
                ContextBundle(task_id="t1-repair", task_text="test", selected_role=ModelRole.CODING, selected_model="code-model-v2", namespace="default", project_id=None, retrieved_records=[], formatted_context="", record_count=0, truncation_occurred=False, truncation_reason=None, context_char_count=0),
            )
        else:
            result = (
                RoutingResult(task_id="t1-repair", selected_role=ModelRole.CODING, selected_model="code-model-v2", availability=ModelAvailability.AVAILABLE, reason="matched"),
                ContextBundle(task_id="t1-repair", task_text="test", selected_role=ModelRole.CODING, selected_model="code-model-v2", namespace="default", project_id=None, retrieved_records=[], formatted_context="", record_count=0, truncation_occurred=False, truncation_reason=None, context_char_count=0),
            )
        return result
    routing_service.route_and_assemble.side_effect = route_side_effect

    loop = CodingAgentLoop(
        workspace, inference, task_manager, planner=RepairPlanner(inference=inference), routing_service=routing_service,
        approval_callback=lambda *_: True,
    )
    request = TaskRequest(task_id="t1", description="Test repair different model")
    result = loop.start(request)

    assert result.status == ExecutionPhase.COMPLETE
    assert model_manager.load_calls == ["plan-model", "code-model-v1", "code-model-v2"]


def test_missing_coding_model_fails_clearly(tmp_path):
    from giscode.routing.types import (
        ContextBundle,
        ModelAvailability,
        ModelRole,
        RoutingRequest,
        RoutingResult,
    )

    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class CodingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            return ExecutionPlan(
                objective="create file",
                assumptions=[],
                files_expected_to_change=["src/new_file.txt"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/new_file.txt", "# ok\n"),
                ],
                verification_steps=[],
            )

    model_manager = FakeModelManager(inference)
    routing_service = MagicMock()
    routing_service._model_manager = model_manager
    routing_service.route_and_assemble.side_effect = [
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.CODING,
                selected_model=None,
                availability=ModelAvailability.UNAVAILABLE,
                reason="no coding model",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.CODING,
                selected_model=None,
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
    ]

    loop = CodingAgentLoop(
        workspace, inference, task_manager, planner=CodingPlanner(inference=inference), routing_service=routing_service
    )
    request = TaskRequest(task_id="t1", description="Test missing coding model")
    result = loop.start(request)

    assert result.status == ExecutionPhase.FAILED
    assert "no coding model available" in result.error


def test_model_load_failure_handles_safely(tmp_path):
    from giscode.routing.types import (
        ContextBundle,
        ModelAvailability,
        ModelRole,
        RoutingRequest,
        RoutingResult,
    )

    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()

    class CodingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            return ExecutionPlan(
                objective="create file",
                assumptions=[],
                files_expected_to_change=["src/new_file.txt"],
                steps=[
                    ExecutionStep("s1", "create", ActionType.CREATE, "src/new_file.txt", "# ok\n"),
                ],
                verification_steps=[],
            )

    model_manager = FakeModelManager(inference)
    model_manager.load_model = lambda name: (_ for _ in ()).throw(RuntimeError("load failed"))
    routing_service = MagicMock()
    routing_service._model_manager = model_manager
    routing_service.route_and_assemble.side_effect = [
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.PLANNING,
                selected_model="plan-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.CODING,
                selected_model="code-model",
                availability=ModelAvailability.AVAILABLE,
                reason="matched",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.CODING,
                selected_model="code-model",
                namespace="default",
                project_id=None,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
            ),
        ),
    ]

    loop = CodingAgentLoop(
        workspace, inference, task_manager, planner=CodingPlanner(inference=inference), routing_service=routing_service
    )
    request = TaskRequest(task_id="t1", description="Test load failure")
    result = loop.start(request)

    assert result.status == ExecutionPhase.FAILED
    assert "model load failed" in result.error


def test_repair_limit_remains_two(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("tests", "test_bad.py", content="import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n        self.assertTrue(False)\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    repair_calls = []

    class AlwaysFailingPlanner(Planner):
        def create_plan(self, request: TaskRequest):
            repair_calls.append(request.task_id)
            return ExecutionPlan(
                objective="repair",
                assumptions=[],
                files_expected_to_change=["tests/test_bad.py"],
                steps=[
                    ExecutionStep("fix-1", "fix test", ActionType.MODIFY, "tests/test_bad.py", "import unittest\n\nclass T(unittest.TestCase):\n    def test_fail(self):\n                        self.assertTrue(False)\n"),
                ],
                verification_steps=[],
            )

    loop = CodingAgentLoop(
        workspace, inference, task_manager, planner=AlwaysFailingPlanner(inference=inference),
        approval_callback=lambda *_: True,
    )
    request = TaskRequest(task_id="repair-limit", description="Test repair limit")
    result = loop.start(request)

    assert result.status == ExecutionPhase.FAILED
    assert result.failure_info.get("reason") == "repair_exhausted"
    assert result.failure_info.get("attempts") == 2
    assert repair_calls.count("repair-limit-repair") == 2


def test_coding_agent_get_current_state(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    planner = MockPlanner()
    loop = CodingAgentLoop(workspace, inference, task_manager, planner=planner)

    state_before = loop.get_current_state()
    assert state_before is not None
    assert state_before.task_id is None
    assert state_before.phase is None
    assert state_before.model_name is None

    request = TaskRequest(task_id="state-test", description="Test state")
    result = loop.start(request)

    state_after = loop.get_current_state()
    assert state_after is not None
    assert state_after.task_id == "state-test"
    assert state_after.phase == ExecutionPhase.COMPLETE
    assert state_after.status is not None
    assert state_after.model_name is None

    state_again = loop.get_current_state()
    assert state_again is not None
    assert state_again.task_id == "state-test"
    assert state_again.phase == ExecutionPhase.COMPLETE
    assert state_again is not state_after

