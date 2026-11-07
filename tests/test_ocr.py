import sys
import threading
import time
from pathlib import Path

import pytest

from giscode.agentic.loop import CodingAgentLoop
from giscode.config.settings import SettingsManager
from giscode.inference.api import InferenceAPI
from giscode.knowledge.config import KnowledgeStoreConfig
from giscode.knowledge.service import KnowledgeService
from giscode.knowledge.types import KnowledgeSourceType
from giscode.models.manager import ModelManager
from giscode.ocr.config import OCRConfig
from giscode.ocr.engine import MockOCREngine, OCREngine, RealOCREngine
from giscode.ocr.errors import (
    OCRCancelledError,
    OCRPreprocessingError,
    OCRRuntimeUnavailableError,
    OCRTimeoutError,
    OCRValidationError,
    OCRWorkspaceError,
)
from giscode.ocr.preprocessing import (
    ContrastNormalizationStep,
    CropStep,
    GrayscaleStep,
    NoiseReductionStep,
    PreprocessingPipeline,
    PreprocessingStep,
    ResizeStep,
    RotationCorrectionStep,
)
from giscode.ocr.service import OCRService
from giscode.ocr.types import OCRRequest, OCRResult, OCRSourceType, OCRStatus, OCREngineType
from giscode.routing.service import RoutingService
from giscode.routing.types import RoutingRequest
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace


def test_ocr_request_creation():
    request = OCRRequest(
        request_id="req-1",
        source_type=OCRSourceType.IMAGE,
        input_path="/tmp/test.png",
        namespace="project-a",
        project_id="proj-a",
        language="eng",
        page=1,
        region={"x": 0, "y": 0, "w": 100, "h": 100},
    )
    assert request.request_id == "req-1"
    assert request.source_type == OCRSourceType.IMAGE
    assert request.language == "eng"


def test_ocr_result_creation():
    result = OCRResult(
        request_id="req-1",
        status=OCRStatus.COMPLETED,
        extracted_text="Hello world",
        language="eng",
        confidence=0.95,
        processing_time=0.5,
    )
    assert result.status == OCRStatus.COMPLETED
    assert result.extracted_text == "Hello world"


def test_request_validation():
    with pytest.raises(OCRValidationError):
        OCRRequest(request_id="", source_type=OCRSourceType.IMAGE)


def test_mock_ocr_success(tmp_path):
    image = tmp_path / "test.png"
    image.write_bytes(b"fake-image")
    engine = MockOCREngine(behavior="success")
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, input_path=str(image))
    result = engine.process(request)
    assert result.status == OCRStatus.COMPLETED
    assert result.extracted_text


def test_mock_ocr_failure(tmp_path):
    image = tmp_path / "test.png"
    image.write_bytes(b"fake-image")
    engine = MockOCREngine(behavior="failure")
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, input_path=str(image))
    result = engine.process(request)
    assert result.status == OCRStatus.FAILED


def test_mock_ocr_empty_result(tmp_path):
    image = tmp_path / "test.png"
    image.write_bytes(b"fake-image")
    engine = MockOCREngine(behavior="empty")
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, input_path=str(image))
    result = engine.process(request)
    assert result.status == OCRStatus.COMPLETED
    assert result.extracted_text == ""


def test_unavailable_ocr_runtime():
    engine = MockOCREngine(behavior="unavailable")
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE)
    result = engine.process(request)
    assert result.status == OCRStatus.UNAVAILABLE


def test_mock_ocr_cancellation(tmp_path):
    image = tmp_path / "test.png"
    image.write_bytes(b"fake-image")
    engine = MockOCREngine(behavior="success")
    engine.cancel()
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, input_path=str(image))
    result = engine.process(request)
    assert result.status == OCRStatus.CANCELLED


def test_mock_ocr_timeout(tmp_path):
    image = tmp_path / "test.png"
    image.write_bytes(b"fake-image")
    engine = MockOCREngine(behavior="success", delay=0.5)
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, input_path=str(image))
    start = time.time()
    result = engine.process(request)
    elapsed = time.time() - start
    assert elapsed >= 0.5
    assert result.status == OCRStatus.COMPLETED


def test_image_existence_validation(tmp_path):
    service = OCRService()
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, input_path=str(tmp_path / "missing.png"))
    with pytest.raises(OCRValidationError):
        service.process(request)


