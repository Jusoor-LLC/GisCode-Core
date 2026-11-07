import pytest

from giscode.inference.api import InferenceAPI
from giscode.inference.errors import (
    InferenceUnavailableError,
    InsufficientResourceBudgetError,
    LoadFailureError,
    ModelAlreadyLoadingError,
    ModelNotFoundError,
)
from giscode.inference.lifecycle import ModelState
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.inference.resource_budget import DefaultResourceBudget
from giscode.inference.runtime import FileSystemModelDiscovery, GenerationRequest


def test_inference_api_initializes_without_model():
    api = InferenceAPI()
    health = api.get_health()
    assert health["model_loaded"] is False
    assert health["model_state"] is None


def test_inference_api_reports_availability():
    api = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    assert api.available is True

    api = InferenceAPI(runtime=MockLlamaCppRuntime(available=False))
    assert api.available is False


def test_model_discovery():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        model_file = Path(tmp) / "test.gguf"
        model_file.write_text("fake model")
        discovery = FileSystemModelDiscovery([tmp])
        found = discovery.discover()
        assert str(model_file) in found


def test_load_model_success():
    runtime = MockLlamaCppRuntime(available=True, load_succeeds=True)
    api = InferenceAPI(runtime=runtime, discovery=FileSystemModelDiscovery([]))

    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f:
        f.write(b"fake model data")
        model_path = f.name

    try:
        model = api.load_model(model_path)
        assert model.state == ModelState.READY
        assert model.name == Path(model_path).stem
    finally:
        api.unload_model()
        Path(model_path).unlink()


def test_load_model_not_found():
    api = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    with pytest.raises(ModelNotFoundError):
        api.load_model("/nonexistent/model.gguf")


def test_load_model_already_loading():
    import tempfile
    from pathlib import Path

    runtime = MockLlamaCppRuntime(available=True)
    api = InferenceAPI(runtime=runtime, discovery=FileSystemModelDiscovery([]))

    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f:
        f.write(b"fake model data")
        model_path = f.name

    try:
        api.load_model(model_path)
        with pytest.raises(ModelAlreadyLoadingError):
            api.load_model(model_path)
    finally:
        api.unload_model()
        Path(model_path).unlink()


def test_load_model_insufficient_resources(monkeypatch):
    class FailingBudget(DefaultResourceBudget):
        def can_load(self, model_size_bytes: int, context_length: int) -> bool:
            return False

    runtime = MockLlamaCppRuntime(available=True)
    api = InferenceAPI(runtime=runtime, budget=FailingBudget(), discovery=FileSystemModelDiscovery([]))

    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f:
        f.write(b"fake model data")
        model_path = f.name

    try:
        with pytest.raises(InsufficientResourceBudgetError):
            api.load_model(model_path)
    finally:
        Path(model_path).unlink()


def test_load_model_runtime_unavailable():
    runtime = MockLlamaCppRuntime(available=False)
    api = InferenceAPI(runtime=runtime, discovery=FileSystemModelDiscovery([]))

    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f:
        f.write(b"fake model data")
        model_path = f.name

    try:
        with pytest.raises(InferenceUnavailableError):
            api.load_model(model_path)
    finally:
        Path(model_path).unlink()


def test_unload_model():
    import tempfile
    from pathlib import Path

    runtime = MockLlamaCppRuntime(available=True)
    api = InferenceAPI(runtime=runtime, discovery=FileSystemModelDiscovery([]))

    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f:
        f.write(b"fake model data")
        model_path = f.name

    try:
        api.load_model(model_path)
        api.unload_model()
        assert api.get_model_status() is None
    finally:
        Path(model_path).unlink()


def test_generate_requires_loaded_model():
    api = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    with pytest.raises(InferenceUnavailableError):
        api.generate(GenerationRequest(prompt="hello"))


def test_generate_with_loaded_model():
    import tempfile
    from pathlib import Path

    runtime = MockLlamaCppRuntime(available=True)
    api = InferenceAPI(runtime=runtime, discovery=FileSystemModelDiscovery([]))

    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f:
        f.write(b"fake model data")
        model_path = f.name

    try:
        api.load_model(model_path)
        response = api.generate(GenerationRequest(prompt="hello world"))
        assert response.text is not None
        assert len(response.text) > 0
        assert api.get_model_status().state == ModelState.READY
    finally:
        api.unload_model()
        Path(model_path).unlink()


def test_resource_budget_interface():
    budget = DefaultResourceBudget()
    resources = budget.get_system_resources()
    assert hasattr(resources, "total_ram_bytes")
    assert hasattr(resources, "available_ram_bytes")
    estimate = budget.estimate_model_memory(1_000_000_000, 2048)
    assert estimate > 1_000_000_000


def test_only_one_model_resident_at_a_time():
    import tempfile
    from pathlib import Path

    runtime = MockLlamaCppRuntime(available=True)
    api = InferenceAPI(runtime=runtime, discovery=FileSystemModelDiscovery([]))

    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f1:
        f1.write(b"fake model data 1")
        path1 = f1.name
    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f2:
        f2.write(b"fake model data 2")
        path2 = f2.name

    try:
        api.load_model(path1)
        assert api.get_model_status().state == ModelState.READY
        api.unload_model()
        api.load_model(path2)
        assert api.get_model_status().path == path2
    finally:
        api.unload_model()
        Path(path1).unlink()
        Path(path2).unlink()
