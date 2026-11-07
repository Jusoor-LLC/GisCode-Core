import warnings

warnings.warn(
    "giscode.task_router is deprecated. GisAi (giscode.gisai) is now the authoritative intelligence layer. "
    "This package will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

from giscode.task_router.router import CapabilityRouter
from giscode.task_router.memory import TaskMemoryStore
from giscode.task_router.delegation import DelegationEngine
from giscode.task_router.understanding import detect_intent, needs_image_for_intent
from giscode.task_router.types import Attachment, TaskIntent, TaskState
