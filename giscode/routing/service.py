import logging
from typing import Any, Optional

from giscode.models.manager import ModelManager
from giscode.routing.config import ContextBudget, RoutingConfig
from giscode.routing.context import ContextAssembler
from giscode.routing.errors import ContextAssemblyError, ModelUnavailableError, RoutingError
from giscode.routing.policies import DeterministicRoutingPolicy
from giscode.routing.router import ModelRouter
from giscode.routing.types import (
    ContextBundle,
    FallbackPolicy,
    ModelAvailability,
    ModelRole,
    RoutingRequest,
    RoutingResult,
)


logger = logging.getLogger(__name__)


class RoutingService:
    def __init__(
        self,
        model_manager: ModelManager,
        knowledge_service: Optional[Any] = None,
        config: Optional[RoutingConfig] = None,
    ):
        self._model_manager = model_manager
        self._config = config or RoutingConfig()
        self._router = ModelRouter(model_manager)
        self._context_assembler = None
        if knowledge_service is not None and self._config.enable_context_assembly:
            self._context_assembler = ContextAssembler(
                knowledge_service=knowledge_service,
                context_budget=self._config.context_budget,
            )

    def route(self, request: RoutingRequest) -> RoutingResult:
        return self._router.route(request)

    def assemble_context(
        self,
        routing_result: RoutingResult,
        request: RoutingRequest,
    ) -> ContextBundle:
        if self._context_assembler is None:
            raise ContextAssemblyError("Context assembler is not configured")
        return self._context_assembler.assemble(routing_result, request)

    def route_and_assemble(self, request: RoutingRequest) -> tuple[RoutingResult, ContextBundle]:
        routing_result = self.route(request)
        if routing_result.availability == ModelAvailability.UNAVAILABLE:
            return routing_result, ContextBundle(
                task_id=request.task_id,
                task_text=request.task_description or request.task_type,
                selected_role=routing_result.selected_role,
                selected_model=None,
                namespace=request.namespace or "default",
                project_id=request.project_id,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
                metadata={"availability": "unavailable"},
            )
        try:
            context_bundle = self.assemble_context(routing_result, request)
        except ContextAssemblyError as exc:
            # Knowledge supports planning but is not a prerequisite for
            # receiving and executing a normal task. Preserve the selected
            # model and continue with an explicitly degraded context bundle.
            logger.warning(
                "Knowledge context unavailable for task %s; continuing without retrieved context: %s",
                request.task_id,
                exc,
            )
            context_bundle = ContextBundle(
                task_id=request.task_id,
                task_text=request.task_description or request.task_type,
                selected_role=routing_result.selected_role,
                selected_model=routing_result.selected_model,
                namespace=request.namespace or "default",
                project_id=request.project_id,
                retrieved_records=[],
                formatted_context="",
                record_count=0,
                truncation_occurred=False,
                truncation_reason=None,
                context_char_count=0,
                metadata={
                    "availability": routing_result.availability.value,
                    "context_status": "degraded",
                    "knowledge_retrieval": "unavailable",
                    "knowledge_error": str(exc),
                },
            )
        return routing_result, context_bundle
