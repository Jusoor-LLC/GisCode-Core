import pytest

from giscode.knowledge.config import KnowledgeStoreConfig
from giscode.knowledge.service import KnowledgeService
from giscode.knowledge.types import KnowledgeSourceType
from giscode.self_improvement.config import SelfImprovementConfig
from giscode.self_improvement.service import SelfImprovementService
from giscode.self_improvement.types import FixStatus, LearningEvent


def test_self_improvement_service_disabled():
    service = SelfImprovementService(config=SelfImprovementConfig(enabled=False))
    assert service.enabled is False
    pair = service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1, error_text="ModuleNotFoundError")
    assert pair is None


def test_record_failure():
    service = SelfImprovementService(config=SelfImprovementConfig(enabled=True))
    pair = service.record_failure(
        failure_type="build_failure",
        command="python -m build",
        exit_code=1,
        error_text="ModuleNotFoundError: No module named 'x'",
        namespace="project-a",
        project_id="proj-a",
        task_id="task-1",
    )
    assert pair is not None
    assert pair.status == FixStatus.FAILED_ATTEMPT
    assert pair.failure_type == "build_failure"
    assert pair.namespace == "project-a"
    assert pair.project_id == "proj-a"
    assert pair.task_id == "task-1"


def test_record_candidate_fix():
    service = SelfImprovementService(config=SelfImprovementConfig(enabled=True))
    pair = service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1, error_text="ModuleNotFoundError")
    updated = service.record_candidate_fix(
        failure_pair_id=pair.pair_id,
        fix_proposal="Install missing module",
        files_changed=["requirements.txt"],
    )
    assert updated is not None
    assert updated.status == FixStatus.CANDIDATE_FIX
    assert updated.fix_proposal == "Install missing module"
    assert updated.files_changed == ["requirements.txt"]


def test_record_verified_fix_success():
    service = SelfImprovementService(config=SelfImprovementConfig(enabled=True))
    pair = service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1, error_text="ModuleNotFoundError")
    service.record_candidate_fix(failure_pair_id=pair.pair_id, fix_proposal="Install missing module")
    verified = service.record_verified_fix(failure_pair_id=pair.pair_id, verification_result="build_and_test_passed", fix_succeeded=True)
    assert verified is not None
    assert verified.status == FixStatus.VERIFIED_PAIR
    assert verified.fix_succeeded is True


def test_record_verified_fix_failure():
    service = SelfImprovementService(config=SelfImprovementConfig(enabled=True))
    pair = service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1, error_text="ModuleNotFoundError")
    service.record_candidate_fix(failure_pair_id=pair.pair_id, fix_proposal="Install missing module")
    verified = service.record_verified_fix(failure_pair_id=pair.pair_id, verification_result="retry_limit_exhausted", fix_succeeded=False)
    assert verified is not None
    assert verified.status == FixStatus.FAILED_ATTEMPT
    assert verified.fix_succeeded is False


def test_unsuccessful_fix_not_verified():
    service = SelfImprovementService(config=SelfImprovementConfig(enabled=True))
    pair = service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1, error_text="ModuleNotFoundError")
    service.record_candidate_fix(failure_pair_id=pair.pair_id, fix_proposal="Install missing module")
    verified = service.record_verified_fix(failure_pair_id=pair.pair_id, fix_succeeded=False)
    assert verified.status != FixStatus.VERIFIED_PAIR
    pairs = service.get_pairs(status=FixStatus.VERIFIED_PAIR)
    assert len(pairs) == 0


def test_deduplication():
    service = SelfImprovementService(config=SelfImprovementConfig(enabled=True))
    pair1 = service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1, error_text="ModuleNotFoundError")
    service.record_candidate_fix(failure_pair_id=pair1.pair_id, fix_proposal="Install module")
    service.record_verified_fix(failure_pair_id=pair1.pair_id, fix_succeeded=True)

    pair2 = service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1, error_text="ModuleNotFoundError")
    service.record_candidate_fix(failure_pair_id=pair2.pair_id, fix_proposal="Install module")
    result = service.record_verified_fix(failure_pair_id=pair2.pair_id, fix_succeeded=True)
    assert result is not None
    assert result.pair_id == pair2.pair_id
    pairs = service.get_pairs(status=FixStatus.VERIFIED_PAIR)
    assert len(pairs) == 1


