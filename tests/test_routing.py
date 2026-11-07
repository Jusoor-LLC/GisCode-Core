import sys
import threading
import time
from pathlib import Path

import pytest

from giscode.agentic.loop import CodingAgentLoop
from giscode.config.settings import SettingsManager
from giscode.inference.api import InferenceAPI
from giscode.knowledge.config import KnowledgeStoreConfig
from giscode.knowledge.service import KnowledgeService
from giscode.knowledge.types import KnowledgeRecord, KnowledgeSourceType
from giscode.models.manager import ModelManager
from giscode.models.types import ModelMetadata, ModelRegistry, ModelStatus
from giscode.routing.config import ContextBudget, RoutingConfig
from giscode.routing.context import ContextAssembler
from giscode.routing.errors import (
    ContextAssemblyError,
    InvalidRoleError,
    ModelUnavailableError,
    PolicyError,
    RoutingError,
)
from giscode.routing.policies import DeterministicRoutingPolicy
from giscode.routing.router import ModelRouter
from giscode.routing.service import RoutingService
from giscode.routing.types import (
    ContextBundle,
    FallbackPolicy,
    ModelAvailability,
    ModelRole,
    RoutingRequest,
    RoutingResult,
)
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace


def _make_all_models_feasible(monkeypatch) -> None:
    """Fake the planner verdict for tests about routing/context assembly.

    A catalog entry has no file on disk, so the real feasibility gate (Stage 3F)
    correctly refuses to route it. The gate itself is covered by
    ``tests/test_model_feasibility_selection.py``.
    """
    from giscode.models.feasibility import Feasibility

    def _verdict(self, name, n_ctx=None):
        return Feasibility(
            name=name,
            path="",
            feasible=True,
            category="ok",
            reason="fake: the resource plan fits",
            n_ctx=4096,
            gpu_layers=1,
            plan={"fits": True, "reason": "fake: the resource plan fits"},
        )

    monkeypatch.setattr(ModelManager, "feasibility", _verdict)


def _make_registry() -> ModelRegistry:
    registry = ModelRegistry()
    registry.register(ModelMetadata(name="chat-model", family="chat", path="", tags=["chat"], status=ModelStatus.REGISTERED))
    registry.register(ModelMetadata(name="plan-model", family="plan", path="", tags=["planning"], status=ModelStatus.REGISTERED))
    registry.register(ModelMetadata(name="code-model", family="code", path="", tags=["coding"], status=ModelStatus.REGISTERED))
    registry.register(ModelMetadata(name="ocr-model", family="ocr", path="", tags=["ocr"], status=ModelStatus.REGISTERED))
    registry.register(ModelMetadata(name="general-model", family="general", path="", tags=["general_reasoning"], status=ModelStatus.REGISTERED))
    return registry


def test_routing_request_creation():
    request = RoutingRequest(
        task_id="task-1",
        task_type="chat",
        requested_role=ModelRole.CHAT,
        task_description="Hello",
        namespace="project-a",
        project_id="proj-a",
        preferred_model="chat-model",
        fallback_policy=FallbackPolicy.STRICT,
    )
    assert request.task_id == "task-1"
    assert request.requested_role == ModelRole.CHAT
    assert request.fallback_policy == FallbackPolicy.STRICT


def test_routing_result_creation():
    result = RoutingResult(
        task_id="task-1",
        selected_role=ModelRole.CHAT,
        selected_model="chat-model",
        availability=ModelAvailability.AVAILABLE,
        reason="matched",
        confidence=0.9,
        fallback_used=False,
    )
    assert result.selected_role == ModelRole.CHAT
    assert result.availability == ModelAvailability.AVAILABLE


def test_chat_routing():
    policy = DeterministicRoutingPolicy()
    registry = _make_registry()
    request = RoutingRequest(task_id="t1", task_type="chat")
    result = policy.route(request, registry)
    assert result.selected_role == ModelRole.CHAT
    assert result.selected_model == "chat-model"


