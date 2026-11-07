import logging
import os
import tempfile
from pathlib import Path

import pytest

from giscode.config.settings import SettingsManager
from giscode.inference.api import InferenceAPI
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.models.catalog import QWEN3_CODER_30B, QWEN3_CODER_480B, register_default_models
from giscode.models.manager import ModelManager
from giscode.models.types import ModelMetadata, ModelRegistry, ModelStatus


def test_model_manager_initializes_with_registry():
    settings = SettingsManager()
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)
    registry = manager.get_registry()
    assert registry is not None
    assert len(registry.list_all()) >= 2


def test_qwen3_coder_models_registered():
    settings = SettingsManager()
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)
    registered = manager.list_registered()
    names = [m.name for m in registered]
    assert "Qwen3-Coder-30B-A3B-Instruct" in names
    assert "Qwen3-Coder-480B-A35B-Instruct" in names


def test_model_discovery(tmp_path, monkeypatch):
    from giscode.models import manager as manager_module

    settings = SettingsManager(config_path=tmp_path / "config.json")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)

    model_file = tmp_path / "test-model.gguf"
    model_file.write_text("fake model")

    monkeypatch.setattr(manager_module, "_DEFAULT_MODEL_SEARCH_DIRS", [])
    settings.set("paths.models_dir", str(tmp_path))

    discovered = manager.discover_models()
    assert len(discovered) == 1
    assert str(model_file) in discovered


def test_mmproj_never_hijacks_chat_model_registry_slot(tmp_path, monkeypatch):
    from giscode.models import manager as manager_module

    settings = SettingsManager(config_path=tmp_path / "config.json")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)

    # A VLM folder whose mmproj stem contains the chat model's name must NOT
    # end up registered under the chat model name pointing at the mmproj file,
    # otherwise the chat model is un-loadable as the main GGUF.
    chat_path = tmp_path / "Qwen3-VL-4B-Instruct-GGUF" / "Qwen3-VL-4B-Instruct-Q4_K_M.gguf"
    proj_path = tmp_path / "Qwen3-VL-4B-Instruct-GGUF" / "mmproj-Qwen3-VL-4B-Instruct-F16.gguf"
    chat_path.parent.mkdir(parents=True)
    chat_path.write_text("chat")
    proj_path.write_text("projector")

    monkeypatch.setattr(manager_module, "_DEFAULT_MODEL_SEARCH_DIRS", [])
    settings.set("paths.models_dir", str(tmp_path / "Qwen3-VL-4B-Instruct-GGUF"))

    manager.auto_register_discovered()

    vl = manager.get_registry().get("Qwen3-VL-4B-Instruct")
    assert vl is not None
    assert "mmproj" not in Path(vl.path).name.lower(), (
        "the chat model registry slot must point at the chat GGUF, not the projector file"
    )
    assert (vl.path or "").endswith("Qwen3-VL-4B-Instruct-Q4_K_M.gguf")
    assert manager.validate_model_path(vl.path) is True
    resolved = manager._resolve_path_for_name("Qwen3-VL-4B-Instruct")
    assert resolved == chat_path.resolve()


def test_register_model_valid_path(tmp_path):
    settings = SettingsManager()
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)

    model_file = tmp_path / "my-model.gguf"
    model_file.write_text("fake model")

    metadata = manager.register_model("my-model", str(model_file))
    assert metadata.name == "my-model"
    assert metadata.path == str(model_file.resolve())
    assert metadata.status == ModelStatus.REGISTERED
    assert metadata.size_bytes == model_file.stat().st_size


def test_register_model_invalid_path():
    settings = SettingsManager()
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)

    with pytest.raises(Exception):
        manager.register_model("bad-model", "/nonexistent/model.gguf")


def test_validate_model_path(tmp_path):
    settings = SettingsManager()
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)

    model_file = tmp_path / "valid.gguf"
    model_file.write_text("fake model")
    assert manager.validate_model_path(str(model_file)) is True
    assert manager.validate_model_path(str(tmp_path / "invalid.txt")) is False
    assert manager.validate_model_path("/nonexistent/file.gguf") is False


def test_estimate_resource_requirements(tmp_path):
    settings = SettingsManager()
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)

    model_file = tmp_path / "big-model.gguf"
    model_file.write_bytes(b"x" * 1000)
    result = manager.estimate_resource_requirements(str(model_file))
    assert "model_size_bytes" in result
    assert result["model_size_bytes"] == 1000
    assert "estimated_memory_bytes" in result
    assert "can_load" in result


def test_check_budget():
    settings = SettingsManager()
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)

    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f:
        f.write(b"x" * 100)
        path = f.name

    try:
        assert manager.check_budget(path) is True
    finally:
        Path(path).unlink()


