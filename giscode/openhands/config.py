from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class OpenHandsConfig:
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    timeout: float = 60.0
    workspace_mount_path: str = "/workspace"
    remove_after_execution: bool = True
    executable: Optional[str] = None
    runtime_args: List[str] = field(default_factory=list)
