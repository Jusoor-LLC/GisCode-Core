import base64
from pathlib import Path

import pytest

from giscode.inference.runtime import ChatMessage, ChatRequest
from giscode.models.types import ModelMetadata, ModelRegistry, ModelStatus
from giscode.inference.lifecycle import ModelInfo, ModelState
from giscode.vision import capabilities
from giscode.vision.engine import MockVisionEngine
from giscode.vision.service import VisionService
from giscode.vision.types import VisionRequest, VisionStatus


def _meta(name: str, path: str, tags=None, caps=None) -> ModelMetadata:
    return ModelMetadata(
        name=name,
        family="test",
        path=path,
        tags=tags or [],
        capabilities=caps or [],
        status=ModelStatus.REGISTERED,
    )


class FakeModelManager:
    def __init__(self, registry: ModelRegistry):
        self._registry = registry
        self._current = None
        self.loaded: list[str] = []
        self.unloaded: list[str] = []

    def get_registry(self) -> ModelRegistry:
        return self._registry

    def get_current_model(self):
        # Returns ModelInfo like the real implementation, not ModelMetadata
        if self._current is None:
            return None
        # Convert ModelMetadata to ModelInfo (like real implementation)
        return ModelInfo(
            name=self._current.name,
            path=self._current.path,
            size_bytes=self._current.size_bytes,
            context_length=self._current.context_length,
            state=ModelState.READY,
        )

    def load_model(self, name: str):
        meta = self._registry.get(name)
        self.loaded.append(name)
        self._current = meta
        return meta

    def unload_model(self, name: str):
        self.unloaded.append(name)
        if self._current is not None and self._current.name == name:
            self._current = None


@pytest.fixture
def image_file(tmp_path: Path) -> Path:
    p = tmp_path / "screenshot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    return p


def test_vision_unavailable_when_no_vision_model(image_file: Path):
    registry = ModelRegistry()
    registry.register(_meta("Qwen3-Coder-30B-A3B-Instruct", str(Path("n/a")), caps=["text", "chat", "coding"]))
    manager = FakeModelManager(registry)
    service = VisionService(model_manager=manager, engine=MockVisionEngine("unavailable"))

    result = service.analyze(VisionRequest(image_paths=[str(image_file)], prompt="Describe", request_id="r1"))

    assert result.status == VisionStatus.UNAVAILABLE
    assert "vision-capable" in (result.error or "")
    assert manager.loaded == []
    assert "text-only" in (result.error or "")


def test_current_vision_model_routes_in_place(image_file: Path, tmp_path: Path):
    registry = ModelRegistry()
    registry.register(_meta("Qwen3-VL-4B-Instruct", str(tmp_path / "qwen3vl.gguf"), caps=["text", "chat", "vision"]))
    manager = FakeModelManager(registry)
    manager._current = registry.get("Qwen3-VL-4B-Instruct")
    service = VisionService(model_manager=manager, engine=MockVisionEngine())

    result = service.analyze(VisionRequest(image_paths=[str(image_file)], prompt="Describe", request_id="r2"))

    assert result.status == VisionStatus.COMPLETE
    assert result.model_used == "mock-vision"
    assert not result.model_swapped
    assert manager.loaded == []


def test_on_demand_load_then_release_swaps_back(image_file: Path, tmp_path: Path):
    text_path = tmp_path / "coder.gguf"
    text_path.write_bytes(b"x")
    vision_path = tmp_path / "vl.gguf"
    vision_path.write_bytes(b"x")
    registry = ModelRegistry()
    registry.register(_meta("Qwen3-Coder-30B-A3B-Instruct", str(text_path), caps=["text", "chat", "coding"]))
    registry.register(_meta("Qwen3-VL-4B-Instruct", str(vision_path), caps=["text", "chat", "vision"]))
    manager = FakeModelManager(registry)
    manager._current = registry.get("Qwen3-Coder-30B-A3B-Instruct")
    service = VisionService(model_manager=manager, engine=MockVisionEngine())

    result = service.analyze(VisionRequest(image_paths=[str(image_file)], prompt="Describe", request_id="r3"))

    assert result.status == VisionStatus.COMPLETE
    assert result.model_swapped
    assert manager.loaded == ["Qwen3-VL-4B-Instruct", "Qwen3-Coder-30B-A3B-Instruct"]
    assert manager.unloaded == ["Qwen3-VL-4B-Instruct"]
    assert service.status == VisionStatus.IDLE
    assert manager._current.name == "Qwen3-Coder-30B-A3B-Instruct"


