import pytest

from giscode.knowledge.config import KnowledgeStoreConfig
from giscode.knowledge.service import KnowledgeService
from giscode.knowledge.types import KnowledgeSourceType
from giscode.research.config import ResearchConfig
from giscode.research.errors import (
    ResearchCancelledError,
    ResearchProviderUnavailableError,
    ResearchTimeoutError,
    ResearchValidationError,
)
from giscode.research.providers import MockResearchProvider, RealResearchProvider
from giscode.research.service import ResearchService
from giscode.research.types import (
    ResearchFinding,
    ResearchProviderType,
    ResearchRequest,
    ResearchResult,
    ResearchSource,
    ResearchSourceType,
    ResearchStatus,
)


def test_research_request_validation():
    with pytest.raises(ValueError):
        ResearchRequest(request_id="", query="test")
    with pytest.raises(ValueError):
        ResearchRequest(request_id="req-1", query="")
    with pytest.raises(ValueError):
        ResearchRequest(request_id="req-1", query="test", max_results=0)
    with pytest.raises(ValueError):
        ResearchRequest(request_id="req-1", query="test", timeout=0)


def test_research_service_disabled():
    service = ResearchService(config=ResearchConfig(enabled=False))
    assert service.enabled is False
    request = ResearchRequest(request_id="req-1", query="test")
    result = service.research(request)
    assert result.status == ResearchStatus.UNAVAILABLE


def test_research_provider_unavailable():
    provider = RealResearchProvider()
    assert provider.is_available() is False
    service = ResearchService(config=ResearchConfig(enabled=True, provider="web_search"), provider=provider)
    request = ResearchRequest(request_id="req-1", query="test")
    result = service.research(request)
    assert result.status == ResearchStatus.UNAVAILABLE


def test_mock_research_success():
    provider = MockResearchProvider(behavior="success")
    service = ResearchService(config=ResearchConfig(enabled=True, provider="mock"), provider=provider)
    request = ResearchRequest(request_id="req-1", query="latest version", namespace="project-a", project_id="proj-a", task_id="task-1")
    result = service.research(request)
    assert result.status == ResearchStatus.COMPLETED
    assert result.finding is not None
    assert result.finding.confidence > 0
    assert len(result.finding.sources) > 0
    assert result.finding.sources[0].source_type == ResearchSourceType.OFFICIAL_RELEASE_NOTES


def test_mock_research_no_results():
    provider = MockResearchProvider(behavior="no_results")
    service = ResearchService(config=ResearchConfig(enabled=True, provider="mock"), provider=provider)
    request = ResearchRequest(request_id="req-1", query="obscure topic")
    result = service.research(request)
    assert result.status == ResearchStatus.COMPLETED
    assert result.finding is not None
    assert len(result.finding.sources) == 0


def test_mock_research_unavailable():
    provider = MockResearchProvider(behavior="unavailable")
    service = ResearchService(config=ResearchConfig(enabled=True, provider="mock"), provider=provider)
    request = ResearchRequest(request_id="req-1", query="test")
    result = service.research(request)
    assert result.status == ResearchStatus.UNAVAILABLE


def test_research_cancellation():
    provider = MockResearchProvider(behavior="success")
    service = ResearchService(config=ResearchConfig(enabled=True, provider="mock"), provider=provider)
    request = ResearchRequest(request_id="req-1", query="test")
    service.cancel()
    result = service.research(request)
    assert result.status == ResearchStatus.CANCELLED


def test_research_query_sanitization():
    service = ResearchService(config=ResearchConfig(enabled=True, provider="mock"))
    request = ResearchRequest(request_id="req-1", query="password=secret123 api_key=xyz")
    result = service.research(request)
    assert "[REDACTED]" in request.query
    assert "secret123" not in request.query
    assert "xyz" not in request.query


