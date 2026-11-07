import pytest

from giscode.knowledge.config import KnowledgeStoreConfig
from giscode.knowledge.service import KnowledgeService
from giscode.knowledge.types import KnowledgeSourceType
from giscode.monitoring.config import MonitoringConfig
from giscode.monitoring.service import MonitoringService
from giscode.monitoring.types import MonitoringEvent, MonitoringEventType, MonitoringStatus


def test_monitoring_service_disabled():
    service = MonitoringService(config=MonitoringConfig(enabled=False))
    assert service.status == MonitoringStatus.DISABLED
    event = MonitoringEvent(
        event_type=MonitoringEventType.APPLICATION_STARTUP,
        status="started",
        message="startup",
    )
    result = service.record(event)
    assert result is None


def test_monitoring_service_enabled_without_knowledge():
    service = MonitoringService(config=MonitoringConfig(enabled=True))
    assert service.status == MonitoringStatus.ENABLED
    event = MonitoringEvent(
        event_type=MonitoringEventType.APPLICATION_STARTUP,
        status="started",
        message="startup",
    )
    result = service.record(event)
    assert result is None
    assert len(service.get_events()) == 1


def test_monitoring_startup_event(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = MonitoringService(
        config=MonitoringConfig(enabled=True, knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    record = service.startup(namespace="project-a", project_id="proj-a")
    assert record is not None
    assert record.source_type == KnowledgeSourceType.TASK
    assert record.namespace == "project-a"
    assert record.project_id == "proj-a"
    assert "event_type" in record.metadata
    assert record.metadata["event_type"] == "application_startup"


def test_monitoring_shutdown_event(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = MonitoringService(
        config=MonitoringConfig(enabled=True, knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    record = service.shutdown(namespace="project-a", project_id="proj-a")
    assert record is not None
    assert record.metadata["event_type"] == "application_shutdown"


def test_monitoring_package_info_event(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = MonitoringService(
        config=MonitoringConfig(enabled=True, knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    package_info = {"version": "0.1.0", "package_type": "msix"}
    record = service.package_info(package_info, namespace="project-a")
    assert record is not None
    assert record.metadata["event_type"] == "package_info"
    assert "package" in record.metadata


def test_monitoring_signing_result_event(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = MonitoringService(
        config=MonitoringConfig(enabled=True, knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    result = service.signing_result(
        {"status": "signing_completed", "message": "signed"},
        namespace="project-a",
    )
    assert result is not None
    assert result.metadata["event_type"] == "signing_result"
    assert result.metadata["status"] == "signing_completed"


def test_monitoring_runtime_failure_event(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = MonitoringService(
        config=MonitoringConfig(enabled=True, knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    record = service.runtime_failure("test error", namespace="project-a")
    assert record is not None
    assert record.metadata["event_type"] == "runtime_failure"
    assert record.metadata["status"] == "failed"


def test_monitoring_namespace_isolation(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = MonitoringService(
        config=MonitoringConfig(enabled=True, knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    service.startup(namespace="project-a")
    service.startup(namespace="project-b")
    events_a = service.get_events(namespace="project-a")
    events_b = service.get_events(namespace="project-b")
    assert len(events_a) == 1
    assert len(events_b) == 1


def test_monitoring_project_isolation(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = MonitoringService(
        config=MonitoringConfig(enabled=True, knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    service.record(
        MonitoringEvent(
            event_type=MonitoringEventType.APPLICATION_STARTUP,
            status="started",
            message="startup proj-a",
            namespace="default",
            project_id="proj-a",
        )
    )
    service.record(
        MonitoringEvent(
            event_type=MonitoringEventType.APPLICATION_STARTUP,
            status="started",
            message="startup proj-b",
            namespace="default",
            project_id="proj-b",
        )
    )
    events = service.get_events(namespace="default")
    assert len(events) == 2
    project_ids = {e.project_id for e in events}
    assert project_ids == {"proj-a", "proj-b"}


def test_monitoring_sensitive_data_redacted():
    event = MonitoringEvent(
        event_type=MonitoringEventType.RUNTIME_FAILURE,
        status="failed",
        message="auth failed",
        metadata={
            "password": "secret123",
            "api_key": "key-abc",
            "nested": {"token": "xyz"},
            "safe_field": "safe_value",
        },
    )
    record = event.to_record()
    assert record["metadata"]["password"] == "***REDACTED***"
    assert record["metadata"]["api_key"] == "***REDACTED***"
    assert record["metadata"]["nested"]["token"] == "***REDACTED***"
    assert record["metadata"]["safe_field"] == "safe_value"


def test_monitoring_event_max_events():
    service = MonitoringService(config=MonitoringConfig(enabled=True, max_events=3))
    for i in range(5):
        service.record(
            MonitoringEvent(
                event_type=MonitoringEventType.HEALTH_STATUS,
                status="ok",
                message=f"event {i}",
            )
        )
    events = service.get_events()
    assert len(events) == 3
    assert events[0].message == "event 2"
    assert events[-1].message == "event 4"


def test_monitoring_disabled_does_not_record():
    service = MonitoringService(config=MonitoringConfig(enabled=False))
    event = MonitoringEvent(
        event_type=MonitoringEventType.APPLICATION_STARTUP,
        status="started",
        message="startup",
    )
    service.record(event)
    assert len(service.get_events()) == 0
