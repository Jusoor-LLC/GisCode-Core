from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class CommandStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass
class CommandRequest:
    command: str
    arguments: List[str] = field(default_factory=list)
    working_directory: Optional[str] = None
    timeout: Optional[float] = None
    environment: Optional[dict] = None
    capture_output: bool = True


@dataclass
class CommandResult:
    command: str
    arguments: List[str] = field(default_factory=list)
    working_directory: str = ""
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    elapsed_time: float = 0.0
    status: CommandStatus = CommandStatus.FAILED
    timed_out: bool = False
    cancelled: bool = False
    error: Optional[str] = None


@dataclass
class ExecutorConfig:
    default_timeout: float = 60.0
    # Hard ceiling for one command, whatever a plan step, the model or a
    # caller asks for through CommandRequest.timeout: a command must never
    # outlive the task that started it. An installation can raise it by
    # passing its own ExecutorConfig where the executor is built
    # (giscode/main.py); the verifier keeps its own smaller budgets (120 s for
    # Python tests, 600 s for Gradle).
    max_timeout: float = 600.0
    max_stdout_bytes: int = 1024 * 1024  # 1 MB
    max_stderr_bytes: int = 1024 * 1024  # 1 MB
