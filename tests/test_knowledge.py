import os
import tempfile
from pathlib import Path

import pytest

from giscode.knowledge.config import KnowledgeStoreConfig
from giscode.knowledge.errors import (
    DuplicateRecordError,
    NamespaceError,
    PathSafetyError,
    RecordNotFoundError,
    RetrievalError,
    ValidationError,
)
from giscode.knowledge.index import compute_content_hash, compute_token_overlap_score, tokenize
from giscode.knowledge.service import KnowledgeService
from giscode.knowledge.store import KnowledgeStore
from giscode.knowledge.types import (
    FailureRecord,
    FixRecord,
    KnowledgeRecord,
    KnowledgeSourceType,
    RetrievalResult,
)


def _make_service(tmp_path: Path) -> KnowledgeService:
    db_path = tmp_path / "knowledge.db"
    config = KnowledgeStoreConfig(storage_path=db_path)
    return KnowledgeService(config=config)


def test_knowledge_record_creation():
    record = KnowledgeRecord(
        record_id="rec-1",
        content="Hello world",
        source="test",
        source_type=KnowledgeSourceType.CODE,
        namespace="project-a",
    )
    assert record.record_id == "rec-1"
    assert record.content == "Hello world"
    assert record.source == "test"
    assert record.source_type == KnowledgeSourceType.CODE
    assert record.namespace == "project-a"
    assert record.project_id is None
    assert record.metadata == {}
    assert record.created_at is not None
    assert record.updated_at is not None
    assert record.content_hash is None
    assert record.parent_id is None
    assert record.tags == []
    assert record.embedding is None


def test_knowledge_record_validation(tmp_path):
    service = _make_service(tmp_path)
    with pytest.raises(ValidationError):
        service.add_record(
            KnowledgeRecord(
                record_id="",
                content="Hello",
                source="test",
                source_type=KnowledgeSourceType.CODE,
                namespace="ns",
            )
        )
    with pytest.raises(ValidationError):
        service.add_record(
            KnowledgeRecord(
                record_id="rec-1",
                content="",
                source="test",
                source_type=KnowledgeSourceType.CODE,
                namespace="ns",
            )
        )


def test_persistent_storage(tmp_path):
    service = _make_service(tmp_path)
    record = KnowledgeRecord(
        record_id="rec-1",
        content="Persistent content",
        source="test",
        source_type=KnowledgeSourceType.DOCUMENTATION,
        namespace="project-a",
    )
    service.add_record(record)
    retrieved = service.get_record("rec-1")
    assert retrieved is not None
    assert retrieved.content == "Persistent content"


def test_add_get_update_delete(tmp_path):
    service = _make_service(tmp_path)
    record = KnowledgeRecord(
        record_id="rec-1",
        content="Original content",
        source="test",
        source_type=KnowledgeSourceType.CODE,
        namespace="project-a",
    )
    service.add_record(record)
    assert service.get_record("rec-1") is not None

    updated = service.update_record("rec-1", content="Updated content")
    assert updated is not None
    assert updated.content == "Updated content"

    assert service.delete_record("rec-1") is True
    assert service.get_record("rec-1") is None


def test_namespace_isolation(tmp_path):
    service = _make_service(tmp_path)
    record_a = KnowledgeRecord(
        record_id="rec-a",
        content="Project A content",
        source="test",
        source_type=KnowledgeSourceType.CODE,
        namespace="project-a",
    )
    record_b = KnowledgeRecord(
        record_id="rec-b",
        content="Project B content",
        source="test",
        source_type=KnowledgeSourceType.CODE,
        namespace="project-b",
    )
    service.add_record(record_a)
    service.add_record(record_b)

    results_a = service.list_records(namespace="project-a")
    results_b = service.list_records(namespace="project-b")
    assert len(results_a) == 1
    assert len(results_b) == 1
    assert results_a[0].record_id == "rec-a"
    assert results_b[0].record_id == "rec-b"


def test_project_isolation(tmp_path):
    service = _make_service(tmp_path)
    record_a = KnowledgeRecord(
        record_id="rec-a",
        content="Project A content",
        source="test",
        source_type=KnowledgeSourceType.CODE,
        namespace="default",
        project_id="project-a",
    )
    record_b = KnowledgeRecord(
        record_id="rec-b",
        content="Project B content",
        source="test",
        source_type=KnowledgeSourceType.CODE,
        namespace="default",
        project_id="project-b",
    )
    service.add_record(record_a)
    service.add_record(record_b)

    results_a = service.list_records(project_id="project-a")
    results_b = service.list_records(project_id="project-b")
    assert len(results_a) == 1
    assert len(results_b) == 1
    assert results_a[0].record_id == "rec-a"
    assert results_b[0].record_id == "rec-b"


