"""Model-state truth: liveness is authoritative, failed loads are never green,
and the Load path recovers after a failure."""

import tempfile
from pathlib import Path

import pytest

from giscode.inference.api import InferenceAPI
from giscode.inference.errors import LoadFailureError
from giscode.inference.lifecycle import ModelState
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.inference.runtime import FileSystemModelDiscovery


def _make_temp_model() -> str:
    f = tempfile.NamedTemporaryFile(suffix=".gguf", delete=False)
    f.write(b"fake model data")
    f.close()
    return f.name


def test_healthy_loaded_model_reports_ready():
    path = _make_temp_model()
    try:
        api = InferenceAPI(runtime=MockLlamaCppRuntime(available=True), discovery=FileSystemModelDiscovery([]))
        api.load_model(path)
        assert api.get_model_status().state == ModelState.READY
        assert api.get_health()["model_loaded"] is True
        assert api.get_health()["model_state"] == "ready"
    finally:
        api.unload_model()
        Path(path).unlink()


def test_dead_runtime_not_reported_ready():
    path = _make_temp_model()
    try:
        runtime = MockLlamaCppRuntime(available=True)
        api = InferenceAPI(runtime=runtime, discovery=FileSystemModelDiscovery([]))
        api.load_model(path)
        assert api.get_model_status() is not None

        # Simulate llama-server dying while the API still believes a model is loaded.
        runtime.stop_server()

        assert api.get_model_status() is None
        assert api.get_health()["model_loaded"] is False
        assert api.get_health()["model_state"] is None
    finally:
        api.unload_model()
        Path(path).unlink()


def test_failed_load_is_recoverable_not_green():
    path = _make_temp_model()
    try:
        runtime = MockLlamaCppRuntime(available=True, load_succeeds=False)
        api = InferenceAPI(runtime=runtime, discovery=FileSystemModelDiscovery([]))

        with pytest.raises(LoadFailureError):
            api.load_model(path)

        # Failed load must not look loaded.
        assert api.get_model_status() is None
        assert api.get_health()["model_loaded"] is False

        # Load button remains usable: a second attempt after the failure recovers.
        runtime._load_succeeds = True
        model = api.load_model(path)
        assert model.state == ModelState.READY
        assert api.get_model_status().state == ModelState.READY
    finally:
        api.unload_model()
        Path(path).unlink()


def test_successful_load_unload_roundtrip():
    path = _make_temp_model()
    try:
        api = InferenceAPI(runtime=MockLlamaCppRuntime(available=True), discovery=FileSystemModelDiscovery([]))
        model = api.load_model(path)
        assert model.state == ModelState.READY
        api.unload_model()
        assert api.get_model_status() is None
        assert api.get_health()["model_loaded"] is False
    finally:
        Path(path).unlink()


def test_failed_runtime_load_records_the_reason_on_the_metadata(tmp_path):
    """A load that fails at runtime must leave the reason on the metadata.

    The budget-refusal branch records its own message (see
    test_resource_budget); this covers the other branch: the model passed
    the budget but the runtime could not load it, and the user has to see why
    on the model's status.
    """
    from giscode.config.settings import SettingsManager
    from giscode.inference.api import InferenceAPI
    from giscode.models.manager import ModelManager
    from giscode.models.types import ModelStatus

    settings = SettingsManager(config_path=tmp_path / "config.json")
    inference = InferenceAPI(
        runtime=MockLlamaCppRuntime(available=True, load_succeeds=False),
        discovery=FileSystemModelDiscovery([]),
    )
    manager = ModelManager(settings, inference)

    model_file = tmp_path / "broken.gguf"
    model_file.write_text("fake model", encoding="utf-8")
    manager.register_model("broken", str(model_file))

    with pytest.raises(LoadFailureError):
        manager.load_model("broken")

    metadata = manager.get_model_status("broken")
    assert metadata is not None
    assert metadata.status == ModelStatus.ERROR
    assert metadata.error_message
    assert "LoadFailureError" in metadata.error_message