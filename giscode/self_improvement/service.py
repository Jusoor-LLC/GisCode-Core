import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from giscode.knowledge.service import KnowledgeService
from giscode.knowledge.types import KnowledgeRecord, KnowledgeSourceType
from giscode.self_improvement.config import SelfImprovementConfig
from giscode.self_improvement.types import FailFixPair, FixStatus, LearningEvent


class SelfImprovementService:
    def __init__(
        self,
        config: Optional[SelfImprovementConfig] = None,
        knowledge_service: Optional[KnowledgeService] = None,
    ) -> None:
        self._config = config or SelfImprovementConfig()
        self._knowledge_service = knowledge_service
        self._pairs: list[FailFixPair] = []
        self._seen_fingerprints: set = set()

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def _compute_fingerprint(self, failure_type: str, command: str, exit_code: int, error_text: str, fix_proposal: str) -> str:
        normalized = f"{failure_type}:{command}:{exit_code}:{error_text[:500]}:{fix_proposal[:200]}"
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def record_failure(
        self,
        failure_type: str,
        command: str,
        exit_code: int = -1,
        error_text: str = "",
        namespace: str = "default",
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[FailFixPair]:
        if not self._config.enabled:
            return None

        pair_id = f"fail-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        pair = FailFixPair(
            pair_id=pair_id,
            namespace=namespace,
            project_id=project_id,
            task_id=task_id,
            failure_type=failure_type,
            error_text=error_text,
            command=command,
            exit_code=exit_code,
            status=FixStatus.FAILED_ATTEMPT,
            metadata=metadata or {},
        )
        self._pairs.append(pair)
        self._store_to_knowledge(pair)
        return pair

    def record_candidate_fix(
        self,
        failure_pair_id: str,
        fix_proposal: str,
        files_changed: Optional[list[str]] = None,
        namespace: str = "default",
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[FailFixPair]:
        if not self._config.enabled:
            return None

        existing = next((p for p in self._pairs if p.pair_id == failure_pair_id), None)
        if existing is None:
            return None

        existing.fix_proposal = fix_proposal
        existing.files_changed = files_changed or []
        existing.status = FixStatus.CANDIDATE_FIX
        existing.updated_at = datetime.now().isoformat()
        if metadata:
            existing.metadata.update(metadata)
        self._store_to_knowledge(existing)
        return existing

    def record_verified_fix(
        self,
        failure_pair_id: str,
        verification_result: str = "",
        fix_succeeded: bool = True,
        metadata: Optional[dict] = None,
    ) -> Optional[FailFixPair]:
        if not self._config.enabled:
            return None

        existing = next((p for p in self._pairs if p.pair_id == failure_pair_id), None)
        if existing is None:
            return None

        fingerprint = self._compute_fingerprint(
            existing.failure_type,
            existing.command,
            existing.exit_code,
            existing.error_text,
            existing.fix_proposal,
        )
        if fingerprint in self._seen_fingerprints:
            return existing

        existing.status = FixStatus.VERIFIED_PAIR if fix_succeeded else FixStatus.FAILED_ATTEMPT
        existing.fix_succeeded = fix_succeeded
        existing.verification_result = verification_result
        existing.updated_at = datetime.now().isoformat()
        if metadata:
            existing.metadata.update(metadata)

        self._seen_fingerprints.add(fingerprint)
        self._store_to_knowledge(existing)
        return existing

    def _store_to_knowledge(self, pair: FailFixPair) -> None:
        if not self._config.knowledge_store_integration or self._knowledge_service is None:
            return

        try:
            record_id = f"self-improvement-{pair.pair_id}"
            content_parts = []
            if pair.error_text:
                content_parts.append(pair.error_text)
            if pair.fix_proposal:
                content_parts.append(f"Fix: {pair.fix_proposal}")
            content = " | ".join(content_parts) if content_parts else pair.failure_type

            metadata = {
                "pair_id": pair.pair_id,
                "task_id": pair.task_id,
                "failure_type": pair.failure_type,
                "error_signature": pair.error_signature,
                "error_text": pair.error_text,
                "command": pair.command,
                "exit_code": pair.exit_code,
                "fix_proposal": pair.fix_proposal,
                "files_changed": pair.files_changed,
                "verification_result": pair.verification_result,
                "fix_succeeded": pair.fix_succeeded,
                "status": pair.status.value,
                "created_at": pair.created_at,
                "updated_at": pair.updated_at,
            }
            existing = self._knowledge_service.get_record(record_id)
            if existing is not None:
                self._knowledge_service.update_record(
                    record_id,
                    content=content,
                    source=f"self-improvement:{pair.status.value}",
                    metadata=metadata,
                )
            else:
                record = KnowledgeRecord(
                    record_id=record_id,
                    content=content,
                    source=f"self-improvement:{pair.status.value}",
                    source_type=KnowledgeSourceType.TASK,
                    namespace=pair.namespace,
                    project_id=pair.project_id,
                    metadata=metadata,
                )
                pair.knowledge_record_id = self._knowledge_service.add_record(record)
        except Exception:
            pass

    def retrieve_fix_patterns(
        self,
        query: str,
        namespace: str = "default",
        project_id: Optional[str] = None,
        top_k: int = 5,
    ) -> list[dict]:
        if not self._config.enabled or self._knowledge_service is None:
            return []

        try:
            results = self._knowledge_service.search(
                query=query,
                namespace=namespace,
                source_type=KnowledgeSourceType.TASK,
                metadata_filters={"status": self._config.min_verification_status},
                top_k=min(top_k, self._config.max_retrieved_patterns),
                project_id=project_id,
            )
        except Exception:
            return []

        patterns = []
        for r in results:
            record = r.record
            meta = record.metadata or {}
            patterns.append({
                "pair_id": meta.get("pair_id"),
                "record_id": record.record_id,
                "failure_type": meta.get("failure_type"),
                "error_signature": meta.get("error_signature"),
                "fix_proposal": meta.get("fix_proposal"),
                "files_changed": meta.get("files_changed", []),
                "verification_result": meta.get("verification_result"),
                "fix_succeeded": meta.get("fix_succeeded"),
                "status": meta.get("status"),
                "score": r.score,
                "namespace": record.namespace,
                "project_id": record.project_id,
            })
        return patterns

    def get_pairs(self, namespace: Optional[str] = None, status: Optional[FixStatus] = None) -> list[FailFixPair]:
        pairs = list(self._pairs)
        if namespace is not None:
            pairs = [p for p in pairs if p.namespace == namespace]
        if status is not None:
            pairs = [p for p in pairs if p.status == status]
        return pairs

    def export_dataset(self, output_path: Optional[str] = None) -> dict:
        output = Path(output_path or self._config.dataset_export_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        verified_pairs = [p for p in self._pairs if p.status == FixStatus.VERIFIED_PAIR]
        dataset = {
            "version": "0.1.0",
            "exported_at": datetime.now().isoformat(),
            "total_pairs": len(verified_pairs),
            "pairs": [],
        }
        for pair in verified_pairs:
            dataset["pairs"].append({
                "pair_id": pair.pair_id,
                "namespace": pair.namespace,
                "project_id": pair.project_id,
                "task_id": pair.task_id,
                "failure_type": pair.failure_type,
                "error_signature": pair.error_signature,
                "error_text": pair.error_text,
                "command": pair.command,
                "exit_code": pair.exit_code,
                "fix_proposal": pair.fix_proposal,
                "files_changed": pair.files_changed,
                "verification_result": pair.verification_result,
                "fix_succeeded": pair.fix_succeeded,
                "created_at": pair.created_at,
                "updated_at": pair.updated_at,
                "metadata": pair.metadata,
            })

        try:
            with open(output, "w", encoding="utf-8") as f:
                json.dump(dataset, f, indent=2)
        except Exception:
            pass

        return dataset

    def record_learning_event(self, event: LearningEvent) -> None:
        if not self._config.enabled or self._knowledge_service is None:
            return
        try:
            record = KnowledgeRecord(
                record_id=f"learning-{event.event_type}-{event.pair_id}",
                content=event.message,
                source=f"monitoring:{event.event_type}",
                source_type=KnowledgeSourceType.TASK,
                namespace=event.namespace,
                project_id=event.project_id,
                metadata={
                    "event_type": event.event_type,
                    "pair_id": event.pair_id,
                    "status": event.status,
                    "timestamp": event.timestamp,
                    **event.metadata,
                },
            )
            self._knowledge_service.add_record(record)
        except Exception:
            pass
