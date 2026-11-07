from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class MonitoringEventType(Enum):
    APPLICATION_STARTUP = "application_startup"
    APPLICATION_SHUTDOWN = "application_shutdown"
    PACKAGE_INFO = "package_info"
    DEPLOYMENT_INSTALL = "deployment_install"
    DEPLOYMENT_FAILURE = "deployment_failure"
    RUNTIME_FAILURE = "runtime_failure"
    SIGNING_RESULT = "signing_result"
    HEALTH_STATUS = "health_status"
    OCR_EVENT = "ocr_event"
    TASK_EVENT = "task_event"


class MonitoringStatus(Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"


@dataclass
class MonitoringEvent:
    event_type: MonitoringEventType
    status: str
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    namespace: str = "default"
    project_id: Optional[str] = None
    application_version: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_record(self, source: str = "monitoring") -> dict:
        return {
            "event_type": self.event_type.value,
            "status": self.status,
            "message": self.message,
            "timestamp": self.timestamp,
            "namespace": self.namespace,
            "project_id": self.project_id,
            "application_version": self.application_version,
            "metadata": self._sanitize_metadata(self.metadata),
        }

    @staticmethod
    def _sanitize_metadata(metadata: dict) -> dict:
        sensitive_keys = {
            "password", "secret", "token", "api_key", "apikey",
            "private_key", "credentials", "certificate_password",
        }
        sanitized = {}
        for key, value in metadata.items():
            if key.lower() in sensitive_keys:
                sanitized[key] = "***REDACTED***"
            elif isinstance(value, dict):
                sanitized[key] = MonitoringEvent._sanitize_metadata(value)
            else:
                sanitized[key] = value
        return sanitized
