from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import threading


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class TaskPhase(Enum):
    IDLE = "idle"
    ANALYZING = "analyzing"
    CODING = "coding"
    VERIFYING = "verifying"
    AUDITING = "auditing"


@dataclass
class TaskState:
    task_id: str
    start_time: Optional[datetime] = None
    elapsed_time: float = 0.0
    max_duration: Optional[float] = None
    current_phase: TaskPhase = TaskPhase.IDLE
    status: TaskStatus = TaskStatus.PENDING
    checkpoints: list = field(default_factory=list)
    completion_criteria: Optional[dict] = None
    failure_info: Optional[dict] = None
    _cancel_event: Optional[threading.Event] = field(default=None, repr=False, compare=False)
    _timer: Optional[threading.Timer] = field(default=None, repr=False, compare=False)
    _restore_required: bool = False