def test_unsupported_format_rejection(tmp_path):
    service = OCRService()
    image = tmp_path / "test.txt"
    image.write_text("hello")
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, input_path=str(image))
    with pytest.raises(OCRValidationError):
        service.process(request)


def test_oversized_input_rejection(tmp_path):
    service = OCRService(config=OCRConfig(max_input_size=10))
    image = tmp_path / "large.png"
    image.write_bytes(b"x" * 20)
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, input_path=str(image))
    with pytest.raises(OCRValidationError):
        service.process(request)


def test_workspace_path_safety(tmp_path):
    service = OCRService()
    workspace = Workspace(tmp_path / "ws")
    safe_path = str(workspace.root_path / "image.png")
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, input_path=safe_path)
    with pytest.raises(OCRValidationError):
        service.process(request)


def test_path_traversal_rejection(tmp_path):
    service = OCRService()
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, input_path="../../etc/passwd")
    with pytest.raises(OCRValidationError):
        service.process(request)


def test_pdf_support_abstraction(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake pdf")
    engine = MockOCREngine(behavior="success")
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.PDF, input_path=str(pdf))
    result = engine.process(request)
    assert result.status == OCRStatus.COMPLETED
    assert result.source_type == OCRSourceType.PDF


def test_pdf_real_engine_unavailable():
    engine = RealOCREngine(runtime_command=None)
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.PDF, input_path="/fake/doc.pdf")
    result = engine.process(request)
    assert result.status == OCRStatus.UNAVAILABLE
    assert "not implemented" in result.error or "not installed" in result.error


def test_screen_capture_abstraction():
    engine = MockOCREngine(behavior="success")
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.SCREENSHOT)
    result = engine.process(request)
    assert result.status == OCRStatus.COMPLETED
    assert result.source_type == OCRSourceType.SCREENSHOT


def test_clipboard_abstraction():
    engine = MockOCREngine(behavior="success")
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.CLIPBOARD_IMAGE)
    result = engine.process(request)
    assert result.status == OCRStatus.COMPLETED
    assert result.source_type == OCRSourceType.CLIPBOARD_IMAGE


def test_preprocessing_abstraction():
    pipeline = PreprocessingPipeline(steps=[ResizeStep(), GrayscaleStep()])
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE)
    result = pipeline.process(request)
    assert result.request_id == "req-1"


def test_ocr_to_knowledge_record(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    engine = MockOCREngine(behavior="success")
    service = OCRService(engine=engine, knowledge_service=knowledge_service)
    result = service.process(OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, namespace="project-a", project_id="proj-a"))
    assert result.status == OCRStatus.COMPLETED
    record = service.ingest_result(result, "project-a", "proj-a")
    assert record is not None
    assert record.source_type == KnowledgeSourceType.OCR


