from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from giscode.knowledge.errors import RetrievalError
from giscode.knowledge.index import compute_token_overlap_score
from giscode.knowledge.types import KnowledgeRecord, KnowledgeSourceType, RetrievalResult


class RetrievalBackend(ABC):
    @abstractmethod
    def search(
        self,
        query: str,
        namespace: Optional[str] = None,
        source_type: Optional[KnowledgeSourceType] = None,
        metadata_filters: Optional[dict[str, Any]] = None,
        top_k: int = 10,
    ) -> list[RetrievalResult]:
        pass


class BaselineLexicalRetrieval(RetrievalBackend):
    def __init__(self, store: Any):
        self._store = store

    def search(
        self,
        query: str,
        namespace: Optional[str] = None,
        source_type: Optional[KnowledgeSourceType] = None,
        metadata_filters: Optional[dict[str, Any]] = None,
        top_k: int = 10,
        project_id: Optional[str] = None,
    ) -> list[RetrievalResult]:
        if not query or not query.strip():
            return []

        candidates = self._store.list_records(namespace=namespace, source_type=source_type, project_id=project_id)
        scored = []
        query_tokens = set(query.lower().split())
        for record in candidates:
            if metadata_filters:
                match = True
                for key, value in metadata_filters.items():
                    if record.metadata.get(key) != value:
                        match = False
                        break
                if not match:
                    continue
            score = compute_token_overlap_score(query, record.content)
            if score > 0:
                scored.append(
                    RetrievalResult(
                        record=record,
                        score=score,
                        retrieval_metadata={"method": "baseline_lexical", "query_tokens": len(query_tokens)},
                    )
                )
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]


class VectorRetrieval(RetrievalBackend):
    def __init__(self, store: Any, embedding_backend: Optional[Any] = None):
        self._store = store
        self._embedding_backend = embedding_backend

    def search(
        self,
        query: str,
        namespace: Optional[str] = None,
        source_type: Optional[KnowledgeSourceType] = None,
        metadata_filters: Optional[dict[str, Any]] = None,
        top_k: int = 10,
        project_id: Optional[str] = None,
    ) -> list[RetrievalResult]:
        if self._embedding_backend is None:
            raise RetrievalError("Vector backend is not configured")
        raise NotImplementedError("Vector retrieval is not yet implemented")
