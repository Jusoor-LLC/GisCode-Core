from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class OrchestrationPhase(Enum):
    IDLE = "idle"
    BUILDING = "building"
    TESTING = "testing"
    FIXING = "fixing"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    ESCALATED = "escalated"


class BuildStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"


class TestStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"


@dataclass
class BuildRequest:
    command: str
    arguments: List[str] = field(default_factory=list)
    working_directory: str = "/workspace"
    timeout: Optional[float] = None
    environment: Optional[dict] = None


@dataclass
class BuildResult:
    command: str
    arguments: List[str] = field(default_factory=list)
    working_directory: str = "/workspace"
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    elapsed_time: float = 0.0
    status: BuildStatus = BuildStatus.FAILED
    timed_out: bool = False
    cancelled: bool = False
    error: Optional[str] = None
    sandbox_status: Optional[str] = None


@dataclass
class TestRequest:
    command: str
    arguments: List[str] = field(default_factory=list)
    working_directory: str = "/workspace"
    timeout: Optional[float] = None
    environment: Optional[dict] = None


@dataclass
class TestResult:
    command: str
    arguments: List[str] = field(default_factory=list)
    working_directory: str = "/workspace"
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    elapsed_time: float = 0.0
    passed: int = 0
    failed: int = 0
    status: TestStatus = TestStatus.FAILED
    timed_out: bool = False
    cancelled: bool = False
    error: Optional[str] = None
    sandbox_status: Optional[str] = None


@dataclass
class FailureRecord:
    failure_type: str
    command: str
    exit_code: int = -1
    stderr: str = ""
    stdout: str = ""
    summary: str = ""
    relevant_path: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    attempt_number: int = 1
    fingerprint: str = ""


@dataclass
class FixAttempt:
    attempt_number: int
    failure: FailureRecord
    fix_proposal: str
    files_changed: List[str] = field(default_factory=list)
    applied: bool = False
    build_result: Optional[BuildResult] = None
    test_result: Optional[TestResult] = None
    success: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class OrchestrationResult:
    task_id: str
    status: OrchestrationPhase
    build_result: Optional[BuildResult] = None
    test_result: Optional[TestResult] = None
    attempts: List[FixAttempt] = field(default_factory=list)
    current_attempt: int = 0
    max_attempts: int = 5
    error: Optional[str] = None
    failure_info: Optional[dict] = None
    checkpoint_label: Optional[str] = None
