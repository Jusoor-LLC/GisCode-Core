from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ModelRole(Enum):
    CHAT = "chat"
    PLANNING = "planning"
    CODING = "coding"
    OCR = "ocr"
    GENERAL_REASONING = "general_reasoning"


class ModelAvailability(Enum):
    REGISTERED = "registered"
    AVAILABLE = "available"
    LOADED = "loaded"
    UNAVAILABLE = "unavailable"


class FallbackPolicy(Enum):
    STRICT = "strict"
    GENERAL_REASONING = "general_reasoning"
    CHAT = "chat"


@dataclass
class RoutingRequest:
    task_id: str
    task_type: str
    requested_role: Optional[ModelRole] = None
    task_description: Optional[str] = None
    namespace: Optional[str] = None
    project_id: Optional[str] = None
    preferred_model: Optional[str] = None
    fallback_policy: FallbackPolicy = FallbackPolicy.STRICT
    context_requirements: Optional[dict] = None


@dataclass
class RoutingResult:
    task_id: str
    selected_role: Optional[ModelRole]
    selected_model: Optional[str]
    availability: ModelAvailability
    reason: str
    confidence: float = 0.0
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ContextBundle:
    task_id: str
    task_text: str
    selected_role: Optional[ModelRole]
    selected_model: Optional[str]
    namespace: str
    project_id: Optional[str]
    retrieved_records: list[dict] = field(default_factory=list)
    formatted_context: str = ""
    record_count: int = 0
    truncation_occurred: bool = False
    truncation_reason: Optional[str] = None
    context_char_count: int = 0
    metadata: dict = field(default_factory=dict)
