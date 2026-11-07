from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from giscode.knowledge.chunker import DocumentChunk, chunk_document
from giscode.knowledge.config import KnowledgeStoreConfig
from giscode.knowledge.errors import ValidationError
from giscode.knowledge.loader import LoadedDocument, load_document
from giscode.knowledge.store import KnowledgeStore
from giscode.knowledge.types import KnowledgeRecord, KnowledgeSourceType


@dataclass
class IngestionResult:
    document_id: str
    source_path: str
    chunks_created: int
    records_added: int
    deduplicated: int
    content_hash: str
    chunks: list = field(default_factory=list)


def _generate_document_id(source_path: str, content_hash: str) -> str:
    """Generate a deterministic document ID from path and content hash."""
    payload = f"{source_path}:{content_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def ingest_document(
    file_path: Path,
    config: Optional[KnowledgeStoreConfig] = None,
    namespace: str = "default",
    project_id: Optional[str] = None,
    source_type: str = "document",
    extra_metadata: Optional[dict] = None,
    chunk_size: int = 1000,
    chunk_overlap: int = 100,
) -> IngestionResult:
    """
    Ingest a document through the full pipeline:
    load -> normalize -> chunk -> KnowledgeStore
    """
    if config is None:
        config = KnowledgeStoreConfig()
    store = KnowledgeStore(config)
    try:
        loaded = load_document(Path(file_path))
        document_id = _generate_document_id(loaded.source_path, loaded.content_hash)

        # Check for existing document with same content hash (skip for empty documents)
        is_empty = not loaded.content.strip()
        if not is_empty:
            existing = store._conn.execute(
                "SELECT record_id FROM content_hashes WHERE content_hash = ?",
                (loaded.content_hash,),
            ).fetchone()

            if existing:
                # Document already exists, return deduplication info
                existing_count = store._conn.execute(
                    "SELECT COUNT(*) FROM knowledge_records WHERE content_hash = ?",
                    (loaded.content_hash,),
                ).fetchone()[0]
                return IngestionResult(
                    document_id=document_id,
                    source_path=loaded.source_path,
                    chunks_created=0,
                    records_added=0,
                    deduplicated=existing_count,
                    content_hash=loaded.content_hash,
                )

        # Chunk the document
        chunks = chunk_document(
            loaded.content,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            document_id=document_id,
        )

        if not chunks:
            # Empty document - use unique placeholder per document
            placeholder_content = f"[EMPTY DOCUMENT: {document_id}]"
            chunks = [
                type("DocumentChunk", (), {
                    "document_id": document_id,
                    "chunk_index": 0,
                    "content": placeholder_content,
                    "start_char": 0,
                    "end_char": 0,
                    "metadata": {"is_last": True, "is_empty_placeholder": True},
                })()
            ]

        records_added = 0
        for chunk in chunks:
            chunk_id = f"{document_id}-chunk-{chunk.chunk_index}"
            record = KnowledgeRecord(
                record_id=chunk_id,
                content=chunk.content,
                source=f"{document_id}-chunk-{chunk.chunk_index}",
                source_type=KnowledgeSourceType.DOCUMENTATION,
                namespace="default",
                project_id=project_id,
                metadata={
                    "document_id": document_id,
                    "chunk_index": chunk.chunk_index,
                    "start_char": chunk.start_char,
                    "end_char": chunk.end_char,
                    "document_source_path": loaded.source_path,
                    "document_content_hash": loaded.content_hash,
                    "document_source_type": loaded.source_type,
                },
            )
            store.add_record(record)
            records_added += 1

        return IngestionResult(
            document_id=document_id,
            source_path=loaded.source_path,
            chunks_created=len(chunks),
            records_added=records_added,
            deduplicated=0,
            content_hash=loaded.content_hash,
            chunks=chunks,
        )
    finally:
        store.close()


def ingest_text(
    content: str,
    source_name: str,
    config: Optional[KnowledgeStoreConfig] = None,
    namespace: str = "default",
    project_id: Optional[str] = None,
    source_type: str = "text",
    extra_metadata: Optional[dict] = None,
    chunk_size: int = 1000,
    chunk_overlap: int = 100,
) -> IngestionResult:
    """Ingest raw text content directly."""
    if config is None:
        config = KnowledgeStoreConfig()
    store = KnowledgeStore(config)
    try:
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        if normalized.startswith("\ufeff"):
            normalized = normalized[1:]

        content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        document_id = _generate_document_id(source_name, content_hash)

        is_empty = not normalized.strip()
        if not is_empty:
            existing = store._conn.execute(
                "SELECT record_id FROM content_hashes WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()

            if existing:
                existing_count = store._conn.execute(
                    "SELECT COUNT(*) FROM knowledge_records WHERE content_hash = ?",
                    (content_hash,),
                ).fetchone()[0]
                return IngestionResult(
                    document_id=document_id,
                    source_path=source_name,
                    chunks_created=0,
                    records_added=0,
                    deduplicated=existing_count,
                    content_hash=content_hash,
                )

        chunks = chunk_document(
            normalized,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            document_id=document_id,
        )

        if not chunks:
            # Empty document - use unique placeholder per document
            placeholder_content = f"[EMPTY DOCUMENT: {document_id}]"
            chunks = [
                type("DocumentChunk", (), {
                    "document_id": document_id,
                    "chunk_index": 0,
                    "content": placeholder_content,
                    "start_char": 0,
                    "end_char": 0,
                    "metadata": {"is_last": True, "is_empty_placeholder": True},
                })()
            ]

        records_added = 0
        for chunk in chunks:
            chunk_id = f"{document_id}-chunk-{chunk.chunk_index}"
            record = KnowledgeRecord(
                record_id=chunk_id,
                content=chunk.content,
                source=f"{document_id}-chunk-{chunk.chunk_index}",
                source_type=KnowledgeSourceType.DOCUMENTATION,
                namespace="default",
                project_id=project_id,
                metadata={
                    "document_id": document_id,
                    "chunk_index": chunk.chunk_index,
                    "start_char": chunk.start_char,
                    "end_char": chunk.end_char,
                    "document_source_path": source_name,
                    "document_content_hash": content_hash,
                },
            )
            store.add_record(record)
            records_added += 1

        return IngestionResult(
            document_id=document_id,
            source_path=source_name,
            chunks_created=len(chunks),
            records_added=records_added,
            deduplicated=0,
            content_hash=content_hash,
            chunks=chunks,
        )
    finally:
        store.close()