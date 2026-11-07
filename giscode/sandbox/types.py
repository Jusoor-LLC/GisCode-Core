from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class SandboxStatus(Enum):
    IDLE = "idle"
    CREATING = "creating"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    DESTROYED = "destroyed"
    UNAVAILABLE = "unavailable"


class SandboxBackend(Enum):
    DOCKER = "docker"
    UNAVAILABLE = "unavailable"


@dataclass
class SandboxConfig:
    backend: SandboxBackend = SandboxBackend.UNAVAILABLE
    image: Optional[str] = None
    network_enabled: bool = False
    memory_limit: Optional[str] = None
    cpu_limit: Optional[float] = None
    default_timeout: float = 60.0
    workspace_mount_path: str = "/workspace"
    remove_after_execution: bool = True


@dataclass
class SandboxRequest:
    command: str
    arguments: List[str] = field(default_factory=list)
    working_directory: str = "/workspace"
    timeout: Optional[float] = None
    environment: Optional[dict] = None
    network_enabled: Optional[bool] = None
    input_data: Optional[str] = None


@dataclass
class SandboxResult:
    sandbox_id: str
    backend: SandboxBackend
    command: str
    arguments: List[str] = field(default_factory=list)
    working_directory: str = "/workspace"
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    elapsed_time: float = 0.0
    status: SandboxStatus = SandboxStatus.FAILED
    timed_out: bool = False
    cancelled: bool = False
    error: Optional[str] = None
    isolation_verified: bool = False