def test_planning_routing():
    policy = DeterministicRoutingPolicy()
    registry = _make_registry()
    request = RoutingRequest(task_id="t1", task_type="plan")
    result = policy.route(request, registry)
    assert result.selected_role == ModelRole.PLANNING
    assert result.selected_model == "plan-model"


def test_coding_routing():
    policy = DeterministicRoutingPolicy()
    registry = _make_registry()
    request = RoutingRequest(task_id="t1", task_type="implement")
    result = policy.route(request, registry)
    assert result.selected_role == ModelRole.CODING
    assert result.selected_model == "code-model"


def test_ocr_routing():
    policy = DeterministicRoutingPolicy()
    registry = _make_registry()
    request = RoutingRequest(task_id="t1", task_type="ocr")
    result = policy.route(request, registry)
    assert result.selected_role == ModelRole.OCR
    assert result.selected_model == "ocr-model"


def test_general_routing():
    policy = DeterministicRoutingPolicy()
    registry = _make_registry()
    request = RoutingRequest(task_id="t1", task_type="general")
    result = policy.route(request, registry)
    assert result.selected_role == ModelRole.GENERAL_REASONING
    assert result.selected_model == "general-model"


def test_unavailable_model():
    policy = DeterministicRoutingPolicy()
    registry = ModelRegistry()
    request = RoutingRequest(task_id="t1", task_type="chat")
    result = policy.route(request, registry)
    assert result.availability == ModelAvailability.UNAVAILABLE
    assert result.selected_model is None


def test_explicit_fallback():
    policy = DeterministicRoutingPolicy()
    registry = _make_registry()
    request = RoutingRequest(task_id="t1", task_type="chat", fallback_policy=FallbackPolicy.GENERAL_REASONING)
    registry.remove("chat-model")
    result = policy.route(request, registry)
    assert result.fallback_used is True
    assert result.selected_role == ModelRole.GENERAL_REASONING
    assert result.selected_model == "general-model"


def test_no_silent_fallback():
    policy = DeterministicRoutingPolicy()
    registry = _make_registry()
    request = RoutingRequest(task_id="t1", task_type="chat", fallback_policy=FallbackPolicy.STRICT)
    registry.remove("chat-model")
    result = policy.route(request, registry)
    assert result.fallback_used is False
    assert result.selected_model is None
    assert result.availability == ModelAvailability.UNAVAILABLE


def test_model_availability_detection():
    registry = _make_registry()
    router = ModelRouter(model_manager=None)
    router._registry = registry
    availability = router.get_model_availability("code-model")
    assert availability == ModelAvailability.REGISTERED
    availability = router.get_model_availability("missing-model")
    assert availability == ModelAvailability.UNAVAILABLE


