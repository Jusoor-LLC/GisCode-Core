from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ExecutionPhase(Enum):
    INTAKE = "intake"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class ActionType(Enum):
    CREATE = "create"
    MODIFY = "modify"
    VERIFY = "verify"
    REPAIR = "repair"
    READ = "read"
    RUN_COMMAND = "run_command"


@dataclass
class TaskRequest:
    task_id: str
    description: str
    max_duration: Optional[float] = None
    completion_criteria: Optional[dict] = None


@dataclass
class ExecutionStep:
    step_id: str
    description: str
    action_type: ActionType
    target_path: str
    content: Optional[str] = None
    expected_result: Optional[str] = None


@dataclass
class ExecutionPlan:
    objective: str
    assumptions: list[str] = field(default_factory=list)
    files_expected_to_change: list[str] = field(default_factory=list)
    steps: list[ExecutionStep] = field(default_factory=list)
    verification_steps: list[str] = field(default_factory=list)


@dataclass
class ActionResult:
    action_type: ActionType
    target_path: str
    success: bool
    message: str = ""
    error: Optional[str] = None


@dataclass
class VerificationResult:
    success: bool
    errors: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    test_execution_status: str = "unknown"  # "passed", "failed", "unavailable", "unknown"
    # Baseline-comparison facts, kept structured because callers (graded
    # learning, reports) must not have to parse the human-readable detail
    # lines. Defaults describe "no baseline evidence", so every existing
    # constructor, dict conversion and consumer behaves exactly as before.
    baseline_available: bool = False
    newly_passing: list[str] = field(default_factory=list)  # failed before, passing after
    tests_before: str = "unknown"  # baseline classification status
    tests_after: str = "unknown"   # post-edit classification status
    # Per-test outcomes (node id -> "passed"/"failed"/"error"/"skipped"),
    # passed through from the runner's classification. Empty means "no
    # per-test evidence", which callers must read as unknown rather than as a
    # clean run; the default keeps every legacy constructor working.
    per_test: dict[str, str] = field(default_factory=dict)


@dataclass
class TaskResult:
    task_id: str
    status: ExecutionPhase
    plan: Optional[ExecutionPlan] = None
    actions: list[ActionResult] = field(default_factory=list)
    verification: Optional[VerificationResult] = None
    packaging: Optional[dict] = None
    error: Optional[str] = None
    failure_info: Optional[dict] = None
    response: Optional[str] = None
    inference_error: Optional[str] = None


@dataclass
class LoopState:
    task_id: Optional[str]
    phase: Optional[ExecutionPhase]
    status: Optional[str]
    model_name: Optional[str]
