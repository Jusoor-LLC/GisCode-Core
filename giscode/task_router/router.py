from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from giscode.models.manager import ModelManager
from giscode.task_router.types import (
    CapabilityPlan,
    CapabilityType,
    IntentCategory,
    RouterDecision,
    TaskIntent,
)
from giscode.vision.capabilities import find_vision_model
from giscode.vision.service import VisionService


class CapabilityRouter:
    def __init__(self, model_manager: ModelManager):
        self._model_manager = model_manager
        self._vision_service: Optional[VisionService] = None

    def set_vision_service(self, vision_service: VisionService) -> None:
        self._vision_service = vision_service

    def plan_capabilities(self, intent: TaskIntent, available: list[str]) -> CapabilityPlan:
        required = intent.required_capabilities
        available_set = set(available)
        required_set = set(required)
        missing = [c for c in required if c.value not in available_set]

        load_order = []
        for cap in required:
            if cap.value not in available_set:
                load_order.append(cap)
                available_set.add(cap.value)

        return CapabilityPlan(
            required=required,
            available=list(required_set - set(missing)),
            missing=missing,
            load_order=load_order,
        )

    def select_model_for_capability(self, cap: CapabilityType) -> Optional[str]:
        if cap == CapabilityType.VISION:
            vision_meta = find_vision_model(self._model_manager.get_registry())
            return vision_meta.name if vision_meta else None
        return None

    def check_vision_available(self) -> bool:
        if self._vision_service:
            return self._vision_service.is_available()
        return find_vision_model(self._model_manager.get_registry()) is not None

    def route(self, intent: TaskIntent, project_active: bool = False) -> RouterDecision:
        capabilities_map = {
            IntentCategory.VISION_ANALYSIS: (CapabilityType.VISION, ["Vision"], ["Vision analysis"]),
            IntentCategory.UI_ANALYSIS: (CapabilityType.VISION, ["Vision", "Coding"], ["Vision analysis", "Identify UI code"]),
            IntentCategory.DEBUGGING: (CapabilityType.CODING, ["Coding", "Vision" if "vision" in [c.value for c in intent.required_capabilities] else "ProjectInspection"], ["Analyze error", "Propose fix", "Write tests"]),
            IntentCategory.CODE_GENERATION: (CapabilityType.CODING, ["Coding", "Vision" if "vision" in [c.value for c in intent.required_capabilities] else "ProjectInspection"], ["Analyze requirements", "Generate code", "Write tests"]),
            IntentCategory.TASK_PLANNING: (CapabilityType.GENERAL_REASONING, ["Planning"], ["Create plan"]),
            IntentCategory.PROJECT_INSPECTION: (CapabilityType.PROJECT_INSPECTION, ["ProjectInspection"], ["Inspect project structure"]),
            IntentCategory.GENERAL_CHAT: (CapabilityType.GENERAL_REASONING, ["Chat"], ["General response"]),
        }

        primary, delegated, plan = capabilities_map.get(intent.category, (CapabilityType.GENERAL_REASONING, ["Chat"], ["General response"]))

        capabilities = intent.required_capabilities or [primary]

        return RouterDecision(
            primary_capability=primary,
            capabilities=capabilities,
            delegated_agents=delegated,
            plan=plan,
            confidence=intent.confidence,
            reasoning=f"Intent: {intent.category.value} (confidence: {intent.confidence:.2f})",
        )