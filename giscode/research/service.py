import re
import threading
from datetime import datetime
from typing import Any, Optional

from giscode.knowledge.service import KnowledgeService
from giscode.knowledge.types import KnowledgeRecord, KnowledgeSourceType
from giscode.research.config import ResearchConfig
from giscode.research.errors import (
    ResearchCancelledError,
    ResearchProviderUnavailableError,
    ResearchTimeoutError,
    ResearchValidationError,
)
from giscode.research.providers import MockResearchProvider, RealResearchProvider, ResearchProvider
from giscode.research.types import (
    ResearchFinding,
    ResearchProviderType,
    ResearchRequest,
    ResearchResult,
    ResearchSource,
    ResearchSourceType,
    ResearchStatus,
)


class ResearchService:
    def __init__(
        self,
        config: Optional[ResearchConfig] = None,
        knowledge_service: Optional[KnowledgeService] = None,
        provider: Optional[ResearchProvider] = None,
    ) -> None:
        self._config = config or ResearchConfig()
        self._knowledge_service = knowledge_service
        self._provider = provider or self._create_default_provider()
        self._cancel_event = threading.Event()
        self._current_request: Optional[ResearchRequest] = None

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def provider_available(self) -> bool:
        return self._provider.is_available()

    def _create_default_provider(self) -> ResearchProvider:
        provider_type = ResearchProviderType(self._config.provider)
        if provider_type == ResearchProviderType.MOCK:
            return MockResearchProvider()
        if provider_type == ResearchProviderType.WEB_SEARCH:
            return RealResearchProvider()
        return MockResearchProvider()

    def _sanitize_query(self, query: str) -> str:
        sensitive_patterns = [
            r"password\s*[=:]\s*\S+",
            r"secret\s*[=:]\s*\S+",
            r"token\s*[=:]\s*\S+",
            r"api[_-]?key\s*[=:]\s*\S+",
            r"apikey\s*[=:]\s*\S+",
            r"private[_-]?key\s*[=:]\s*\S+",
            r"credentials\s*[=:]\s*\S+",
            r"certificate[_-]?password\s*[=:]\s*\S+",
        ]
        sanitized = query
        for pattern in sensitive_patterns:
            sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)
        return sanitized

    def research(self, request: ResearchRequest) -> ResearchResult:
        if not self._config.enabled:
            return ResearchResult(
                request_id=request.request_id,
                status=ResearchStatus.UNAVAILABLE,
                error="Research is disabled",
            )

        self._current_request = request
        self._cancel_event.clear()

        if not self._provider.is_available():
            return ResearchResult(
                request_id=request.request_id,
                status=ResearchStatus.UNAVAILABLE,
                provider=self._provider.provider_type.value,
                error="Research provider is unavailable",
            )

        if len(request.query) > self._config.max_query_length:
            return ResearchResult(
                request_id=request.request_id,
                status=ResearchStatus.FAILED,
                error=f"Query exceeds max length: {self._config.max_query_length}",
            )

        sanitized_query = self._sanitize_query(request.query)
        if sanitized_query != request.query:
            request.query = sanitized_query

        started_at = datetime.now().isoformat()

        try:
            result = self._provider.research(request)
            result.started_at = started_at
            result.finished_at = datetime.now().isoformat()
            result.provider = self._provider.provider_type.value

            if self._cancel_event.is_set():
                result.status = ResearchStatus.CANCELLED
                result.error = "Research was cancelled"

            if result.status == ResearchStatus.COMPLETED and result.finding:
                self._ingest_result(request, result)

            return result
        except Exception as e:
            return ResearchResult(
                request_id=request.request_id,
                status=ResearchStatus.FAILED,
                provider=self._provider.provider_type.value,
                started_at=started_at,
                error=str(e),
            )

    def _ingest_result(self, request: ResearchRequest, result: ResearchResult) -> None:
        if not self._config.knowledge_store_integration or self._knowledge_service is None:
            return
        if result.finding is None:
            return

        try:
            record = KnowledgeRecord(
                record_id=f"research-{request.request_id}",
                content=result.finding.summary,
                source=f"research:{request.request_id}",
                source_type=KnowledgeSourceType.TASK,
                namespace=request.namespace,
                project_id=request.project_id,
                metadata={
                    "request_id": request.request_id,
                    "task_id": request.task_id,
                    "query": request.query,
                    "purpose": request.purpose,
                    "provider": result.provider,
                    "status": result.status.value,
                    "confidence": result.finding.confidence,
                    "is_current": result.finding.is_current,
                    "retrieval_timestamp": result.finding.retrieval_timestamp,
                    "started_at": result.started_at,
                    "finished_at": result.finished_at,
                    "sources": [
                        {
                            "title": s.title,
                            "url": s.url,
                            "domain": s.domain,
                            "source_type": s.source_type.value,
                            "published_at": s.published_at,
                            "retrieved_at": s.retrieved_at,
                            "relevance": s.relevance,
                            "authority": s.authority,
                            "snippet": s.snippet,
                        }
                        for s in result.finding.sources
                    ],
                },
            )
            self._knowledge_service.add_record(record)
        except Exception:
            pass

    def cancel(self) -> None:
        self._cancel_event.set()
        if isinstance(self._provider, MockResearchProvider):
            self._provider.cancel()

    def get_status(self) -> ResearchStatus:
        if not self._config.enabled:
            return ResearchStatus.UNAVAILABLE
        if not self._provider.is_available():
            return ResearchStatus.UNAVAILABLE
        if self._cancel_event.is_set():
            return ResearchStatus.CANCELLED
        return ResearchStatus.ENABLED