def test_content_hashing(tmp_path):
    service = _make_service(tmp_path)
    record = KnowledgeRecord(
        record_id="rec-1",
        content="Hash me",
        source="test",
        source_type=KnowledgeSourceType.CODE,
        namespace="project-a",
    )
    service.add_record(record)
    retrieved = service.get_record("rec-1")
    assert retrieved is not None
    assert retrieved.content_hash is not None


def test_duplicate_detection_same_namespace(tmp_path):
    service = _make_service(tmp_path)
    record1 = KnowledgeRecord(
        record_id="rec-1",
        content="Duplicate content",
        source="test",
        source_type=KnowledgeSourceType.CODE,
        namespace="project-a",
    )
    service.add_record(record1)
    with pytest.raises(DuplicateRecordError):
        service.add_record(
            KnowledgeRecord(
                record_id="rec-2",
                content="Duplicate content",
                source="test",
                source_type=KnowledgeSourceType.CODE,
                namespace="project-a",
            )
        )


def test_same_content_different_namespace_allowed(tmp_path):
    service = _make_service(tmp_path)
    record1 = KnowledgeRecord(
        record_id="rec-1",
        content="Same content",
        source="test",
        source_type=KnowledgeSourceType.CODE,
        namespace="project-a",
    )
    record2 = KnowledgeRecord(
        record_id="rec-2",
        content="Same content",
        source="test",
        source_type=KnowledgeSourceType.CODE,
        namespace="project-b",
    )
    service.add_record(record1)
    service.add_record(record2)
    assert service.get_record("rec-1") is not None
    assert service.get_record("rec-2") is not None


def test_baseline_retrieval_ranking(tmp_path):
    service = _make_service(tmp_path)
    service.add_record(KnowledgeRecord(record_id="r1", content="python error fix", source="s", source_type=KnowledgeSourceType.ERROR, namespace="ns"))
    service.add_record(KnowledgeRecord(record_id="r2", content="javascript build error", source="s", source_type=KnowledgeSourceType.ERROR, namespace="ns"))
    service.add_record(KnowledgeRecord(record_id="r3", content="python unit test success", source="s", source_type=KnowledgeSourceType.TEST_RESULT, namespace="ns"))

    results = service.search("python error", namespace="ns", top_k=2)
    assert len(results) == 2
    assert results[0].record.record_id == "r1"
    assert results[1].record.record_id == "r3"


def test_top_k_behavior(tmp_path):
    service = _make_service(tmp_path)
    for i in range(5):
        service.add_record(KnowledgeRecord(record_id=f"r{i}", content=f"python code {i}", source="s", source_type=KnowledgeSourceType.CODE, namespace="ns"))
    results = service.search("python", namespace="ns", top_k=3)
    assert len(results) == 3


def test_source_type_filtering(tmp_path):
    service = _make_service(tmp_path)
    service.add_record(KnowledgeRecord(record_id="r1", content="python error", source="s", source_type=KnowledgeSourceType.ERROR, namespace="ns"))
    service.add_record(KnowledgeRecord(record_id="r2", content="python code", source="s", source_type=KnowledgeSourceType.CODE, namespace="ns"))
    results = service.search("python", namespace="ns", source_type=KnowledgeSourceType.ERROR)
    assert len(results) == 1
    assert results[0].record.record_id == "r1"


def test_metadata_filtering(tmp_path):
    service = _make_service(tmp_path)
    service.add_record(KnowledgeRecord(record_id="r1", content="doc A", source="s", source_type=KnowledgeSourceType.DOCUMENTATION, namespace="ns", metadata={"lang": "en"}))
    service.add_record(KnowledgeRecord(record_id="r2", content="doc B", source="s", source_type=KnowledgeSourceType.DOCUMENTATION, namespace="ns", metadata={"lang": "de"}))
    results = service.search("doc", namespace="ns", metadata_filters={"lang": "en"})
    assert len(results) == 1
    assert results[0].record.record_id == "r1"


def test_empty_result_behavior(tmp_path):
    service = _make_service(tmp_path)
    results = service.search("nonexistent", namespace="ns")
    assert results == []


def test_invalid_namespace_rejection(tmp_path):
    service = _make_service(tmp_path)
    with pytest.raises(NamespaceError):
        service.add_record(KnowledgeRecord(record_id="r1", content="test", source="s", source_type=KnowledgeSourceType.CODE, namespace=""))


def test_storage_path_safety():
    with pytest.raises(PathSafetyError):
        KnowledgeStoreConfig(storage_path=Path("C:/etc/passwd"))


