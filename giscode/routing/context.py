from typing import Optional

from giscode.knowledge.service import KnowledgeService
from giscode.routing.config import ContextBudget
from giscode.routing.errors import ContextAssemblyError
from giscode.routing.types import (
    ContextBundle,
    ModelRole,
    RoutingRequest,
    RoutingResult,
)


class ContextAssembler:
    def __init__(
        self,
        knowledge_service: KnowledgeService,
        context_budget: Optional[ContextBudget] = None,
    ):
        self._knowledge_service = knowledge_service
        self._context_budget = context_budget or ContextBudget()

    def assemble(
        self,
        routing_result: RoutingResult,
        request: RoutingRequest,
    ) -> ContextBundle:
        if not self._knowledge_service:
            raise ContextAssemblyError("Knowledge service is not configured")

        task_text = request.task_description or request.task_type
        namespace = request.namespace or "default"
        project_id = request.project_id

        query = task_text
        if len(query) > self._context_budget.max_query_length:
            query = query[: self._context_budget.max_query_length]

        top_k = self._context_budget.max_records
        try:
            results = self._knowledge_service.search(
                query=query,
                namespace=namespace,
                top_k=top_k,
                project_id=project_id,
            )
        except Exception as e:
            raise ContextAssemblyError(f"Knowledge retrieval failed: {e}") from e

        records = []
        total_chars = 0
        truncation_occurred = False
        truncation_reason = None

        for result in results:
            record = {
                "record_id": result.record.record_id,
                "content": result.record.content,
                "source": result.record.source,
                "source_type": result.record.source_type.value,
                "score": result.score,
                "namespace": result.record.namespace,
            }
            content_len = len(record["content"])
            if total_chars + content_len > self._context_budget.max_context_chars:
                truncation_occurred = True
                truncation_reason = "context_budget_exceeded"
                break
            if content_len > self._context_budget.max_record_chars:
                record["content"] = record["content"][: self._context_budget.max_record_chars]
                truncation_occurred = True
                truncation_reason = "record_truncated"
            records.append(record)
            total_chars += len(record["content"])

        formatted_parts = []
        for record in records:
            formatted_parts.append(
                f"[{record['source_type']}] {record['source']}: {record['content']}"
            )
        formatted_context = "\n".join(formatted_parts)

        return ContextBundle(
            task_id=request.task_id,
            task_text=task_text,
            selected_role=routing_result.selected_role,
            selected_model=routing_result.selected_model,
            namespace=namespace,
            project_id=project_id,
            retrieved_records=records,
            formatted_context=formatted_context,
            record_count=len(records),
            truncation_occurred=truncation_occurred,
            truncation_reason=truncation_reason,
            context_char_count=total_chars,
            metadata={
                "availability": routing_result.availability.value,
                "fallback_used": routing_result.fallback_used,
            },
        )