def test_missing_image_fails_honestly():
    registry = ModelRegistry()
    registry.register(_meta("Qwen3-VL-4B-Instruct", "n/a", caps=["text", "chat", "vision"]))
    manager = FakeModelManager(registry)
    service = VisionService(model_manager=manager, engine=MockVisionEngine())

    result = service.analyze(VisionRequest(image_paths=["C:/does/not/exist.png"], prompt="p", request_id="r4"))

    assert result.status == VisionStatus.FAILED
    assert "not found" in (result.error or "")
    assert manager.loaded == []


def test_capability_heuristics():
    vl = _meta("Qwen3-VL-4B-Instruct-Q4_K_M", "n/a")
    assert capabilities.is_vision_capable(vl)
    assert "vision" in capabilities.compute_capabilities(vl)

    coder = _meta("Qwen3-Coder-30B-A3B-Instruct", "n/a")
    assert not capabilities.is_vision_capable(coder)
    assert "vision" not in capabilities.compute_capabilities(coder)
    assert "coding" in capabilities.compute_capabilities(coder)


def test_mmproj_sibling_marks_vision(tmp_path: Path):
    proj = tmp_path / "mmproj-qwen.gguf"
    proj.write_bytes(b"x")
    chat = tmp_path / "qwen-vl-4b-q4.gguf"
    chat.write_bytes(b"x")
    meta = _meta("Qwen-VL-4B", str(chat))
    assert capabilities.is_vision_capable(meta)


def test_find_vision_model_excludes_mmproj(tmp_path: Path):
    proj = tmp_path / "mmproj-qwen.gguf"
    proj.write_bytes(b"x")
    chat = tmp_path / "qwen3-vl-4b-q4.gguf"
    chat.write_bytes(b"x")
    registry = ModelRegistry()
    registry.register(_meta("mmproj-Qwen3-VL-4B", str(proj), caps=["text"]))
    registry.register(_meta("Qwen3-VL-4B-Instruct", str(chat), caps=["text", "chat", "vision"]))
    found = capabilities.find_vision_model(registry)
    assert found is not None
    assert "mmproj" not in found.name.lower()


def test_chat_payload_encodes_images(tmp_path: Path):
    p = tmp_path / "shot.png"
    raw = b"\x89PNG\r\n\x1a\n" + b"1234"
    p.write_bytes(raw)
    request = ChatRequest(messages=[ChatMessage(role="user", content="hi")], images=[str(p)])
    payload = request.to_payload()
    assert "images" in payload
    expected = f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}"
    assert payload["images"] == [expected]


def test_chat_payload_no_images():
    request = ChatRequest(messages=[ChatMessage(role="user", content="hi")])
    assert "images" not in request.to_payload()


class _FakeInference:
    """Mock inference that returns a ModelInfo with a given name (simulating loaded model)."""
    def __init__(self, model_name: str | None = None):
        self._model = ModelInfo(
            name=model_name or "test-model",
            path="/fake/path/model.gguf",
            size_bytes=1024,
            state=ModelState.READY,
        ) if model_name else None

    def get_model_status(self):
        return self._model


def test_metadata_resolution_exact_name(image_file: Path, tmp_path: Path):
    """Case A: Exact registered model name resolves correctly."""
    registry = ModelRegistry()
    registry.register(_meta("Qwen3-VL-4B-Instruct", str(tmp_path / "qwen3vl.gguf"), caps=["text", "chat", "vision"]))
    manager = FakeModelManager(registry)
    inference = _FakeInference("Qwen3-VL-4B-Instruct")
    service = VisionService(model_manager=manager, inference=inference, engine=MockVisionEngine())

    # _current_metadata should resolve to ModelMetadata, not ModelInfo
    meta = service._current_metadata()
    assert meta is not None
    assert isinstance(meta, ModelMetadata)
    assert meta.name == "Qwen3-VL-4B-Instruct"
    assert "vision" in meta.capabilities

    # current_capabilities should work without AttributeError
    caps = service.current_capabilities()
    assert "vision" in caps

    # analyze should route to the already-loaded vision model
    result = service.analyze(VisionRequest(image_paths=[str(image_file)], prompt="Describe", request_id="r-exact"))
    assert result.status == VisionStatus.COMPLETE


