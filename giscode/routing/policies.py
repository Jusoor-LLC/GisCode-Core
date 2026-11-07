from typing import Optional

from giscode.models.types import ModelMetadata, ModelRegistry
from giscode.routing.errors import ModelUnavailableError, PolicyError
from giscode.routing.types import FallbackPolicy, ModelAvailability, ModelRole, RoutingRequest, RoutingResult


class DeterministicRoutingPolicy:
    TASK_TYPE_ROLE_MAP = {
        "chat": ModelRole.CHAT,
        "intake": ModelRole.CHAT,
        "plan": ModelRole.PLANNING,
        "planning": ModelRole.PLANNING,
        "implement": ModelRole.CODING,
        "implementation": ModelRole.CODING,
        "code": ModelRole.CODING,
        "coding": ModelRole.CODING,
        "ocr": ModelRole.OCR,
        "vision": ModelRole.OCR,
        "general": ModelRole.GENERAL_REASONING,
        "reasoning": ModelRole.GENERAL_REASONING,
    }

    ROLE_FALLBACK_MAP = {
        FallbackPolicy.STRICT: {
            ModelRole.CHAT: ModelRole.CHAT,
            ModelRole.PLANNING: ModelRole.PLANNING,
            ModelRole.CODING: ModelRole.CODING,
            ModelRole.OCR: ModelRole.OCR,
            ModelRole.GENERAL_REASONING: ModelRole.GENERAL_REASONING,
        },
        FallbackPolicy.GENERAL_REASONING: {
            ModelRole.CHAT: ModelRole.GENERAL_REASONING,
            ModelRole.PLANNING: ModelRole.GENERAL_REASONING,
            ModelRole.CODING: ModelRole.GENERAL_REASONING,
            ModelRole.OCR: ModelRole.GENERAL_REASONING,
            ModelRole.GENERAL_REASONING: ModelRole.GENERAL_REASONING,
        },
        FallbackPolicy.CHAT: {
            ModelRole.CHAT: ModelRole.CHAT,
            ModelRole.PLANNING: ModelRole.CHAT,
            ModelRole.CODING: ModelRole.CHAT,
            ModelRole.OCR: ModelRole.CHAT,
            ModelRole.GENERAL_REASONING: ModelRole.CHAT,
        },
    }

    def resolve_role(self, request: RoutingRequest) -> ModelRole:
        if request.requested_role is not None:
            return request.requested_role
        task_type = request.task_type.lower()
        if task_type in self.TASK_TYPE_ROLE_MAP:
            return self.TASK_TYPE_ROLE_MAP[task_type]
        return ModelRole.GENERAL_REASONING

    def find_model_for_role(
        self,
        role: ModelRole,
        registry: ModelRegistry,
        preferred_model: Optional[str] = None,
    ) -> tuple[Optional[ModelMetadata], ModelAvailability]:
        if preferred_model:
            metadata = registry.get(preferred_model)
            if metadata is None:
                # The preferred identity is whatever the runtime reported, which
                # for a GGUF is the file stem and carries a quantization suffix.
                # Resolve it to the canonical registered name before giving up,
                # otherwise a loaded model can never satisfy its own request.
                metadata = registry.resolve_canonical_name(preferred_model)
            if metadata is not None:
                return metadata, ModelAvailability.REGISTERED
            return None, ModelAvailability.UNAVAILABLE

        candidates = registry.list_all()
        role_value = role.value
        for metadata in candidates:
            tags = [t.lower() for t in metadata.tags]
            if role_value in tags or metadata.family.lower() == role_value:
                return metadata, ModelAvailability.REGISTERED

        return None, ModelAvailability.UNAVAILABLE

    def determine_fallback(
        self,
        role: ModelRole,
        policy: FallbackPolicy,
        registry: ModelRegistry,
    ) -> tuple[Optional[ModelRole], Optional[ModelMetadata], ModelAvailability]:
        if policy == FallbackPolicy.STRICT:
            return None, None, ModelAvailability.UNAVAILABLE

        fallback_role = self.ROLE_FALLBACK_MAP.get(policy, {}).get(role, ModelRole.GENERAL_REASONING)
        metadata, availability = self.find_model_for_role(fallback_role, registry)
        if metadata is not None:
            return fallback_role, metadata, availability
        return None, None, ModelAvailability.UNAVAILABLE

    def route(self, request: RoutingRequest, registry: ModelRegistry) -> RoutingResult:
        role = self.resolve_role(request)
        metadata, availability = self.find_model_for_role(role, registry, request.preferred_model)

        if metadata is not None:
            return RoutingResult(
                task_id=request.task_id,
                selected_role=role,
                selected_model=metadata.name,
                availability=availability,
                reason=f"Matched role {role.value} to model {metadata.name}",
                confidence=1.0,
            )

        fallback_role, fallback_metadata, fallback_availability = self.determine_fallback(
            role, request.fallback_policy, registry
        )

        if fallback_metadata is not None:
            return RoutingResult(
                task_id=request.task_id,
                selected_role=fallback_role,
                selected_model=fallback_metadata.name,
                availability=fallback_availability,
                reason=f"Primary role {role.value} unavailable, fell back to {fallback_role.value}",
                confidence=0.5,
                fallback_used=True,
                fallback_reason=f"No model available for {role.value}",
            )

        return RoutingResult(
            task_id=request.task_id,
            selected_role=role,
            selected_model=None,
            availability=ModelAvailability.UNAVAILABLE,
            reason=f"No model available for role {role.value}",
            confidence=0.0,
            fallback_used=False,
            fallback_reason="No fallback model available",
        )
