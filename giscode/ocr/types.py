from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from giscode.ocr.errors import OCRValidationError


class OCRSourceType(Enum):
    IMAGE = "image"
    SCREENSHOT = "screenshot"
    DOCUMENT = "document"
    PDF = "pdf"
    CLIPBOARD_IMAGE = "clipboard_image"


class OCRStatus(Enum):
    UNAVAILABLE = "unavailable"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class OCREngineType(Enum):
    MOCK = "mock"
    TESSERACT = "tesseract"
    PADDLEOCR = "paddleocr"
    EASYOCR = "easyocr"
    VISION_MODEL = "vision_model"


@dataclass
class OCRRequest:
    request_id: str
    source_type: OCRSourceType
    input_path: Optional[str] = None
    input_bytes: Optional[bytes] = None
    namespace: str = "default"
    project_id: Optional[str] = None
    language: str = "eng"
    page: Optional[int] = None
    region: Optional[dict] = None
    metadata: dict = field(default_factory=dict)
    task_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise OCRValidationError("request_id is required")


@dataclass
class OCRResult:
    request_id: str
    status: OCRStatus
    extracted_text: str = ""
    source_type: Optional[OCRSourceType] = None
    language: str = "eng"
    confidence: Optional[float] = None
    processing_time: float = 0.0
    page: Optional[int] = None
    region: Optional[dict] = None
    error: Optional[str] = None
    timed_out: bool = False
    cancelled: bool = False
    metadata: dict = field(default_factory=dict)
