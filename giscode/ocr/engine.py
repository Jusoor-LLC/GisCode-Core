import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from giscode.ocr.errors import (
    OCRCancelledError,
    OCRPreprocessingError,
    OCRRuntimeUnavailableError,
    OCRTimeoutError,
    OCRValidationError,
)
from giscode.ocr.types import OCRRequest, OCRResult, OCRStatus


class OCREngine(ABC):
    @abstractmethod
    def process(self, request: OCRRequest) -> OCRResult:
        pass

    @abstractmethod
    def is_available(self) -> bool:
        pass

    def get_engine_type(self) -> str:
        return "abstract"


class MockOCREngine(OCREngine):
    def __init__(self, behavior: str = "success", delay: float = 0.0):
        self._behavior = behavior
        self._delay = delay
        self._cancelled = False

    def process(self, request: OCRRequest) -> OCRResult:
        if self._delay > 0:
            time.sleep(self._delay)

        if self._behavior == "unavailable":
            return OCRResult(
                request_id=request.request_id,
                status=OCRStatus.UNAVAILABLE,
                error="OCR runtime is unavailable",
            )

        if self._behavior == "failure":
            return OCRResult(
                request_id=request.request_id,
                status=OCRStatus.FAILED,
                error="Mock OCR engine failed",
            )

        if self._behavior == "empty":
            return OCRResult(
                request_id=request.request_id,
                status=OCRStatus.COMPLETED,
                extracted_text="",
            )

        if self._cancelled:
            return OCRResult(
                request_id=request.request_id,
                status=OCRStatus.CANCELLED,
                cancelled=True,
                error="OCR was cancelled",
            )

        text = f"Mock OCR text from {request.source_type.value}"
        if request.input_path:
            text = f"{text} ({Path(request.input_path).name})"
        return OCRResult(
            request_id=request.request_id,
            status=OCRStatus.COMPLETED,
            extracted_text=text,
            source_type=request.source_type,
            language=request.language,
            confidence=0.95,
            page=request.page,
            region=request.region,
        )

    def is_available(self) -> bool:
        return self._behavior != "unavailable"

    def cancel(self) -> None:
        self._cancelled = True

    def get_engine_type(self) -> str:
        return "mock"


class RealOCREngine(OCREngine):
    def __init__(self, runtime_command: Optional[str] = None):
        self._runtime_command = runtime_command or self._discover_runtime()

    def _discover_runtime(self) -> Optional[str]:
        return shutil.which("tesseract")

    def is_available(self) -> bool:
        return self._runtime_command is not None

    def get_engine_type(self) -> str:
        return "tesseract" if self._runtime_command else "none"

    def process(self, request: OCRRequest) -> OCRResult:
        if not self.is_available():
            return OCRResult(
                request_id=request.request_id,
                status=OCRStatus.UNAVAILABLE,
                error="OCR runtime is not installed or not in PATH",
            )

        if request.input_path is None:
            return OCRResult(
                request_id=request.request_id,
                status=OCRStatus.FAILED,
                error="Real OCR engine requires an input file path",
            )

        input_path = Path(request.input_path)
        if not input_path.exists() or not input_path.is_file():
            return OCRResult(
                request_id=request.request_id,
                status=OCRStatus.FAILED,
                error=f"Input file not found: {request.input_path}",
            )

        return OCRResult(
            request_id=request.request_id,
            status=OCRStatus.UNAVAILABLE,
            error="Real OCR execution is not implemented in Task 013",
        )