def test_context_assembly(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    knowledge_service.add_record(
        KnowledgeRecord(
            record_id="k1",
            content="python build error fix",
            source="s",
            source_type=KnowledgeSourceType.FIX,
            namespace="project-a",
        )
    )
    assembler = ContextAssembler(knowledge_service=knowledge_service, context_budget=ContextBudget(max_context_chars=100, max_records=10, max_record_chars=100))
    routing_result = RoutingResult(task_id="t1", selected_role=ModelRole.CODING, selected_model="code-model", availability=ModelAvailability.AVAILABLE, reason="ok")
    request = RoutingRequest(task_id="t1", task_type="coding", task_description="python build", namespace="project-a")
    bundle = assembler.assemble(routing_result, request)
    assert bundle.record_count == 1
    assert bundle.selected_model == "code-model"


def test_knowledge_store_integration(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    knowledge_service.add_record(
        KnowledgeRecord(
            record_id="k1",
            content="python error fix",
            source="s",
            source_type=KnowledgeSourceType.FIX,
            namespace="project-a",
        )
    )
    assembler = ContextAssembler(knowledge_service=knowledge_service, context_budget=ContextBudget(max_context_chars=1000, max_records=10, max_record_chars=1000))
    routing_result = RoutingResult(task_id="t1", selected_role=ModelRole.CODING, selected_model="code-model", availability=ModelAvailability.AVAILABLE, reason="ok")
    request = RoutingRequest(task_id="t1", task_type="coding", task_description="python error", namespace="project-a")
    bundle = assembler.assemble(routing_result, request)
    assert len(bundle.retrieved_records) == 1
    assert bundle.retrieved_records[0]["record_id"] == "k1"


def test_namespace_isolation(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    knowledge_service.add_record(
        KnowledgeRecord(
            record_id="k1",
            content="project a secret",
            source="s",
            source_type=KnowledgeSourceType.CODE,
            namespace="project-a",
        )
    )
    knowledge_service.add_record(
        KnowledgeRecord(
            record_id="k2",
            content="project b secret",
            source="s",
            source_type=KnowledgeSourceType.CODE,
            namespace="project-b",
        )
    )
    assembler = ContextAssembler(knowledge_service=knowledge_service, context_budget=ContextBudget(max_context_chars=1000, max_records=10, max_record_chars=1000))
    routing_result = RoutingResult(task_id="t1", selected_role=ModelRole.CHAT, selected_model="chat-model", availability=ModelAvailability.AVAILABLE, reason="ok")
    request = RoutingRequest(task_id="t1", task_type="chat", task_description="secret", namespace="project-a")
    bundle = assembler.assemble(routing_result, request)
    assert all(r["namespace"] == "project-a" for r in bundle.retrieved_records)


def test_project_isolation(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    knowledge_service.add_record(
        KnowledgeRecord(
            record_id="k1",
            content="project a secret",
            source="s",
            source_type=KnowledgeSourceType.CODE,
            namespace="default",
            project_id="proj-a",
        )
    )
    knowledge_service.add_record(
        KnowledgeRecord(
            record_id="k2",
            content="project b secret",
            source="s",
            source_type=KnowledgeSourceType.CODE,
            namespace="default",
            project_id="proj-b",
        )
    )
    assembler = ContextAssembler(knowledge_service=knowledge_service, context_budget=ContextBudget(max_context_chars=1000, max_records=10, max_record_chars=1000))
    routing_result = RoutingResult(task_id="t1", selected_role=ModelRole.CHAT, selected_model="chat-model", availability=ModelAvailability.AVAILABLE, reason="ok")
    request = RoutingRequest(task_id="t1", task_type="chat", task_description="secret", namespace="default", project_id="proj-a")
    bundle = assembler.assemble(routing_result, request)
    assert len(bundle.retrieved_records) == 1
    assert bundle.retrieved_records[0]["record_id"] == "k1"


def test_top_k_limit(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    for i in range(5):
        knowledge_service.add_record(
            KnowledgeRecord(
                record_id=f"k{i}",
                content=f"python code {i}",
                source="s",
                source_type=KnowledgeSourceType.CODE,
                namespace="ns",
            )
        )
    assembler = ContextAssembler(knowledge_service=knowledge_service, context_budget=ContextBudget(max_context_chars=10000, max_records=3, max_record_chars=1000))
    routing_result = RoutingResult(task_id="t1", selected_role=ModelRole.CODING, selected_model="code-model", availability=ModelAvailability.AVAILABLE, reason="ok")
    request = RoutingRequest(task_id="t1", task_type="coding", task_description="python", namespace="ns")
    bundle = assembler.assemble(routing_result, request)
    assert bundle.record_count <= 3


def test_context_size_limit(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    knowledge_service.add_record(
        KnowledgeRecord(
            record_id="k1",
            content="python " + "x" * 5000,
            source="s",
            source_type=KnowledgeSourceType.CODE,
            namespace="ns",
        )
    )
    assembler = ContextAssembler(knowledge_service=knowledge_service, context_budget=ContextBudget(max_context_chars=100, max_records=10, max_record_chars=100))
    routing_result = RoutingResult(task_id="t1", selected_role=ModelRole.CODING, selected_model="code-model", availability=ModelAvailability.AVAILABLE, reason="ok")
    request = RoutingRequest(task_id="t1", task_type="coding", task_description="python", namespace="ns")
    bundle = assembler.assemble(routing_result, request)
    assert bundle.context_char_count <= 100
    assert bundle.truncation_occurred is True


def test_deterministic_truncation(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    for i in range(3):
        knowledge_service.add_record(
            KnowledgeRecord(
                record_id=f"k{i}",
                content=f"python {i} " + "x" * 200,
                source="s",
                source_type=KnowledgeSourceType.CODE,
                namespace="ns",
            )
        )
    assembler = ContextAssembler(knowledge_service=knowledge_service, context_budget=ContextBudget(max_context_chars=150, max_records=10, max_record_chars=100))
    routing_result = RoutingResult(task_id="t1", selected_role=ModelRole.CODING, selected_model="code-model", availability=ModelAvailability.AVAILABLE, reason="ok")
    request = RoutingRequest(task_id="t1", task_type="coding", task_description="python", namespace="ns")
    bundle = assembler.assemble(routing_result, request)
    assert bundle.truncation_occurred is True
    assert bundle.truncation_reason is not None


def test_truncation_reporting(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    knowledge_service.add_record(
        KnowledgeRecord(
            record_id="k1",
            content="python " + "x" * 5000,
            source="s",
            source_type=KnowledgeSourceType.CODE,
            namespace="ns",
        )
    )
    assembler = ContextAssembler(knowledge_service=knowledge_service, context_budget=ContextBudget(max_context_chars=100, max_records=10, max_record_chars=100))
    routing_result = RoutingResult(task_id="t1", selected_role=ModelRole.CODING, selected_model="code-model", availability=ModelAvailability.AVAILABLE, reason="ok")
    request = RoutingRequest(task_id="t1", task_type="coding", task_description="python", namespace="ns")
    bundle = assembler.assemble(routing_result, request)
    assert bundle.truncation_occurred is True
    assert bundle.truncation_reason in {"context_budget_exceeded", "record_truncated"}


def test_route_and_assemble_integration(tmp_path, monkeypatch):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    knowledge_service.add_record(
        KnowledgeRecord(
            record_id="k1",
            content="python build fix",
            source="s",
            source_type=KnowledgeSourceType.FIX,
            namespace="project-a",
        )
    )
    settings_manager = SettingsManager
    settings = settings_manager()
    inference = InferenceAPI()
    model_manager = ModelManager(settings, inference)
    _make_all_models_feasible(monkeypatch)
    routing_service = RoutingService(model_manager=model_manager, knowledge_service=knowledge_service)
    request = RoutingRequest(task_id="t1", task_type="coding", task_description="python build", namespace="project-a")
    routing_result, context_bundle = routing_service.route_and_assemble(request)
    assert routing_result.selected_role == ModelRole.CODING
    assert context_bundle.record_count >= 1


def test_route_and_assemble_degrades_when_knowledge_retrieval_fails():
    class StubModelManager:
        def get_registry(self):
            return _make_registry()

    class FailingKnowledgeService:
        def search(self, **_kwargs):
            raise RuntimeError("database disk image is malformed")

    routing_service = RoutingService(
        model_manager=StubModelManager(),
        knowledge_service=FailingKnowledgeService(),
    )
    request = RoutingRequest(
        task_id="degraded-knowledge",
        task_type="planning",
        requested_role=ModelRole.PLANNING,
        task_description="Inspect the current project",
    )

    routing_result, context_bundle = routing_service.route_and_assemble(request)

    assert routing_result.selected_role == ModelRole.PLANNING
    assert context_bundle.record_count == 0
    assert context_bundle.metadata["context_status"] == "degraded"
    assert context_bundle.metadata["knowledge_retrieval"] == "unavailable"
    assert "database disk image is malformed" in context_bundle.metadata["knowledge_error"]


def test_agentic_integration(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    inference = InferenceAPI()
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    knowledge_service.add_record(
        KnowledgeRecord(
            record_id="k1",
            content="python build fix",
            source="s",
            source_type=KnowledgeSourceType.FIX,
            namespace="project-a",
        )
    )
    settings = SettingsManager()
    model_manager = ModelManager(settings, inference)
    _make_all_models_feasible(monkeypatch)
    routing_service = RoutingService(model_manager=model_manager, knowledge_service=knowledge_service)
    loop = CodingAgentLoop(workspace, inference, task_manager, knowledge_service=knowledge_service, routing_service=routing_service)
    routing_dict, context_dict = loop.route_and_assemble(task_id="t1", task_type="coding", namespace="project-a", task_description="python build")
    assert routing_dict is not None
    assert routing_dict["selected_role"] == "coding"
    assert context_dict is not None
    assert context_dict["record_count"] >= 1


def test_existing_model_manager_inference_api_integration():
    settings = SettingsManager()
    inference = InferenceAPI()
    model_manager = ModelManager(settings, inference)
    routing_service = RoutingService(model_manager=model_manager)
    request = RoutingRequest(task_id="t1", task_type="chat")
    result = routing_service.route(request)
    assert result.selected_model is None or result.availability in {ModelAvailability.REGISTERED, ModelAvailability.UNAVAILABLE}


def test_previous_tasks_remain_intact():
    from giscode.routing.types import ModelRole, RoutingRequest, RoutingResult
    request = RoutingRequest(task_id="t1", task_type="chat")
    assert request.task_id == "t1"
    result = RoutingResult(task_id="t1", selected_role=ModelRole.CHAT, selected_model=None, availability=ModelAvailability.UNAVAILABLE, reason="none")
    assert result.task_id == "t1"


def test_context_assembly_without_knowledge_service():
    assembler = ContextAssembler(knowledge_service=None)
    with pytest.raises(ContextAssemblyError):
        assembler.assemble(
            RoutingResult(task_id="t1", selected_role=ModelRole.CHAT, selected_model=None, availability=ModelAvailability.UNAVAILABLE, reason="none"),
            RoutingRequest(task_id="t1", task_type="chat"),
        )


def test_fallback_policy_strict_no_silent_substitution():
    policy = DeterministicRoutingPolicy()
    registry = _make_registry()
    registry.remove("chat-model")
    request = RoutingRequest(task_id="t1", task_type="chat", fallback_policy=FallbackPolicy.STRICT)
    result = policy.route(request, registry)
    assert result.fallback_used is False
    assert result.selected_model is None


def test_fallback_policy_general_reasoning():
    policy = DeterministicRoutingPolicy()
    registry = _make_registry()
    registry.remove("chat-model")
    request = RoutingRequest(task_id="t1", task_type="chat", fallback_policy=FallbackPolicy.GENERAL_REASONING)
    result = policy.route(request, registry)
    assert result.fallback_used is True
    assert result.selected_role == ModelRole.GENERAL_REASONING
    assert result.selected_model == "general-model"


def test_route_and_assemble_unavailable_model():
    settings_manager = pytest.importorskip("giscode.config.settings").SettingsManager
    settings = settings_manager()
    inference = InferenceAPI()
    model_manager = ModelManager(settings, inference)
    routing_service = RoutingService(model_manager=model_manager)
    request = RoutingRequest(task_id="t1", task_type="unknown")
    routing_result, context_bundle = routing_service.route_and_assemble(request)
    assert routing_result.availability == ModelAvailability.UNAVAILABLE
    assert context_bundle.record_count == 0


def test_context_bundle_fields():
    bundle = ContextBundle(
        task_id="t1",
        task_text="hello",
        selected_role=ModelRole.CHAT,
        selected_model="chat-model",
        namespace="ns",
        project_id="proj",
        retrieved_records=[],
        formatted_context="",
        record_count=0,
        truncation_occurred=False,
        truncation_reason=None,
        context_char_count=0,
    )
    assert bundle.task_id == "t1"
    assert bundle.namespace == "ns"
    assert bundle.project_id == "proj"


def test_model_role_values():
    assert ModelRole.CHAT.value == "chat"
    assert ModelRole.PLANNING.value == "planning"
    assert ModelRole.CODING.value == "coding"
    assert ModelRole.OCR.value == "ocr"
    assert ModelRole.GENERAL_REASONING.value == "general_reasoning"


def test_availability_values():
    assert ModelAvailability.REGISTERED.value == "registered"
    assert ModelAvailability.AVAILABLE.value == "available"
    assert ModelAvailability.LOADED.value == "loaded"
    assert ModelAvailability.UNAVAILABLE.value == "unavailable"
