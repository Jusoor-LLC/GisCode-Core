from enum import Enum
from dataclasses import dataclass, field
from typing import Optional


class ModelState(Enum):
    UNLOADED = "unloaded"
    LOADING = "loading"
    READY = "ready"
    BUSY = "busy"
    UNLOADING = "unloading"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class ModelInfo:
    name: str
    path: str
    size_bytes: int = 0
    context_length: int = 2048
    gpu_layers: int = 0
    state: ModelState = ModelState.UNLOADED
    error_message: Optional[str] = None
    load_time: Optional[float] = None