def test_metadata_resolution_quantized_suffix(image_file: Path, tmp_path: Path):
    """Case B: Quantized/suffixed model name (e.g., -Q4_K_M) resolves to correct metadata."""
    registry = ModelRegistry()
    # Registered under canonical name (without quant suffix)
    registry.register(_meta("Qwen3-VL-4B-Instruct", str(tmp_path / "qwen3vl.gguf"), caps=["text", "chat", "vision"]))
    manager = FakeModelManager(registry)
    # Inference returns the stem WITH quant suffix (as it does in production)
    inference = _FakeInference("Qwen3-VL-4B-Instruct-Q4_K_M")
    service = VisionService(model_manager=manager, inference=inference, engine=MockVisionEngine())

    # _current_metadata should resolve via resolve_canonical_name to the registered metadata
    meta = service._current_metadata()
    assert meta is not None
    assert isinstance(meta, ModelMetadata)
    assert meta.name == "Qwen3-VL-4B-Instruct"
    assert "vision" in meta.capabilities

    # current_capabilities should work without AttributeError (this was the crash)
    caps = service.current_capabilities()
    assert "vision" in caps

    # _current_is_vision_capable should work
    assert service._current_is_vision_capable() is True

    # analyze should route to the already-loaded vision model
    result = service.analyze(VisionRequest(image_paths=[str(image_file)], prompt="Describe", request_id="r-quant"))
    assert result.status == VisionStatus.COMPLETE


def test_metadata_resolution_various_suffixes(image_file: Path, tmp_path: Path):
    """Case D: Various quantization suffixes all resolve correctly (no regression)."""
    registry = ModelRegistry()
    registry.register(_meta("MyModel-VL-7B", str(tmp_path / "model.gguf"), caps=["text", "chat", "vision"]))
    manager = FakeModelManager(registry)

    for suffix in ["-Q4_K_M", "-Q8_0", "-F16", "-Q5_K_S", "-IQ4_XS"]:
        inference = _FakeInference(f"MyModel-VL-7B{suffix}")
        service = VisionService(model_manager=manager, inference=inference, engine=MockVisionEngine())

        meta = service._current_metadata()
        assert meta is not None, f"Failed to resolve suffix: {suffix}"
        assert isinstance(meta, ModelMetadata)
        assert meta.name == "MyModel-VL-7B"
        assert "vision" in meta.capabilities

        caps = service.current_capabilities()
        assert "vision" in caps, f"Capabilities failed for suffix: {suffix}"


def test_metadata_resolution_no_match_returns_none():
    """Case D: Unrelated model name returns None (does not fall back to ModelInfo)."""
    registry = ModelRegistry()
    registry.register(_meta("Some-Other-Model", "n/a", caps=["text", "chat"]))
    manager = FakeModelManager(registry)
    inference = _FakeInference("Completely-Different-Model-Q4_K_M")
    service = VisionService(model_manager=manager, inference=inference, engine=MockVisionEngine())

    meta = service._current_metadata()
    assert meta is None, "Unrelated model name should not resolve to any metadata"


def test_fallback_to_model_manager_current_model(image_file: Path, tmp_path: Path):
    """When inference is None, falls back to model_manager.get_current_model() which returns ModelInfo."""
    registry = ModelRegistry()
    registry.register(_meta("Qwen3-VL-4B-Instruct", str(tmp_path / "qwen3vl.gguf"), caps=["text", "chat", "vision"]))
    manager = FakeModelManager(registry)
    # Set _current to ModelMetadata; get_current_model() will convert to ModelInfo
    manager._current = registry.get("Qwen3-VL-4B-Instruct")
    service = VisionService(model_manager=manager, inference=None, engine=MockVisionEngine())

    meta = service._current_metadata()
    assert meta is not None
    assert isinstance(meta, ModelMetadata)
    assert meta.name == "Qwen3-VL-4B-Instruct"
    assert "vision" in meta.capabilities

    caps = service.current_capabilities()
    assert "vision" in caps


def test_analyze_with_quantized_suffix_in_runtime_check(image_file: Path, tmp_path: Path):
    """The runtime check in analyze() also resolves quantized suffix correctly."""
    registry = ModelRegistry()
    registry.register(_meta("Qwen3-VL-4B-Instruct", str(tmp_path / "qwen3vl.gguf"), caps=["text", "chat", "vision"]))
    manager = FakeModelManager(registry)
    inference = _FakeInference("Qwen3-VL-4B-Instruct-Q4_K_M")
    service = VisionService(model_manager=manager, inference=inference, engine=MockVisionEngine())

    # This exercises the analyze() path at lines 77-78 (the second fix location)
    result = service.analyze(VisionRequest(image_paths=[str(image_file)], prompt="Describe", request_id="r-analyze"))
    assert result.status == VisionStatus.COMPLETE


