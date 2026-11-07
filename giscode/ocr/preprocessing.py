from pathlib import Path
from typing import Optional

from giscode.ocr.errors import OCRPreprocessingError
from giscode.ocr.types import OCRRequest


class PreprocessingStep:
    def apply(self, request: OCRRequest) -> OCRRequest:
        return request


class ResizeStep(PreprocessingStep):
    def __init__(self, max_width: int = 1024, max_height: int = 1024):
        self.max_width = max_width
        self.max_height = max_height

    def apply(self, request: OCRRequest) -> OCRRequest:
        return request


class GrayscaleStep(PreprocessingStep):
    def apply(self, request: OCRRequest) -> OCRRequest:
        return request


class ContrastNormalizationStep(PreprocessingStep):
    def apply(self, request: OCRRequest) -> OCRRequest:
        return request


class NoiseReductionStep(PreprocessingStep):
    def apply(self, request: OCRRequest) -> OCRRequest:
        return request


class RotationCorrectionStep(PreprocessingStep):
    def apply(self, request: OCRRequest) -> OCRRequest:
        return request


class CropStep(PreprocessingStep):
    def __init__(self, region: Optional[dict] = None):
        self.region = region

    def apply(self, request: OCRRequest) -> OCRRequest:
        if self.region is not None:
            request.region = self.region
        return request


class PreprocessingPipeline:
    def __init__(self, steps: Optional[list[PreprocessingStep]] = None):
        self._steps = steps or []

    def process(self, request: OCRRequest) -> OCRRequest:
        result = request
        for step in self._steps:
            result = step.apply(result)
        return result
