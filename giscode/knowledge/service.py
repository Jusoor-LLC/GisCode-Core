from typing import Any, Optional

from giscode.knowledge.config import KnowledgeStoreConfig
from giscode.knowledge.errors import (
    DuplicateRecordError,
    KnowledgeStoreError,
    RecordNotFoundError,
    RetrievalError,
    ValidationError,
)
from giscode.knowledge.retrieval import BaselineLexicalRetrieval, RetrievalBackend, VectorRetrieval
from giscode.knowledge.store import KnowledgeStore
from giscode.knowledge.types import (
    FailureRecord,
    FixRecord,
    KnowledgeRecord,
    KnowledgeSourceType,
    RetrievalResult,
)


class KnowledgeService:
    def __init__(
        self,
        config: Optional[KnowledgeStoreConfig] = None,
        store: Optional[KnowledgeStore] = None,
        retrieval_backend: Optional[RetrievalBackend] = None,
    ):
        self._config = config or KnowledgeStoreConfig()
        self._store = store or KnowledgeStore(self._config)
        self._retrieval_backend = retrieval_backend or BaselineLexicalRetrieval(self._store)

    def add_record(self, record: KnowledgeRecord) -> str:
        return self._store.add_record(record)

    def get_record(self, record_id: str) -> Optional[KnowledgeRecord]:
        return self._store.get_record(record_id)

    def update_record(self, record_id: str, **updates: Any) -> Optional[KnowledgeRecord]:
        return self._store.update_record(record_id, **updates)

    def delete_record(self, record_id: str) -> bool:
        return self._store.delete_record(record_id)

    def list_records(
        self,
        namespace: Optional[str] = None,
        source_type: Optional[KnowledgeSourceType] = None,
        project_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[KnowledgeRecord]:
        return self._store.list_records(
            namespace=namespace,
            source_type=source_type,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )

    def count(
        self,
        namespace: Optional[str] = None,
        source_type: Optional[KnowledgeSourceType] = None,
        project_id: Optional[str] = None,
    ) -> int:
        return self._store.count(namespace=namespace, source_type=source_type, project_id=project_id)

    def search(
        self,
        query: str,
        namespace: Optional[str] = None,
        source_type: Optional[KnowledgeSourceType] = None,
        metadata_filters: Optional[dict[str, Any]] = None,
        top_k: int = 10,
        project_id: Optional[str] = None,
    ) -> list[RetrievalResult]:
        if len(query) > self._config.max_query_length:
            raise ValidationError(f"Query exceeds max length: {self._config.max_query_length}")
        top_k = min(top_k, self._config.max_top_k)
        return self._retrieval_backend.search(
            query=query,
            namespace=namespace,
            source_type=source_type,
            metadata_filters=metadata_filters,
            top_k=top_k,
            project_id=project_id,
        )

    def add_failure_record(self, record: FailureRecord) -> str:
        return self._store.add_failure_record(record)

    def get_failure_record(self, failure_id: str) -> Optional[FailureRecord]:
        return self._store.get_failure_record(failure_id)

    def add_fix_record(self, record: FixRecord) -> str:
        return self._store.add_fix_record(record)

    def get_fix_record(self, fix_id: str) -> Optional[FixRecord]:
        return self._store.get_fix_record(fix_id)

    def list_fix_records_for_failure(self, failure_id: str) -> list[FixRecord]:
        return self._store.list_fix_records_for_failure(failure_id)

    def close(self) -> None:
        self._store.close()