def test_research_namespace_isolation(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = ResearchService(
        config=ResearchConfig(enabled=True, provider="mock", knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    request_a = ResearchRequest(request_id="req-a", query="project-a topic", namespace="project-a", project_id="proj-a")
    request_b = ResearchRequest(request_id="req-b", query="project-b topic", namespace="project-b", project_id="proj-b")
    result_a = service.research(request_a)
    result_b = service.research(request_b)
    assert result_a.status == ResearchStatus.COMPLETED
    assert result_b.status == ResearchStatus.COMPLETED
    records_a = knowledge_service.list_records(namespace="project-a", source_type=KnowledgeSourceType.TASK)
    records_b = knowledge_service.list_records(namespace="project-b", source_type=KnowledgeSourceType.TASK)
    assert len(records_a) == 1
    assert len(records_b) == 1
    assert records_a[0].namespace == "project-a"
    assert records_b[0].namespace == "project-b"


def test_research_knowledge_store_ingestion(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = ResearchService(
        config=ResearchConfig(enabled=True, provider="mock", knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    request = ResearchRequest(request_id="req-1", query="api documentation", namespace="default", task_id="task-1")
    result = service.research(request)
    assert result.status == ResearchStatus.COMPLETED
    assert result.finding is not None
    records = knowledge_service.list_records(namespace="default", source_type=KnowledgeSourceType.TASK)
    assert len(records) == 1
    record = records[0]
    assert record.metadata["request_id"] == "req-1"
    assert record.metadata["task_id"] == "task-1"
    assert "sources" in record.metadata
    assert len(record.metadata["sources"]) > 0


def test_research_retrieval(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_service = KnowledgeService(config=KnowledgeStoreConfig(storage_path=db_path))
    service = ResearchService(
        config=ResearchConfig(enabled=True, provider="mock", knowledge_store_integration=True),
        knowledge_service=knowledge_service,
    )
    request = ResearchRequest(request_id="req-1", query="latest version", namespace="default")
    result = service.research(request)
    assert result.status == ResearchStatus.COMPLETED
    retrieved = knowledge_service.search("latest version", namespace="default", source_type=KnowledgeSourceType.TASK, top_k=5)
    assert len(retrieved) >= 1
    assert "1.2.3" in retrieved[0].record.content


def test_research_current_version_information():
    provider = MockResearchProvider(behavior="success")
    service = ResearchService(config=ResearchConfig(enabled=True, provider="mock"), provider=provider)
    request = ResearchRequest(request_id="req-1", query="latest version")
    result = service.research(request)
    assert result.status == ResearchStatus.COMPLETED
    assert result.finding is not None
    assert result.finding.is_current is True
    assert "1.2.3" in result.finding.summary
    assert result.finding.retrieval_timestamp != ""


def test_research_source_quality():
    provider = MockResearchProvider(behavior="success")
    service = ResearchService(config=ResearchConfig(enabled=True, provider="mock"), provider=provider)
    request = ResearchRequest(request_id="req-1", query="latest version")
    result = service.research(request)
    assert result.status == ResearchStatus.COMPLETED
    source = result.finding.sources[0]
    assert source.title != ""
    assert source.url != ""
    assert source.domain != ""
    assert source.source_type == ResearchSourceType.OFFICIAL_RELEASE_NOTES
    assert source.relevance > 0
    assert source.authority > 0
    assert source.retrieved_at != ""


def test_research_max_results():
    provider = MockResearchProvider(behavior="success")
    service = ResearchService(config=ResearchConfig(enabled=True, provider="mock", max_results=3), provider=provider)
    request = ResearchRequest(request_id="req-1", query="latest version", max_results=3)
    result = service.research(request)
    assert result.status == ResearchStatus.COMPLETED
    assert len(result.finding.sources) <= 3


def test_research_timeout():
    provider = MockResearchProvider(behavior="timeout")
    service = ResearchService(config=ResearchConfig(enabled=True, provider="mock"), provider=provider)
    request = ResearchRequest(request_id="req-1", query="test", timeout=0.1)
    result = service.research(request)
    assert result.status == ResearchStatus.TIMED_OUT
