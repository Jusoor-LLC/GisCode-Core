from typing import Any, Optional

from giscode.knowledge.service import KnowledgeService
from giscode.knowledge.types import KnowledgeRecord, KnowledgeSourceType
from giscode.monitoring.config import MonitoringConfig
from giscode.monitoring.types import MonitoringEvent, MonitoringEventType, MonitoringStatus


class MonitoringService:
    def __init__(
        self,
        config: Optional[MonitoringConfig] = None,
        knowledge_service: Optional[KnowledgeService] = None,
    ) -> None:
        self._config = config or MonitoringConfig()
        self._knowledge_service = knowledge_service
        self._status = MonitoringStatus.ENABLED if self._config.enabled else MonitoringStatus.DISABLED
        self._events: list[MonitoringEvent] = []

    @property
    def status(self) -> MonitoringStatus:
        return self._status

    def record(self, event: MonitoringEvent) -> Optional[KnowledgeRecord]:
        if self._status != MonitoringStatus.ENABLED:
            return None

        self._events.append(event)
        if len(self._events) > self._config.max_events:
            self._events = self._events[-self._config.max_events :]

        if not self._config.knowledge_store_integration or self._knowledge_service is None:
            return None

        namespace = event.namespace or self._config.namespace
        project_id = event.project_id or self._config.project_id
        application_version = event.application_version or self._config.application_version

        record = KnowledgeRecord(
            record_id=f"monitor-{event.event_type.value}-{len(self._events)}",
            content=event.message,
            source=f"monitoring:{event.event_type.value}",
            source_type=KnowledgeSourceType.TASK,
            namespace=namespace,
            project_id=project_id,
            metadata={
                "event_type": event.event_type.value,
                "status": event.status,
                "timestamp": event.timestamp,
                "application_version": application_version,
                **event.metadata,
            },
        )
        self._knowledge_service.add_record(record)
        return record

    def startup(self, namespace: str = "default", project_id: Optional[str] = None) -> Optional[KnowledgeRecord]:
        event = MonitoringEvent(
            event_type=MonitoringEventType.APPLICATION_STARTUP,
            status="started",
            message="Application startup",
            namespace=namespace,
            project_id=project_id,
            application_version=self._config.application_version,
        )
        return self.record(event)

    def shutdown(self, namespace: str = "default", project_id: Optional[str] = None) -> Optional[KnowledgeRecord]:
        event = MonitoringEvent(
            event_type=MonitoringEventType.APPLICATION_SHUTDOWN,
            status="stopped",
            message="Application shutdown",
            namespace=namespace,
            project_id=project_id,
            application_version=self._config.application_version,
        )
        return self.record(event)

    def package_info(self, package_info: dict, namespace: str = "default", project_id: Optional[str] = None) -> Optional[KnowledgeRecord]:
        event = MonitoringEvent(
            event_type=MonitoringEventType.PACKAGE_INFO,
            status="recorded",
            message="Package information recorded",
            namespace=namespace,
            project_id=project_id,
            application_version=self._config.application_version,
            metadata={"package": package_info},
        )
        return self.record(event)

    def signing_result(self, result: dict, namespace: str = "default", project_id: Optional[str] = None) -> Optional[KnowledgeRecord]:
        event = MonitoringEvent(
            event_type=MonitoringEventType.SIGNING_RESULT,
            status=result.get("status", "unknown"),
            message=result.get("message", ""),
            namespace=namespace,
            project_id=project_id,
            metadata=result,
        )
        return self.record(event)

    def runtime_failure(self, error: str, namespace: str = "default", project_id: Optional[str] = None) -> Optional[KnowledgeRecord]:
        event = MonitoringEvent(
            event_type=MonitoringEventType.RUNTIME_FAILURE,
            status="failed",
            message=error,
            namespace=namespace,
            project_id=project_id,
            metadata={"error": error},
        )
        return self.record(event)

    def get_events(self, namespace: Optional[str] = None) -> list[MonitoringEvent]:
        if namespace is None:
            return list(self._events)
        return [e for e in self._events if e.namespace == namespace]
