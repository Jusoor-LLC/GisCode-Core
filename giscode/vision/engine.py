from __future__ import annotations

import time
import base64
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from giscode.inference.runtime import ChatMessage, ChatRequest
from giscode.vision.types import VisionRequest, VisionResult, VisionStatus

_SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def _image_as_data_uri(path: str) -> Optional[str]:
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return None
        suffix = p.suffix.lower().lstrip(".")
        mime = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
            "bmp": "image/bmp",
            "gif": "image/gif",
        }.get(suffix)
        if mime is None:
            return None
        with open(p, "rb") as fh:
            raw = fh.read()
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except Exception:
        return None


class VisionEngine(ABC):
    @abstractmethod
    def is_available(self) -> bool:
        pass

    @abstractmethod
    def analyze(self, request: VisionRequest) -> VisionResult:
        pass

    def get_engine_type(self) -> str:
        return "abstract"


class LlamaCppVisionEngine(VisionEngine):
    """Real vision inference through the active llama.cpp model.

    The service-layer guarantee is that the currently loaded model is
    vision-capable before this engine runs. Inline image data is attached
    as base64 data URIs in the chat payload (llama-server multimodal).
    """

    def __init__(self, api, model_name: str):
        self._api = api
        self._model_name = model_name

    def is_available(self) -> bool:
        try:
            return bool(self._api.get_health())
        except Exception:
            return False

    def get_engine_type(self) -> str:
        return "llama-cpp"

    def analyze(self, request: VisionRequest) -> VisionResult:
        started = time.time()
        for path in request.image_paths:
            self._validate_path(path)
        
        # Build multimodal content array with data URIs
        content = [{"type": "text", "text": request.prompt}]
        for path in request.image_paths:
            data_uri = _image_as_data_uri(path)
            if data_uri:
                content.append({"type": "image_url", "image_url": {"url": data_uri}})
        
        try:
            response = self._api.chat_generate(
                ChatRequest(
                    messages=[
                        ChatMessage(role="user", content=content),
                    ],
                    max_tokens=request.max_tokens,
                    temperature=0.2,
                    request_id=request.request_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - surface any engine failure honestly
            return VisionResult(
                status=VisionStatus.FAILED,
                request_id=request.request_id,
                model_used=self._model_name,
                error=str(exc),
                elapsed=time.time() - started,
            )
        text = (response.text or "").strip()
        return VisionResult(
            status=VisionStatus.COMPLETE if text else VisionStatus.FAILED,
            request_id=request.request_id,
            text=text,
            model_used=self._model_name,
            error=None if text else "Vision model returned an empty response",
            elapsed=time.time() - started,
        )

    def _validate_path(self, path: str) -> None:
        p = Path(path)
        if not p.exists() or not p.is_file():
            raise ValueError(f"Image file not found: {path}")
        if p.suffix.lower() not in _SUPPORTED_SUFFIXES:
            raise ValueError(
                f"Unsupported image format '{p.suffix}': supported are "
                f"{', '.join(sorted(_SUPPORTED_SUFFIXES))}"
            )


class MockVisionEngine(VisionEngine):
    """Deterministic engine for tests and when no vision-capable model exists."""

    def __init__(self, behavior: str = "success", delay: float = 0.0, model_name: str = "mock-vision"):
        self._behavior = behavior
        self._delay = delay
        self._model_name = model_name

    def is_available(self) -> bool:
        return self._behavior != "unavailable"

    def get_engine_type(self) -> str:
        return "mock"

    def analyze(self, request: VisionRequest) -> VisionResult:
        if self._delay:
            time.sleep(self._delay)
        if self._behavior == "unavailable":
            return VisionResult(
                status=VisionStatus.UNAVAILABLE,
                request_id=request.request_id,
                error="No vision-capable model is available",
            )
        if self._behavior == "failure":
            return VisionResult(
                status=VisionStatus.FAILED,
                request_id=request.request_id,
                error="Mock vision engine failed",
            )
        names = ", ".join(Path(p).name for p in request.image_paths)
        text = f"[Vision mock] Analyzed {names}: screenshot shows a login screen with an email input."
        return VisionResult(
            status=VisionStatus.COMPLETE,
            request_id=request.request_id,
            text=text,
            model_used=self._model_name,
        )

    def get_engine_type(self) -> str:
        return "mock"