"""Model lifecycle across a task: no mid-task unload of the shared model.

J8's symptom was a mid-task reset: planning unloaded the (single) local model
right after creating the plan, so a repair planner probe inside the same task
saw no model loaded and the run died with "repair planning failure: ... no
model loaded". These tests pin the fixed contract:

* one model for both roles stays loaded from planning through implementation;
* a two-model setup still releases the planning model before the coding model
  loads;
* planning failure still frees the model it loaded;
* a selected coding model that is not resident is loaded (or the task fails
  with a clear load error) instead of running blind with no model.
"""

from giscode.agentic.loop import CodingAgentLoop
from giscode.agentic.planner import MockPlanner, PlanningError
from giscode.agentic.types import (
    ActionType,
    ExecutionPlan,
    ExecutionStep,
    ExecutionPhase,
    TaskRequest,
)
from giscode.inference.api import InferenceAPI
from giscode.inference.lifecycle import ModelInfo, ModelState
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.models.types import ModelMetadata, ModelStatus
from giscode.routing.types import (
    ContextBundle,
    ModelAvailability,
    ModelRole,
    RoutingResult,
)
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace


class FakeModelManager:
    def __init__(self, inference, registry=None):
        self._inference = inference
        self._loaded = None
        self.load_calls = []
        self.unload_calls = []
        self._registry = registry or {
            "solo-model": ModelMetadata(
                name="solo-model",
                family="coding",
                path="/models/solo-model.gguf",
                status=ModelStatus.READY,
            ),
            "plan-model": ModelMetadata(
                name="plan-model",
                family="planning",
                path="/models/plan-model.gguf",
                status=ModelStatus.READY,
            ),
            "code-model": ModelMetadata(
                name="code-model",
                family="coding",
                path="/models/code-model.gguf",
                status=ModelStatus.READY,
            ),
        }

    def load_model(self, name):
        if name not in self._registry:
            raise KeyError(f"model not registered: {name}")
        self.load_calls.append(name)
        self._loaded = name
        info = ModelInfo(
            name=name,
            path=f"/models/{name}.gguf",
            state=ModelState.READY,
            size_bytes=0,
            context_length=4096,
        )
        self._inference._model = info
        runtime = getattr(self._inference, "_runtime", None)
        if runtime is not None and hasattr(runtime, "start_server"):
            try:
                runtime.start_server(info.path)
            except Exception:
                pass

    def unload_model(self, name):
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

    def get_model_status(self, name):
        return self._registry.get(name)

    def get_current_model(self):
        return self._loaded

    def auto_register_discovered(self):
        return []


class HealthGatePlanner(MockPlanner):
    """Refuses to plan without a loaded model, as the production planner does."""

    def __init__(self, inference):
        self._inference = inference

    def create_plan(self, request):
        if not self._inference.get_health().get("model_loaded"):
            raise PlanningError("no model is loaded for planning")
        return ExecutionPlan(
            objective=request.description or "noop",
            assumptions=[],
            files_expected_to_change=["src/app.py"],
            steps=[
                ExecutionStep("s1", "create", ActionType.CREATE, "src/app.py", "# x\n")
            ],
            verification_steps=[],
        )


def _routing(role_to_model):
    class Routing:
        def __init__(self):
            self._model_manager = None

        def route_and_assemble(self, request):
            selected = role_to_model[request.requested_role]
            return (
                RoutingResult(
                    task_id=request.task_id,
                    selected_role=request.requested_role,
                    selected_model=selected,
                    availability=(
                        ModelAvailability.UNAVAILABLE
                        if selected is None
                        else ModelAvailability.AVAILABLE
                    ),
                    reason="test",
                ),
                ContextBundle(
                    task_id=request.task_id,
                    task_text=request.task_description or "",
                    selected_role=request.requested_role,
                    selected_model=selected,
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

    return Routing()


def _build(tmp_path, role_to_model, registry=None):
    workspace = Workspace(tmp_path / "ws")
    workspace.write("src", "app.py", content="# original\n")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    model_manager = FakeModelManager(inference, registry=registry)
    routing = _routing(role_to_model)
    routing._model_manager = model_manager
    planner = HealthGatePlanner(inference=inference)
    loop = CodingAgentLoop(
        workspace,
        inference,
        task_manager,
        planner=planner,
        routing_service=routing,
    )
    return loop, inference, task_manager, model_manager, planner


def _start_task(loop, task_manager, task_id="t1"):
    request = TaskRequest(task_id=task_id, description="edit src/app.py")
    task_manager.start_task(task_id, max_duration=None, completion_criteria=None)
    result = loop.submit(request)
    return request, result


def test_single_model_stays_loaded_for_the_whole_task(tmp_path):
    loop, inference, task_manager, model_manager, planner = _build(
        tmp_path,
        {
            ModelRole.PLANNING: "solo-model",
            ModelRole.CODING: "solo-model",
        },
    )
    request, result = _start_task(loop, task_manager)

    result = loop._run_planning(request, result)

    # The mid-task window: planning must not have reset the shared model.
    assert inference.get_health()["model_loaded"] is True
    # A repair planner probe inside the same task must see a loaded model.
    assert planner.create_plan(
        TaskRequest(task_id="t1-repair", description="repair")
    ).steps

    loop._run_implementation(result)

    assert model_manager.load_calls == ["solo-model"]
    assert model_manager.unload_calls == []
    assert inference.get_health()["model_loaded"] is True


def test_two_model_setup_still_releases_planning_model(tmp_path):
    loop, inference, task_manager, model_manager, _ = _build(
        tmp_path,
        {
            ModelRole.PLANNING: "plan-model",
            ModelRole.CODING: "code-model",
        },
    )
    request, result = _start_task(loop, task_manager)

    loop._run_planning(request, result)
    loop._run_implementation(result)

    assert model_manager.load_calls == ["plan-model", "code-model"]
    assert model_manager.unload_calls == ["plan-model"]


def test_planning_failure_still_frees_the_model_it_loaded(tmp_path):
    class FailingPlanner(MockPlanner):
        def __init__(self, inference):
            self._inference = inference

        def create_plan(self, request):
            raise PlanningError("planning failed")

    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    model_manager = FakeModelManager(inference)
    routing = _routing({ModelRole.PLANNING: "plan-model"})
    routing._model_manager = model_manager
    loop = CodingAgentLoop(
        workspace,
        inference,
        task_manager,
        planner=FailingPlanner(inference=inference),
        routing_service=routing,
    )
    request = TaskRequest(task_id="t1", description="anything")
    task_manager.start_task("t1", max_duration=None, completion_criteria=None)
    result = loop.submit(request)

    result = loop._run_planning(request, result)

    assert result.status == ExecutionPhase.FAILED
    assert model_manager.load_calls == ["plan-model"]
    assert model_manager.unload_calls == ["plan-model"]


def test_missing_coding_model_fails_fast_instead_of_running_blind(tmp_path):
    loop, inference, task_manager, model_manager, _ = _build(
        tmp_path,
        {ModelRole.CODING: "ghost-model"},
        registry={},
    )
    request, result = _start_task(loop, task_manager)
    result.plan = ExecutionPlan(
        objective="noop",
        assumptions=[],
        files_expected_to_change=["src/app.py"],
        steps=[
            ExecutionStep("s1", "create", ActionType.CREATE, "src/app.py", "# x\n")
        ],
        verification_steps=[],
    )

    result = loop._run_implementation(result)

    assert result.status == ExecutionPhase.FAILED
    assert "implementation failure: model load failed" in (result.error or "")
    assert model_manager.load_calls == []
