from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class OpenHandsStatus(Enum):
    IDLE = "idle"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"


class OpenHandsPhase(Enum):
    INITIALIZING = "initializing"
    PLANNING = "planning"
    CODING = "coding"
    BUILDING = "building"
    TESTING = "testing"
    VERIFYING = "verifying"
    COMPLETE = "complete"


@dataclass
class OpenHandsRequest:
    task_id: str
    command: str
    arguments: List[str] = field(default_factory=list)
    working_directory: str = "/workspace"
    timeout: Optional[float] = None
    input_data: Optional[str] = None


@dataclass
class OpenHandsResult:
    task_id: str
    status: OpenHandsStatus
    phase: Optional[OpenHandsPhase] = None
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    elapsed_time: float = 0.0
    error: Optional[str] = None
    timed_out: bool = False
    cancelled: bool = False
    sandbox_status: Optional[str] = None
