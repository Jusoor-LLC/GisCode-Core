"""Native, application-managed local AI for GisCode.

The public surface is :class:`AIModuleService`: it owns the runtime and model
installation, configures the existing ``InferenceAPI``/``ModelManager`` path,
and runs the readiness test.  Nothing here downloads anything at import time.
"""
from giscode.ai_module.service import (
    AIModuleError,
    AIModuleService,
    ModuleResult,
    ModuleStatus,
    READY,
    READY_SLOW,
    TOO_LARGE,
    FAILED,
)

__all__ = [
    "AIModuleError",
    "AIModuleService",
    "ModuleResult",
    "ModuleStatus",
    "READY",
    "READY_SLOW",
    "TOO_LARGE",
    "FAILED",
]