def test_one_model_at_a_time_enforcement(tmp_path):
    settings = SettingsManager()
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)

    model1 = tmp_path / "model1.gguf"
    model1.write_text("model 1")
    model2 = tmp_path / "model2.gguf"
    model2.write_text("model 2")

    manager.register_model("model1", str(model1))
    manager.register_model("model2", str(model2))

    manager.load_model("model1")
    assert manager.get_current_model() is not None
    assert manager.get_current_model().name == "model1"

    manager.unload_model("model1")
    assert manager.get_current_model() is None

    manager.load_model("model2")
    assert manager.get_current_model() is not None
    assert manager.get_current_model().name == "model2"

    manager.unload_model("model2")


def test_unload_before_load(tmp_path):
    settings = SettingsManager()
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)

    model_file = tmp_path / "test-model.gguf"
    model_file.write_bytes(b"fake model")

    manager.register_model("test-model", str(model_file))
    result1 = manager.load_model("test-model")
    # Loading the same model again should return the existing model (no exception)
    result2 = manager.load_model("test-model")
    assert result1 is result2  # Same object returned
    assert result1.status.name == "READY"
    manager.unload_model("test-model")


def test_model_status_reporting(tmp_path):
    settings = SettingsManager()
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)

    model_file = tmp_path / "status-test.gguf"
    model_file.write_text("fake model")
    manager.register_model("status-test", str(model_file))

    metadata = manager.get_model_status("status-test")
    assert metadata is not None
    assert metadata.status == ModelStatus.REGISTERED

    manager.load_model("status-test")
    metadata = manager.get_model_status("status-test")
    assert metadata.status == ModelStatus.READY

    manager.unload_model("status-test")
    metadata = manager.get_model_status("status-test")
    assert metadata.status == ModelStatus.UNKNOWN


def test_configuration_support(tmp_path):
    settings = SettingsManager(config_path=tmp_path / "config.json")
    settings.set("model.selected_model", "Qwen3-Coder-30B-A3B-Instruct")
    settings.set("model.context_size", 8192)
    settings.set("model.gpu_layers", 20)
    settings.set("model.generation_defaults.max_tokens", 1024)

    assert settings.get("model.selected_model") == "Qwen3-Coder-30B-A3B-Instruct"
    assert settings.get("model.context_size") == 8192
    assert settings.get("model.gpu_layers") == 20
    assert settings.get("model.generation_defaults.max_tokens") == 1024


def test_qwen3_coder_registration():
    registry = ModelRegistry()
    register_default_models(registry)

    model30b = registry.get("Qwen3-Coder-30B-A3B-Instruct")
    assert model30b is not None
    assert model30b.family == "Qwen3-Coder"
    assert model30b.context_length == 32768

    model480b = registry.get("Qwen3-Coder-480B-A35B-Instruct")
    assert model480b is not None
    assert model480b.family == "Qwen3-Coder"
    assert model480b.context_length == 32768


def test_autoload_without_selected_model_leaves_unloaded(
    tmp_path, monkeypatch, model_autoload_enabled
):
    from giscode import main as main_module
    from giscode.models import manager as manager_module

    logger = logging.getLogger("test.autoload.unset")
    settings = SettingsManager(config_path=tmp_path / "config.json")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)

    model_file = tmp_path / "Qwen3-VL-4B-Instruct-GGUF" / "Qwen3-VL-4B-Instruct-Q4_K_M.gguf"
    project_file = tmp_path / "Qwen3-VL-4B-Instruct-GGUF" / "mmproj-Qwen3-VL-4B-Instruct-F16.gguf"
    model_file.parent.mkdir(parents=True)
    model_file.write_text("chat")
    project_file.write_text("projector")
    monkeypatch.setattr(manager_module, "_DEFAULT_MODEL_SEARCH_DIRS", [])
    settings.set("paths.models_dir", str(tmp_path / "Qwen3-VL-4B-Instruct-GGUF"))

    main_module._try_autoload_model(logger, settings, manager)

    assert manager.get_current_model() is None, (
        "startup must not silently load a model when none was selected"
    )
    assert settings.get("model.selected_model", "") == "" or settings.get(
        "model.selected_model"
    ) is None, "startup must not write a model selection into the config"


def test_autoload_with_selected_model_loads_it(
    tmp_path, monkeypatch, model_autoload_enabled
):
    from giscode import main as main_module
    from giscode.models import manager as manager_module

    logger = logging.getLogger("test.autoload.selected")
    settings = SettingsManager(config_path=tmp_path / "config.json")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)

    model_file = tmp_path / "mymodel.gguf"
    model_file.write_text("fake model")
    monkeypatch.setattr(manager_module, "_DEFAULT_MODEL_SEARCH_DIRS", [])
    settings.set("paths.models_dir", str(tmp_path))
    settings.set("model.selected_model", "mymodel")
    manager.auto_register_discovered()

    main_module._try_autoload_model(logger, settings, manager)

    current = manager.get_current_model()
    assert current is not None
    assert current.name == "mymodel"
