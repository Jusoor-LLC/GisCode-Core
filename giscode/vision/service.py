from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from giscode.models.types import ModelMetadata
from giscode.vision import capabilities
from giscode.vision.engine import LlamaCppVisionEngine, MockVisionEngine, VisionEngine
from giscode.vision.types import VisionRequest, VisionResult, VisionStatus


class VisionService:
    """On-demand vision capability.

    Normal chat never loads a vision model. When an image needs analysis:
      1. If the currently loaded model is vision-capable, route to it.
      2. Otherwise find a registered vision-capable model, load it on demand,
         run the analysis, then unload it and swap the previous model back.
      3. If no vision-capable model exists, report UNAVAILABLE honestly and
         never fabricate an analysis with a text-only model.
    """

    def __init__(
        self,
        model_manager,
        inference=None,
        engine: Optional[VisionEngine] = None,
        task_manager=None,
        keep_model_loaded: bool = False,
    ):
        self._model_manager = model_manager
        self._inference = inference
        self._engine = engine
        self._task_manager = task_manager
        self._keep_model_loaded = keep_model_loaded
        self._loaded_by_us: Optional[str] = None
        self._swapped_from: Optional[str] = None
        self.status = VisionStatus.IDLE
        self.last_error: Optional[str] = None

    def is_available(self) -> bool:
        if getattr(self._engine, "_behavior", None) == "unavailable":
            return False
        if self._current_is_vision_capable():
            return True
        return capabilities.find_vision_model(self._registry()) is not None

    def current_capabilities(self) -> list[str]:
        meta = self._current_metadata()
        if meta is None:
            return []
        return [c.lower() for c in (meta.capabilities or [])]

    def analyze(self, request: VisionRequest) -> VisionResult:
        self.status = VisionStatus.LOADING
        self.last_error = None
        started = time.time()
        try:
            for path in request.image_paths:
                self._validate_image(path)
        except Exception as exc:
            self.status = VisionStatus.FAILED
            self.last_error = str(exc)
            return VisionResult(
                status=VisionStatus.FAILED,
                request_id=request.request_id,
                error=str(exc),
                elapsed=time.time() - started,
            )

        # Check if the currently running llama-server has a vision model loaded
        if self._inference is not None:
            try:
                current_model = self._inference.get_model_status()
                if current_model and current_model.name:
                    current_meta = self._registry().resolve_canonical_name(current_model.name)
                    if current_meta and "vision" in [c.lower() for c in (current_meta.capabilities or [])]:
                        return self._run_engine(request, model_name=current_model.name)
            except Exception:
                pass

        if self._engine is not None and self._current_is_vision_capable():
            return self._run_engine(request, model_name=self._current_metadata().name)

        current = self._current_metadata()
        if current is not None and "vision" in [c.lower() for c in current.capabilities]:
            return self._run_engine(request, model_name=current.name)

        vision_meta = capabilities.find_vision_model(self._registry())
        if vision_meta is None:
            self.status = VisionStatus.UNAVAILABLE
            self.last_error = (
                "No vision-capable model is installed. GisCode found no model tagged "
                "for image analysis (VLM/vision GGUF, e.g. Qwen3-VL or a *.mmproj "
                "projector). The image was intentionally NOT processed by a text-only "
                "model to avoid fabricating a description."
            )
            return VisionResult(
                status=VisionStatus.UNAVAILABLE,
                request_id=request.request_id,
                error=self.last_error,
                elapsed=time.time() - started,
            )

        try:
            self._swapped_from = current.name if current is not None else None
            loaded = self._model_manager.load_model(vision_meta.name)
            self._loaded_by_us = loaded.name
        except Exception as exc:
            self.status = VisionStatus.FAILED
            self.last_error = str(exc)
            return VisionResult(
                status=VisionStatus.FAILED,
                request_id=request.request_id,
                error=f"Failed to load vision model '{vision_meta.name}': {exc}",
                elapsed=time.time() - started,
            )

        result = self._run_engine(request, model_name=loaded.name)
        result.model_swapped = self._swapped_from is not None
        if result.status == VisionStatus.FAILED:
            self.status = VisionStatus.FAILED
            self.last_error = result.error
        if not self._keep_model_loaded:
            self.release()
        result.elapsed = time.time() - started
        if result.status == VisionStatus.COMPLETE:
            self.status = VisionStatus.COMPLETE if self._keep_model_loaded else VisionStatus.IDLE
        return result

    def release(self) -> None:
        """Unload a vision model loaded by this service and swap the previous model back."""
        if self._loaded_by_us is None:
            self.status = VisionStatus.IDLE
            return
        self.status = VisionStatus.RELEASING
        try:
            self._model_manager.unload_model(self._loaded_by_us)
        except Exception:
            pass
        finally:
            self._loaded_by_us = None
        if self._swapped_from:
            try:
                previous = self._registry().get(self._swapped_from)
                if previous is not None and previous.path and Path(previous.path).exists():
                    self._model_manager.load_model(previous.name)
            except Exception:
                pass
            self._swapped_from = None
        self.status = VisionStatus.IDLE

    def _run_engine(self, request: VisionRequest, model_name: Optional[str] = None) -> VisionResult:
        self.status = VisionStatus.ANALYZING
        engine = self._engine
        if engine is None:
            inference = self._inference or self._model_manager._inference
            engine = LlamaCppVisionEngine(inference, model_name or "unknown")
        try:
            result = engine.analyze(request)
        except Exception as exc:
            self.status = VisionStatus.FAILED
            self.last_error = str(exc)
            return VisionResult(
                status=VisionStatus.FAILED,
                request_id=request.request_id,
                error=str(exc),
            )
        return result

    def _current_metadata(self) -> Optional[ModelMetadata]:
        # Try to get current model from inference API (real runtime)
        # Use the provided inference or fall back to model manager's inference
        inference = self._inference or getattr(self._model_manager, "_inference", None)
        if inference is not None:
            try:
                current_info = inference.get_model_status()
                if current_info is not None:
                    # Look up the full metadata from registry by name,
                    # using canonical resolution to handle quantized suffixes
                    return self._model_manager.get_registry().resolve_canonical_name(current_info.name)
            except Exception:
                pass
        # Fallback to model manager's current model (for tests and legacy)
        try:
            current_info = self._model_manager.get_current_model()
            if current_info is not None:
                # Resolve the model info's name through registry to get ModelMetadata
                # This handles quantized suffixes like -Q4_K_M
                return self._model_manager.get_registry().resolve_canonical_name(current_info.name)
        except Exception:
            return None

    def _current_is_vision_capable(self) -> bool:
        meta = self._current_metadata()
        return meta is not None and "vision" in [c.lower() for c in meta.capabilities]

    def _registry(self):
        return self._model_manager.get_registry()

    def _validate_image(self, path: str) -> None:
        p = Path(path)
        if not p.exists() or not p.is_file():
            raise ValueError(f"Image file not found: {path}")
        if p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif", ".pdf"}:
            raise ValueError(
                f"Unsupported image format '{p.suffix}'. Supported: "
                "PNG, JPG/JPEG, WEBP, BMP, GIF, TIFF, PDF"
            )