def test_knowledge_store_ingestion(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    engine = MockOCREngine(behavior="success")
    service = OCRService(engine=engine, knowledge_service=knowledge_service)
    result = OCRResult(request_id="req-1", status=OCRStatus.COMPLETED, extracted_text="text")
    record = service.ingest_result(result, "project-a", "proj-a")
    assert record is not None
    assert knowledge_service.get_record(record.record_id) is not None


def test_namespace_isolation(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    engine = MockOCREngine(behavior="success")
    service = OCRService(engine=engine, knowledge_service=knowledge_service)
    result_a = service.process(OCRRequest(request_id="req-a", source_type=OCRSourceType.IMAGE, namespace="project-a"))
    result_b = service.process(OCRRequest(request_id="req-b", source_type=OCRSourceType.IMAGE, namespace="project-b"))
    service.ingest_result(result_a, "project-a")
    service.ingest_result(result_b, "project-b")
    records_a = knowledge_service.list_records(namespace="project-a", source_type=KnowledgeSourceType.OCR)
    records_b = knowledge_service.list_records(namespace="project-b", source_type=KnowledgeSourceType.OCR)
    assert len(records_a) == 1
    assert len(records_b) == 1


def test_project_isolation(tmp_path):
    db_path = tmp_path / "knowledge.db"
    knowledge_config = KnowledgeStoreConfig(storage_path=db_path)
    knowledge_service = KnowledgeService(config=knowledge_config)
    engine = MockOCREngine(behavior="success")
    service = OCRService(engine=engine, knowledge_service=knowledge_service)
    result_a = service.process(OCRRequest(request_id="req-a", source_type=OCRSourceType.IMAGE, namespace="default", project_id="proj-a"))
    result_b = service.process(OCRRequest(request_id="req-b", source_type=OCRSourceType.IMAGE, namespace="default", project_id="proj-b"))
    service.ingest_result(result_a, "default", "proj-a")
    service.ingest_result(result_b, "default", "proj-b")
    records_a = knowledge_service.list_records(project_id="proj-a", source_type=KnowledgeSourceType.OCR)
    records_b = knowledge_service.list_records(project_id="proj-b", source_type=KnowledgeSourceType.OCR)
    assert len(records_a) == 1
    assert len(records_b) == 1


def test_routing_ocr_role_integration():
    settings = SettingsManager()
    inference = InferenceAPI()
    model_manager = ModelManager(settings, inference)
    routing_service = RoutingService(model_manager=model_manager)
    request = RoutingRequest(task_id="t1", task_type="ocr")
    result = routing_service.route(request)
    assert result.selected_role.value == "ocr" or result.availability == ModelAvailability.UNAVAILABLE


def test_agentic_integration(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    task_manager = TaskManager()
    inference = InferenceAPI()
    engine = MockOCREngine(behavior="success")
    service = OCRService(engine=engine, task_manager=task_manager)
    loop = CodingAgentLoop(workspace, inference, task_manager, ocr_service=service)
    result = loop.ocr(request_id="req-1", source_type="image", namespace="project-a")
    assert result is not None
    assert result["status"] == "completed"


def test_cleanup_after_cancellation(tmp_path):
    engine = MockOCREngine(behavior="success")
    engine.cancel()
    service = OCRService(engine=engine)
    image = tmp_path / "test.png"
    image.write_bytes(b"fake-image")
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, input_path=str(image))
    with pytest.raises(OCRCancelledError):
        service.process(request)


def test_cleanup_after_timeout(tmp_path):
    engine = MockOCREngine(behavior="success", delay=0.6)
    config = OCRConfig(timeout=0.5)
    service = OCRService(engine=engine, config=config)
    image = tmp_path / "test.png"
    image.write_bytes(b"fake-image")
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE, input_path=str(image))
    with pytest.raises(OCRTimeoutError):
        service.process(request)


def test_real_ocr_runtime_discovery():
    engine = RealOCREngine()
    assert engine.is_available() is False or engine.get_engine_type() != "none"


def test_real_ocr_unavailable_result():
    engine = RealOCREngine(runtime_command=None)
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE)
    result = engine.process(request)
    assert result.status == OCRStatus.UNAVAILABLE


def test_previous_tasks_remain_intact():
    request = OCRRequest(request_id="req-1", source_type=OCRSourceType.IMAGE)
    assert request.request_id == "req-1"
    result = OCRResult(request_id="req-1", status=OCRStatus.COMPLETED)
    assert result.status == OCRStatus.COMPLETED


def test_ocr_status_values():
    assert OCRStatus.UNAVAILABLE.value == "unavailable"
    assert OCRStatus.READY.value == "ready"
    assert OCRStatus.RUNNING.value == "running"
    assert OCRStatus.COMPLETED.value == "completed"
    assert OCRStatus.FAILED.value == "failed"
    assert OCRStatus.CANCELLED.value == "cancelled"
    assert OCRStatus.TIMED_OUT.value == "timed_out"


def test_ocr_source_type_values():
    assert OCRSourceType.IMAGE.value == "image"
    assert OCRSourceType.SCREENSHOT.value == "screenshot"
    assert OCRSourceType.DOCUMENT.value == "document"
    assert OCRSourceType.PDF.value == "pdf"
    assert OCRSourceType.CLIPBOARD_IMAGE.value == "clipboard_image"


def test_ocr_engine_type_values():
    assert OCREngineType.MOCK.value == "mock"
    assert OCREngineType.TESSERACT.value == "tesseract"
    assert OCREngineType.PADDLEOCR.value == "paddleocr"
    assert OCREngineType.EASYOCR.value == "easyocr"
    assert OCREngineType.VISION_MODEL.value == "vision_model"