def test_failure_record_storage(tmp_path):
    service = _make_service(tmp_path)
    failure = FailureRecord(
        failure_id="f1",
        error_signature="sig-1",
        error_text="ImportError",
        phase="build",
        context="python main.py",
        project_id="project-a",
        namespace="project-a",
    )
    service.add_failure_record(failure)
    retrieved = service.get_failure_record("f1")
    assert retrieved is not None
    assert retrieved.error_text == "ImportError"


def test_fix_record_storage(tmp_path):
    service = _make_service(tmp_path)
    failure = FailureRecord(
        failure_id="f1",
        error_signature="sig-1",
        error_text="ImportError",
        phase="build",
        context="python main.py",
        project_id="project-a",
        namespace="project-a",
    )
    service.add_failure_record(failure)
    fix = FixRecord(
        fix_id="fix-1",
        failure_id="f1",
        attempted_fix="pip install missing",
        result="success",
        verification_status="verified",
        project_id="project-a",
        namespace="project-a",
    )
    service.add_fix_record(fix)
    fixes = service.list_fix_records_for_failure("f1")
    assert len(fixes) == 1
    assert fixes[0].fix_id == "fix-1"


def test_failure_fix_relationship(tmp_path):
    service = _make_service(tmp_path)
    failure = FailureRecord(
        failure_id="f1",
        error_signature="sig-1",
        error_text="ImportError",
        phase="build",
        context="python main.py",
        project_id="project-a",
        namespace="project-a",
    )
    service.add_failure_record(failure)
    fix = FixRecord(
        fix_id="fix-1",
        failure_id="f1",
        attempted_fix="pip install missing",
        result="success",
        verification_status="verified",
        project_id="project-a",
        namespace="project-a",
    )
    service.add_fix_record(fix)
    retrieved_fix = service.get_fix_record("fix-1")
    assert retrieved_fix is not None
    assert retrieved_fix.failure_id == "f1"


def test_agentic_retrieval_integration(tmp_path):
    from giscode.agentic.loop import CodingAgentLoop
    from giscode.inference.api import InferenceAPI
    from giscode.task_control.manager import TaskManager
    from giscode.workspace.workspace import Workspace

    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    inference = InferenceAPI()
    service = _make_service(tmp_path)
    service.add_record(KnowledgeRecord(record_id="k1", content="python build fix", source="s", source_type=KnowledgeSourceType.FIX, namespace="project-a"))
    loop = CodingAgentLoop(workspace, inference, task_manager, knowledge_service=service)
    results = loop.retrieve_knowledge("python build", namespace="project-a")
    assert len(results) == 1
    assert results[0]["record_id"] == "k1"


def test_persistence_across_service_restart(tmp_path):
    db_path = tmp_path / "knowledge.db"
    config = KnowledgeStoreConfig(storage_path=db_path)
    service1 = KnowledgeService(config=config)
    service1.add_record(KnowledgeRecord(record_id="rec-1", content="Persistent", source="s", source_type=KnowledgeSourceType.CODE, namespace="ns"))
    service1.close()

    service2 = KnowledgeService(config=config)
    assert service2.get_record("rec-1") is not None
    assert service2.get_record("rec-1").content == "Persistent"
    service2.close()


def test_previous_tasks_remain_intact():
    from giscode.knowledge.types import KnowledgeRecord, KnowledgeSourceType
    record = KnowledgeRecord(record_id="r1", content="test", source="s", source_type=KnowledgeSourceType.CODE, namespace="ns")
    assert record.record_id == "r1"


def test_tokenize():
    tokens = tokenize("Hello World! Python3")
    assert "hello" in tokens
    assert "world" in tokens
    assert "python3" in tokens


def test_compute_token_overlap_score():
    score = compute_token_overlap_score("python error", "python build error fix")
    assert score > 0.0
    assert score <= 1.0


def test_compute_content_hash():
    h1 = compute_content_hash("content", "ns", "src")
    h2 = compute_content_hash("content", "ns", "src")
    h3 = compute_content_hash("content", "other-ns", "src")
    assert h1 == h2
    assert h1 != h3


def test_record_count(tmp_path):
    service = _make_service(tmp_path)
    for i in range(3):
        service.add_record(KnowledgeRecord(record_id=f"r{i}", content=f"content {i}", source="s", source_type=KnowledgeSourceType.CODE, namespace="ns"))
    assert service.count(namespace="ns") == 3
    assert service.count(namespace="other") == 0


def test_list_records_pagination(tmp_path):
    service = _make_service(tmp_path)
    for i in range(5):
        service.add_record(KnowledgeRecord(record_id=f"r{i}", content=f"content {i}", source="s", source_type=KnowledgeSourceType.CODE, namespace="ns"))
    page1 = service.list_records(namespace="ns", limit=2, offset=0)
    page2 = service.list_records(namespace="ns", limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