def test_fallback_with_quantized_modelinfo_name(image_file: Path, tmp_path: Path):
    """Fallback path resolves quantized ModelInfo name to canonical ModelMetadata."""
    registry = ModelRegistry()
    registry.register(_meta("Qwen3-VL-4B-Instruct", str(tmp_path / "qwen3vl.gguf"), caps=["text", "chat", "vision"]))
    manager = FakeModelManager(registry)
    # Set _current to ModelMetadata with quantized name; get_current_model() returns ModelInfo with that name
    manager._current = _meta("Qwen3-VL-4B-Instruct-Q4_K_M", str(tmp_path / "qwen3vl.gguf"), caps=["text", "chat", "vision"])
    service = VisionService(model_manager=manager, inference=None, engine=MockVisionEngine())

    meta = service._current_metadata()
    assert meta is not None
    assert isinstance(meta, ModelMetadata)
    # Should resolve to canonical name without quant suffix
    assert meta.name == "Qwen3-VL-4B-Instruct"
    assert "vision" in meta.capabilities

    caps = service.current_capabilities()
    assert "vision" in caps
    assert service._current_is_vision_capable() is True

    # analyze() should work through fallback path
    result = service.analyze(VisionRequest(image_paths=[str(image_file)], prompt="Describe", request_id="r-fallback-quant"))
    assert result.status == VisionStatus.COMPLETE


def test_fallback_with_unknown_modelinfo_name_returns_none(image_file: Path, tmp_path: Path):
    """Fallback path returns None for unknown model names (no AttributeError)."""
    registry = ModelRegistry()
    registry.register(_meta("Some-Model", str(tmp_path / "model.gguf"), caps=["text", "chat"]))
    manager = FakeModelManager(registry)
    manager._current = _meta("Unknown-Model-Q4_K_M", str(tmp_path / "unknown.gguf"), caps=["text"])
    service = VisionService(model_manager=manager, inference=None, engine=MockVisionEngine())

    meta = service._current_metadata()
    assert meta is None, "Unknown model name should return None, not ModelInfo"

    caps = service.current_capabilities()
    assert caps == []

    assert service._current_is_vision_capable() is False

    # analyze() should fall through to on-demand vision model lookup
    # which will fail since no vision model registered
    result = service.analyze(VisionRequest(image_paths=[str(image_file)], prompt="Describe", request_id="r-unknown"))
    assert result.status == VisionStatus.UNAVAILABLE


def test_current_capabilities_never_raises_attribute_error(image_file: Path, tmp_path: Path):
    """current_capabilities() must never raise AttributeError even with ModelInfo fallback."""
    registry = ModelRegistry()
    registry.register(_meta("Test-VL-7B", str(tmp_path / "model.gguf"), caps=["text", "chat", "vision"]))
    manager = FakeModelManager(registry)
    manager._current = _meta("Test-VL-7B-Q4_K_M", str(tmp_path / "model.gguf"), caps=["text", "chat", "vision"])
    service = VisionService(model_manager=manager, inference=None, engine=MockVisionEngine())

    # This was the original crash: 'ModelInfo' object has no attribute 'capabilities'
    caps = service.current_capabilities()
    assert "vision" in caps

    is_vision = service._current_is_vision_capable()
    assert is_vision is True

    # analyze() should also work without AttributeError
    result = service.analyze(VisionRequest(image_paths=[str(image_file)], prompt="Describe", request_id="r-cap"))
    assert result.status == VisionStatus.COMPLETE


def test_fallback_with_various_quantized_suffixes(image_file: Path, tmp_path: Path):
    """Various quantization suffixes all resolve correctly through fallback path."""
    registry = ModelRegistry()
    registry.register(_meta("MyModel-VL-7B", str(tmp_path / "model.gguf"), caps=["text", "chat", "vision"]))
    manager = FakeModelManager(registry)

    for suffix in ["-Q4_K_M", "-Q8_0", "-F16", "-Q5_K_S", "-IQ4_XS"]:
        manager._current = _meta(f"MyModel-VL-7B{suffix}", str(tmp_path / "model.gguf"), caps=["text", "chat", "vision"])
        service = VisionService(model_manager=manager, inference=None, engine=MockVisionEngine())

        meta = service._current_metadata()
        assert meta is not None, f"Failed to resolve suffix: {suffix}"
        assert isinstance(meta, ModelMetadata)
        assert meta.name == "MyModel-VL-7B"
        assert "vision" in meta.capabilities

        caps = service.current_capabilities()
        assert "vision" in caps, f"Capabilities failed for suffix: {suffix}"

        assert service._current_is_vision_capable() is True

        result = service.analyze(VisionRequest(image_paths=[str(image_file)], prompt="Describe", request_id=f"r-{suffix}"))
        assert result.status == VisionStatus.COMPLETE, f"Analyze failed for suffix: {suffix}"