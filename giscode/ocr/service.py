from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from giscode.knowledge.service import KnowledgeService
from giscode.knowledge.types import KnowledgeRecord, KnowledgeSourceType
from giscode.ocr.config import OCRConfig
from giscode.ocr.engine import MockOCREngine, OCREngine
from giscode.ocr.errors import (
    OCRCancelledError,
    OCRPreprocessingError,
    OCRRuntimeUnavailableError,
    OCRTimeoutError,
    OCRValidationError,
)
from giscode.ocr.preprocessing import PreprocessingPipeline
from giscode.ocr.types import OCRRequest, OCRResult, OCRStatus
from giscode.task_control.manager import TaskManager


class OCRService:
    def __init__(
        self,
        engine: Optional[OCREngine] = None,
        config: Optional[OCRConfig] = None,
        task_manager: Optional[TaskManager] = None,
        knowledge_service: Optional[KnowledgeService] = None,
    ):
        self._engine = engine or MockOCREngine()
        self._config = config or OCRConfig()
        self._task_manager = task_manager
        self._knowledge_service = knowledge_service
        self._pipeline = PreprocessingPipeline()

    def process(self, request: OCRRequest) -> OCRResult:
        self._validate_request(request)
        self._check_task_control()

        start = time.time()
        result = self._engine.process(request)
        elapsed = time.time() - start
        result.processing_time = elapsed
        result.request_id = request.request_id
        result.source_type = request.source_type
        result.page = request.page
        result.region = request.region

        if elapsed > self._config.timeout:
            result.timed_out = True
            result.status = OCRStatus.TIMED_OUT
            result.error = f"OCR processing exceeded timeout of {self._config.timeout}s"

        if result.status == OCRStatus.CANCELLED:
            raise OCRCancelledError(result.error or "OCR was cancelled")
        if result.status == OCRStatus.TIMED_OUT:
            raise OCRTimeoutError(result.error or "OCR timed out")

        if result.status == OCRStatus.COMPLETED:
            result.language = request.language

        return result

    def ingest_result(self, result: OCRResult, namespace: str, project_id: Optional[str] = None) -> Optional[KnowledgeRecord]:
        if self._knowledge_service is None:
            return None
        if result.status != OCRStatus.COMPLETED or not result.extracted_text:
            return None
        record = KnowledgeRecord(
            record_id=f"ocr-{result.request_id}",
            content=result.extracted_text,
            source=f"ocr:{result.request_id}",
            source_type=KnowledgeSourceType.OCR,
            namespace=namespace,
            project_id=project_id,
            metadata={
                "language": result.language,
                "confidence": result.confidence,
                "processing_time": result.processing_time,
                "page": result.page,
                "region": str(result.region) if result.region else None,
            },
        )
        self._knowledge_service.add_record(record)
        return record

    def _validate_request(self, request: OCRRequest) -> None:
        if not request.request_id:
            raise OCRValidationError("request_id is required")
        if request.input_path is not None:
            path = Path(request.input_path)
            if not path.exists():
                raise OCRValidationError(f"Input file does not exist: {request.input_path}")
            if not path.is_file():
                raise OCRValidationError(f"Input path is not a file: {request.input_path}")
            if path.stat().st_size > self._config.max_input_size:
                raise OCRValidationError(f"Input file exceeds max size: {self._config.max_input_size}")
            if ".." in path.parts:
                raise OCRValidationError(f"Path traversal detected: {request.input_path}")
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".gif", ".webp", ".pdf"}:
                raise OCRValidationError(f"Unsupported file format: {path.suffix}")
        if request.input_bytes is not None:
            if len(request.input_bytes) > self._config.max_input_size:
                raise OCRValidationError(f"Input bytes exceed max size: {self._config.max_input_size}")

    def _check_task_control(self) -> None:
        if self._task_manager is None:
            return
        active = self._task_manager.get_active_task()
        if active is None:
            return
        if active.status.value == "cancelled":
            raise OCRCancelledError("Task was cancelled")
        if active.status.value == "timeout":
            raise OCRTimeoutError("Task timed out")
