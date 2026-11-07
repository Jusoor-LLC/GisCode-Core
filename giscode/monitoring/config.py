from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from giscode.monitoring.types import MonitoringStatus


@dataclass
class MonitoringConfig:
    enabled: bool = True
    namespace: str = "default"
    project_id: Optional[str] = None
    application_name: str = "GisCode"
    application_version: str = "0.1.0"
    max_events: int = 1000
    knowledge_store_integration: bool = True
