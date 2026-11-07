from typing import Any, Optional

from giscode.models.feasibility import is_coding_capable
from giscode.models.manager import ModelManager
from giscode.models.types import ModelRegistry
from giscode.routing.errors import ModelUnavailableError, PolicyError
from giscode.routing.policies import DeterministicRoutingPolicy
from giscode.routing.types import (
    FallbackPolicy,
    ModelAvailability,
    ModelRole,
    RoutingRequest,
    RoutingResult,
)



class ModelRouter:
    def __init__(
        self,
        model_manager: Optional[ModelManager] = None,
        policy: Optional[DeterministicRoutingPolicy] = None,
        registry: Optional[ModelRegistry] = None,
    ):
        self._model_manager = model_manager
        self._policy = policy or DeterministicRoutingPolicy()
        self._registry = registry

    @property
    def registry(self) -> ModelRegistry:
        if self._registry is not None:
            return self._registry
        if self._model_manager is not None:
            return self._model_manager.get_registry()
        return ModelRegistry()

    def route(self, request: RoutingRequest) -> RoutingResult:
        result = self._policy.route(request, self.registry)
        return self._reject_infeasible(request, result)

    # ------------------------------------------------------------------
    # feasibility gate
    # ------------------------------------------------------------------
    def _feasibility_for(self, name: str):
        """The manager's feasibility verdict, or None when it cannot answer.

        A manager without the method (fakes, registry-only routers) leaves the
        previous behaviour untouched.
        """
        checker = getattr(self._model_manager, "feasibility", None)
        if not callable(checker):
            return None
        try:
            return checker(name)
        except Exception:
            return None

    @staticmethod
    def _matches_role(metadata, role: ModelRole) -> bool:
        tags = [str(tag).lower() for tag in getattr(metadata, "tags", []) or []]
        family = str(getattr(metadata, "family", "") or "").lower()
        return role.value in tags or family == role.value

    def _coding_candidates(self) -> list:
        """Registry models that declare coding capability (the only evidence used)."""
        return [item for item in self.registry.list_all() if is_coding_capable(item)]

    def _coding_evidence(self, name: str) -> dict:
        """How strongly *name*'s coding capability is known.

        ``unrecorded`` means the model was registered without capability
        evidence - older code paths, or a hand-registered entry - and must not
        be described as verified.
        """
        metadata = self.registry.get(name)
        if metadata is None:
            return {"level": "unrecorded", "source": "", "verified": False}
        level = metadata.evidence_level("coding") if hasattr(metadata, "evidence_level") else "unrecorded"
        record = (getattr(metadata, "capability_evidence", {}) or {}).get("coding") or {}
        return {
            "level": level,
            "source": str(record.get("source", "") or ""),
            "verified": bool(record.get("verified", False)),
        }

    def _no_coding_reason(self) -> str:
        """Honest explanation when no coding-capable model can be used."""
        candidates = self._coding_candidates()
        if not candidates:
            return (
                "no model declares coding capability; a generic text model is not "
                "substituted for a coding task"
            )
        details = []
        for candidate in candidates[:3]:
            verdict = self._feasibility_for(candidate.name)
            if verdict is None:
                continue
            details.append(f"'{candidate.name}' cannot be loaded: {verdict.label} - {verdict.reason}")
        if not details:
            return "no coding-capable model can be verified"
        return "no coding-capable model can be loaded (" + "; ".join(details) + ")"

    def _first_feasible(self, role: ModelRole, exclude: str):
        """Another model of the same role that the planner says will load."""
        for candidate in self.registry.list_all():
            if candidate.name == exclude:
                continue
            if role is ModelRole.CODING:
                # Coding capability is the evidence; a filename is not.
                if not is_coding_capable(candidate):
                    continue
            elif not self._matches_role(candidate, role):
                continue
            verdict = self._feasibility_for(candidate.name)
            if verdict is not None and getattr(verdict, "feasible", False):
                return candidate, verdict
        return None, None

    def _reject_infeasible(self, request: RoutingRequest, result: RoutingResult) -> RoutingResult:
        """Never hand back a model the loader would refuse.

        An explicit ``preferred_model`` is refused rather than replaced: the
        user asked for that model. A role-level choice may move to another model
        of the same role that the planner accepts; if there is none, the honest
        answer is "unavailable" with the planner's reason - never a silently
        substituted model, and never a vision-only model for coding.
        """
        if result.selected_model is None:
            return result
        metadata = dict(result.metadata or {})
        coding_request = self._policy.resolve_role(request) is ModelRole.CODING
        if coding_request:
            # Record *how* coding capability is known for the model being
            # handed back: a catalog declaration and a file-name guess are very
            # different claims, and the caller must be able to say which it got.
            metadata["coding_evidence"] = self._coding_evidence(result.selected_model)

        # A coding task is never answered by a model that does not declare
        # coding capability - including when the policy itself crossed into a
        # general role because no coding model matched its tags.
        if coding_request:
            selected = self.registry.get(result.selected_model)
            if selected is None or not is_coding_capable(selected):
                if request.preferred_model:
                    return RoutingResult(
                        task_id=result.task_id,
                        selected_role=result.selected_role,
                        selected_model=None,
                        availability=ModelAvailability.UNAVAILABLE,
                        reason=(
                            f"'{result.selected_model}' does not declare coding capability; "
                            "a generic model is not used for a coding task (explicitly requested)"
                        ),
                        confidence=0.0,
                        fallback_used=False,
                        fallback_reason="not coding-capable",
                        metadata=metadata,
                    )
                alternative, alternative_verdict = self._first_feasible(
                    ModelRole.CODING, exclude=result.selected_model
                )
                if alternative is not None:
                    metadata["feasibility"] = alternative_verdict.as_dict()
                    metadata["coding_evidence"] = self._coding_evidence(alternative.name)
                    return RoutingResult(
                        task_id=result.task_id,
                        selected_role=ModelRole.CODING,
                        selected_model=alternative.name,
                        availability=ModelAvailability.REGISTERED,
                        reason=(
                            f"'{result.selected_model}' does not declare coding capability; "
                            f"using '{alternative.name}' instead"
                        ),
                        confidence=result.confidence,
                        fallback_used=True,
                        fallback_reason="the matched model is not coding-capable",
                        metadata=metadata,
                    )
                return RoutingResult(
                    task_id=result.task_id,
                    selected_role=ModelRole.CODING,
                    selected_model=None,
                    availability=ModelAvailability.UNAVAILABLE,
                    reason=self._no_coding_reason(),
                    confidence=0.0,
                    fallback_used=False,
                    fallback_reason="no coding-capable model is available",
                    metadata=metadata,
                )

        verdict = self._feasibility_for(result.selected_model)
        if verdict is None:
            return result
        if getattr(verdict, "feasible", True):
            metadata["feasibility"] = verdict.as_dict()
            return RoutingResult(
                task_id=result.task_id,
                selected_role=result.selected_role,
                selected_model=result.selected_model,
                availability=result.availability,
                reason=result.reason,
                confidence=result.confidence,
                fallback_used=result.fallback_used,
                fallback_reason=result.fallback_reason,
                metadata=metadata,
            )

        rejected = metadata.get("rejected_models") or []
        rejected = [*rejected, verdict.as_dict()]

        if request.preferred_model:
            return RoutingResult(
                task_id=result.task_id,
                selected_role=result.selected_role,
                selected_model=None,
                availability=ModelAvailability.UNAVAILABLE,
                reason=f"{verdict.describe()} (explicitly requested)",
                confidence=0.0,
                fallback_used=False,
                fallback_reason=verdict.reason,
                metadata={**metadata, "rejected_models": rejected},
            )

        alternative, alternative_verdict = self._first_feasible(
            result.selected_role, exclude=result.selected_model
        )
        if alternative is not None:
            metadata["feasibility"] = alternative_verdict.as_dict()
            return RoutingResult(
                task_id=result.task_id,
                selected_role=result.selected_role,
                selected_model=alternative.name,
                availability=ModelAvailability.REGISTERED,
                reason=f"{verdict.describe()}; using '{alternative.name}' instead",
                confidence=result.confidence,
                fallback_used=True,
                fallback_reason=f"'{result.selected_model}' is {verdict.label}",
                metadata={**metadata, "rejected_models": rejected},
            )

        role = result.selected_role.value if result.selected_role else "suitable"
        return RoutingResult(
            task_id=result.task_id,
            selected_role=result.selected_role,
            selected_model=None,
            availability=ModelAvailability.UNAVAILABLE,
            reason=(
                f"{verdict.describe()}; no other {role} model can be loaded"
            ),
            confidence=0.0,
            fallback_used=False,
            fallback_reason=verdict.reason,
            metadata={**metadata, "rejected_models": rejected},
        )

    def get_model_availability(self, model_name: str) -> ModelAvailability:
        if self._model_manager is not None:
            metadata = self._model_manager.get_model_status(model_name)
            if metadata is not None:
                return self._map_status(metadata.status)
            return ModelAvailability.UNAVAILABLE
        if self._registry is not None:
            metadata = self._registry.get(model_name)
            if metadata is not None:
                return self._map_status(metadata.status)
        return ModelAvailability.UNAVAILABLE

    @staticmethod
    def _map_status(status: Any) -> ModelAvailability:
        mapping = {
            "registered": ModelAvailability.REGISTERED,
            "loading": ModelAvailability.AVAILABLE,
            "ready": ModelAvailability.LOADED,
            "busy": ModelAvailability.LOADED,
            "unloading": ModelAvailability.AVAILABLE,
            "error": ModelAvailability.UNAVAILABLE,
            "cancelled": ModelAvailability.UNAVAILABLE,
            "unknown": ModelAvailability.UNAVAILABLE,
        }
        return mapping.get(status.value if hasattr(status, "value") else str(status), ModelAvailability.UNAVAILABLE)
