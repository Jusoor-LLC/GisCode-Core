from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class CapabilityType(Enum):
    GENERAL_REASONING = "general_reasoning"
    CODING = "coding"
    VISION = "vision"
    OCR = "ocr"
    PROJECT_INSPECTION = "project_inspection"
    WEB_RESEARCH = "web_research"
    TERMINAL = "terminal"
    TESTING = "testing"
    BUILD = "build"
    FILE_ANALYSIS = "file_analysis"


class TaskState(Enum):
    PENDING = "pending"
    WAITING_FOR_INPUT = "waiting_for_input"
    ROUTING = "routing"
    LOADING_CAPABILITY = "loading_capability"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IntentCategory(Enum):
    VISION_ANALYSIS = "vision_analysis"
    UI_ANALYSIS = "ui_analysis"
    CODE_GENERATION = "code_generation"
    DEBUGGING = "debugging"
    PROJECT_INSPECTION = "project_inspection"
    GENERAL_CHAT = "general_chat"
    TASK_PLANNING = "task_planning"
    UNKNOWN = "unknown"


# ``Attachment`` lives in core; re-exported so every path is one class object.
from giscode.agentic.attachments import Attachment


@dataclass
class TaskIntent:
    category: IntentCategory
    confidence: float
    raw_text: str
    required_capabilities: list[CapabilityType] = field(default_factory=list)
    suggested_plan: list[str] = field(default_factory=list)


@dataclass
class CapabilityPlan:
    required: list[CapabilityType]
    available: list[CapabilityType]
    missing: list[CapabilityType]
    load_order: list[CapabilityType]
    estimated_load_time: float = 0.0


@dataclass
class TaskMemory:
    task_id: str
    project_id: str
    section: str
    state: TaskState = TaskState.PENDING
    intent: Optional[TaskIntent] = None
    pending_text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    capability_plan: Optional[CapabilityPlan] = None
    current_capability: Optional[CapabilityType] = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    result: Optional[Any] = None
    error: Optional[str] = None

    def add_attachment(self, attachment: Attachment) -> None:
        self.attachments.append(attachment)
        self.updated_at = datetime.now()

    def set_intent(self, intent: TaskIntent) -> None:
        self.intent = intent
        self.updated_at = datetime.now()


@dataclass
class RouterDecision:
    primary_capability: CapabilityType
    capabilities: list[CapabilityType]
    delegated_agents: list[str]
    plan: list[str]
    confidence: float
    reasoning: str