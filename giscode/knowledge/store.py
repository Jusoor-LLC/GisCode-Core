import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from giscode.config.paths import profile_root
from giscode.knowledge.config import KnowledgeStoreConfig
from giscode.knowledge.errors import (
    DuplicateRecordError,
    NamespaceError,
    PathSafetyError,
    RecordNotFoundError,
    StorageError,
    ValidationError,
)
from giscode.knowledge.index import compute_content_hash
from giscode.knowledge.types import (
    FailureRecord,
    FixRecord,
    KnowledgeRecord,
    KnowledgeSourceType,
)


class KnowledgeStore:
    def __init__(self, config: Optional[KnowledgeStoreConfig] = None):
        if config is None:
            raise TypeError("KnowledgeStore requires a KnowledgeStoreConfig")
        self._config = config
        self._lock = threading.Lock()
        self._ensure_storage_path()
        self._conn = self._create_connection()
        self._initialize_schema()

    def _ensure_storage_path(self) -> None:
        storage_path = self._config.storage_path
        if storage_path is None:
            raise StorageError("Storage path is not configured")
        resolved = Path(storage_path).expanduser().resolve()
        try:
            resolved.relative_to(profile_root())
        except ValueError:
            raise PathSafetyError(f"Storage path is outside home directory: {resolved}")
        resolved.parent.mkdir(parents=True, exist_ok=True)

    def _create_connection(self) -> sqlite3.Connection:
        if self._config.storage_path is None:
            raise StorageError("Storage path is not configured")
        conn = sqlite3.connect(
            str(self._config.storage_path),
            check_same_thread=False,
            isolation_level="DEFERRED",
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _initialize_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_records (
                record_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                source_type TEXT NOT NULL,
                namespace TEXT NOT NULL,
                project_id TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                content_hash TEXT,
                parent_id TEXT,
                tags TEXT DEFAULT '[]',
                embedding TEXT
            );
            CREATE TABLE IF NOT EXISTS failure_records (
                failure_id TEXT PRIMARY KEY,
                error_signature TEXT NOT NULL,
                error_text TEXT NOT NULL,
                phase TEXT NOT NULL,
                context TEXT NOT NULL,
                project_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS fix_records (
                fix_id TEXT PRIMARY KEY,
                failure_id TEXT NOT NULL,
                attempted_fix TEXT NOT NULL,
                result TEXT NOT NULL,
                verification_status TEXT NOT NULL,
                project_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS content_hashes (
                content_hash TEXT PRIMARY KEY,
                record_id TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_namespace ON knowledge_records(namespace);
            CREATE INDEX IF NOT EXISTS idx_knowledge_project ON knowledge_records(project_id);
            CREATE INDEX IF NOT EXISTS idx_knowledge_source_type ON knowledge_records(source_type);
            CREATE INDEX IF NOT EXISTS idx_knowledge_hash ON knowledge_records(content_hash);
            CREATE INDEX IF NOT EXISTS idx_failure_project ON failure_records(project_id);
            CREATE INDEX IF NOT EXISTS idx_failure_namespace ON failure_records(namespace);
            CREATE INDEX IF NOT EXISTS idx_fix_failure ON fix_records(failure_id);
        """)
        self._conn.commit()

    def add_record(self, record: KnowledgeRecord) -> str:
        if not record.content or not record.source:
            raise ValidationError("content and source are required")
        if not record.namespace:
            raise NamespaceError("namespace is required")
        if len(record.content) > self._config.max_record_size:
            raise ValidationError(f"Record content exceeds max size: {self._config.max_record_size}")
        if not record.record_id:
            raise ValidationError("record_id is required")

        record.updated_at = datetime.now().isoformat()
        if not record.created_at:
            record.created_at = record.updated_at

        if self._config.enable_content_hash and not record.content_hash:
            record.content_hash = compute_content_hash(record.content, record.namespace, record.source)

        with self._lock:
            if record.content_hash and self._config.enable_deduplication:
                existing = self._conn.execute(
                    "SELECT record_id FROM content_hashes WHERE content_hash = ?",
                    (record.content_hash,),
                ).fetchone()
                if existing:
                    raise DuplicateRecordError(f"Duplicate content detected: {record.content_hash}")

            try:
                self._conn.execute(
                    """
                    INSERT INTO knowledge_records (
                        record_id, content, source, source_type, namespace, project_id,
                        metadata, created_at, updated_at, content_hash, parent_id, tags, embedding
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.record_id,
                        record.content,
                        record.source,
                        record.source_type.value,
                        record.namespace,
                        record.project_id,
                        str(record.metadata),
                        record.created_at,
                        record.updated_at,
                        record.content_hash,
                        record.parent_id,
                        str(record.tags),
                        str(record.embedding) if record.embedding else None,
                    ),
                )
                if record.content_hash:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO content_hashes (content_hash, record_id) VALUES (?, ?)",
                        (record.content_hash, record.record_id),
                    )
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                self._conn.rollback()
                raise StorageError(f"Failed to add record: {e}") from e

        return record.record_id

    def get_record(self, record_id: str) -> Optional[KnowledgeRecord]:
        row = self._conn.execute(
            "SELECT * FROM knowledge_records WHERE record_id = ?", (record_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_record(row)

    def update_record(self, record_id: str, **updates: Any) -> Optional[KnowledgeRecord]:
        existing = self.get_record(record_id)
        if not existing:
            return None

        allowed_fields = {
            "content", "source", "source_type", "metadata", "tags", "parent_id", "embedding"
        }
        set_clauses = []
        values = []
        for key, value in updates.items():
            if key not in allowed_fields:
                continue
            if key == "source_type" and isinstance(value, KnowledgeSourceType):
                value = value.value
            if key == "tags" and value is not None:
                value = list(value)
            if key == "embedding" and value is not None:
                value = list(value)
            if key == "metadata" and isinstance(value, dict):
                value = str(value)
            set_clauses.append(f"{key} = ?")
            values.append(value)

        if not set_clauses:
            return existing

        set_clauses.append("updated_at = ?")
        values.append(datetime.now().isoformat())
        values.append(record_id)

        with self._lock:
            self._conn.execute(
                f"UPDATE knowledge_records SET {', '.join(set_clauses)} WHERE record_id = ?",
                values,
            )
            self._conn.commit()

        return self.get_record(record_id)

    def delete_record(self, record_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM knowledge_records WHERE record_id = ?", (record_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def list_records(
        self,
        namespace: Optional[str] = None,
        source_type: Optional[KnowledgeSourceType] = None,
        project_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[KnowledgeRecord]:
        query = "SELECT * FROM knowledge_records WHERE 1=1"
        params = []
        if namespace:
            query += " AND namespace = ?"
            params.append(namespace)
        if source_type:
            query += " AND source_type = ?"
            params.append(source_type.value)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def count(
        self,
        namespace: Optional[str] = None,
        source_type: Optional[KnowledgeSourceType] = None,
        project_id: Optional[str] = None,
    ) -> int:
        query = "SELECT COUNT(*) FROM knowledge_records WHERE 1=1"
        params = []
        if namespace:
            query += " AND namespace = ?"
            params.append(namespace)
        if source_type:
            query += " AND source_type = ?"
            params.append(source_type.value)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        row = self._conn.execute(query, params).fetchone()
        return row[0] if row else 0

    def add_failure_record(self, record: FailureRecord) -> str:
        if not all([record.failure_id, record.error_signature, record.error_text, record.project_id, record.namespace]):
            raise ValidationError("failure_id, error_signature, error_text, project_id, and namespace are required")
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO failure_records (
                        failure_id, error_signature, error_text, phase, context,
                        project_id, namespace, timestamp, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.failure_id,
                        record.error_signature,
                        record.error_text,
                        record.phase,
                        record.context,
                        record.project_id,
                        record.namespace,
                        record.timestamp,
                        str(record.metadata),
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                self._conn.rollback()
                raise StorageError(f"Failed to add failure record: {e}") from e
        return record.failure_id

    def get_failure_record(self, failure_id: str) -> Optional[FailureRecord]:
        row = self._conn.execute(
            "SELECT * FROM failure_records WHERE failure_id = ?", (failure_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_failure(row)

    def add_fix_record(self, record: FixRecord) -> str:
        if not all([record.fix_id, record.failure_id, record.attempted_fix, record.project_id, record.namespace]):
            raise ValidationError("fix_id, failure_id, attempted_fix, project_id, and namespace are required")
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO fix_records (
                        fix_id, failure_id, attempted_fix, result, verification_status,
                        project_id, namespace, timestamp, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.fix_id,
                        record.failure_id,
                        record.attempted_fix,
                        record.result,
                        record.verification_status,
                        record.project_id,
                        record.namespace,
                        record.timestamp,
                        str(record.metadata),
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                self._conn.rollback()
                raise StorageError(f"Failed to add fix record: {e}") from e
        return record.fix_id

    def get_fix_record(self, fix_id: str) -> Optional[FixRecord]:
        row = self._conn.execute(
            "SELECT * FROM fix_records WHERE fix_id = ?", (fix_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_fix(row)

    def list_fix_records_for_failure(self, failure_id: str) -> list[FixRecord]:
        rows = self._conn.execute(
            "SELECT * FROM fix_records WHERE failure_id = ? ORDER BY timestamp ASC", (failure_id,)
        ).fetchall()
        return [self._row_to_fix(row) for row in rows]

    def _row_to_record(self, row: sqlite3.Row) -> KnowledgeRecord:
        return KnowledgeRecord(
            record_id=row["record_id"],
            content=row["content"],
            source=row["source"],
            source_type=KnowledgeSourceType(row["source_type"]),
            namespace=row["namespace"],
            project_id=row["project_id"],
            metadata=eval(row["metadata"]) if row["metadata"] else {},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            content_hash=row["content_hash"],
            parent_id=row["parent_id"],
            tags=eval(row["tags"]) if row["tags"] else [],
            embedding=eval(row["embedding"]) if row["embedding"] else None,
        )

    def _row_to_failure(self, row: sqlite3.Row) -> FailureRecord:
        return FailureRecord(
            failure_id=row["failure_id"],
            error_signature=row["error_signature"],
            error_text=row["error_text"],
            phase=row["phase"],
            context=row["context"],
            project_id=row["project_id"],
            namespace=row["namespace"],
            timestamp=row["timestamp"],
            metadata=eval(row["metadata"]) if row["metadata"] else {},
        )

    def _row_to_fix(self, row: sqlite3.Row) -> FixRecord:
        return FixRecord(
            fix_id=row["fix_id"],
            failure_id=row["failure_id"],
            attempted_fix=row["attempted_fix"],
            result=row["result"],
            verification_status=row["verification_status"],
            project_id=row["project_id"],
            namespace=row["namespace"],
            timestamp=row["timestamp"],
            metadata=eval(row["metadata"]) if row["metadata"] else {},
        )

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
