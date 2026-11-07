from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class VisionStatus(Enum):
    IDLE = "idle"
    LOADING = "loading"
    ANALYZING = "analyzing"
    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    RELEASING = "releasing"


@dataclass
class VisionRequest:
    image_paths: list[str]
    prompt: str
    request_id: str
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    max_tokens: int = 512


@dataclass
class VisionResult:
    status: VisionStatus
    request_id: str
    text: str = ""
    model_used: Optional[str] = None
    model_swapped: bool = False
    error: Optional[str] = None
    elapsed: float = 0.0
    metadata: dict = field(default_factory=dict)