def test_knowledge_store_integration(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = SelfImprovementService(
        config=SelfImprovementConfig(enabled=True, knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    pair = service.record_failure(
        failure_type="build_failure",
        command="python -m build",
        exit_code=1,
        error_text="ModuleNotFoundError",
        namespace="project-a",
        project_id="proj-a",
    )
    assert pair.knowledge_record_id is not None
    record = knowledge_service.get_record(pair.knowledge_record_id)
    assert record is not None
    assert record.source_type == KnowledgeSourceType.TASK
    assert record.namespace == "project-a"


def test_namespace_isolation(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = SelfImprovementService(
        config=SelfImprovementConfig(enabled=True, knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1, namespace="project-a")
    service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1, namespace="project-b")
    pairs_a = service.get_pairs(namespace="project-a")
    pairs_b = service.get_pairs(namespace="project-b")
    assert len(pairs_a) == 1
    assert len(pairs_b) == 1


def test_retrieve_fix_patterns(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = SelfImprovementService(
        config=SelfImprovementConfig(enabled=True, knowledge_store_integration=True, max_retrieved_patterns=5),
        knowledge_service=knowledge_service,
    )
    pair = service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1, error_text="ModuleNotFoundError")
    service.record_candidate_fix(failure_pair_id=pair.pair_id, fix_proposal="Install missing module")
    service.record_verified_fix(failure_pair_id=pair.pair_id, fix_succeeded=True)

    patterns = service.retrieve_fix_patterns(query="ModuleNotFoundError", namespace="default", top_k=5)
    assert len(patterns) >= 1
    assert any(p["fix_proposal"] == "Install missing module" for p in patterns)


def test_export_dataset(tmp_path):
    service = SelfImprovementService(
        config=SelfImprovementConfig(enabled=True, dataset_export_path=str(tmp_path / "dataset.json")),
    )
    pair = service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1, error_text="ModuleNotFoundError")
    service.record_candidate_fix(failure_pair_id=pair.pair_id, fix_proposal="Install module")
    service.record_verified_fix(failure_pair_id=pair.pair_id, fix_succeeded=True)

    dataset = service.export_dataset()
    assert dataset["total_pairs"] == 1
    assert len(dataset["pairs"]) == 1
    assert dataset["pairs"][0]["fix_proposal"] == "Install module"
    assert dataset["pairs"][0]["fix_succeeded"] is True


def test_export_dataset_excludes_unverified(tmp_path):
    service = SelfImprovementService(
        config=SelfImprovementConfig(enabled=True, dataset_export_path=str(tmp_path / "dataset.json")),
    )
    pair = service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1, error_text="ModuleNotFoundError")
    service.record_candidate_fix(failure_pair_id=pair.pair_id, fix_proposal="Install module")
    service.record_verified_fix(failure_pair_id=pair.pair_id, fix_succeeded=False)

    dataset = service.export_dataset()
    assert dataset["total_pairs"] == 0


def test_learning_event_recording(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = SelfImprovementService(
        config=SelfImprovementConfig(enabled=True, knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    pair = service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1)
    event = LearningEvent(event_type="fix_attempted", pair_id=pair.pair_id, status="candidate", message="Fix attempted")
    service.record_learning_event(event)
    assert len(service.get_pairs()) == 1


def test_disabled_does_not_record():
    service = SelfImprovementService(config=SelfImprovementConfig(enabled=False))
    pair = service.record_failure(failure_type="build_failure", command="python -m build", exit_code=1)
    assert pair is None
    assert len(service.get_pairs()) == 0
