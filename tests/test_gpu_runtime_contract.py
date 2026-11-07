"""Runtime contract: discovery, chat capability and GPU-required mode.

These protect the three defects found in the Stage 3D audit:

1. LM Studio's backends and weights were resolved from ``Path.home()`` only, so
   a redirected ``USERPROFILE``/``HOME`` hid the CUDA build and every model.
2. ``InferenceAPI`` selected a runtime by ``hasattr(..., "chat_generate")``,
   which is always true because the base class defines the method and raises.
3. Nothing could demand GPU execution: a CPU build or a zero-layer load was
   silently accepted.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from giscode.config.paths import GISCODE_TEST_MODE_ENV
from giscode.inference import api as api_module
from giscode.inference import backend_selection, runtime
from giscode.inference.errors import InferenceUnavailableError
from giscode.inference.lifecycle import ModelInfo, ModelState


@pytest.fixture(autouse=True)
def _fresh_backend_state(monkeypatch):
    """No test may see this machine's real profile or a cached choice."""
    backend_selection.reset_probe_cache()
    runtime._LAST_BACKEND_CHOICE = None
    monkeypatch.delenv(runtime.LLAMA_SERVER_PATH_ENV, raising=False)
    monkeypatch.delenv(GISCODE_TEST_MODE_ENV, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(runtime, "_KNOWN_LLAMA_SERVER_PATHS", [])
    yield
    backend_selection.reset_probe_cache()
    runtime._LAST_BACKEND_CHOICE = None


def _server(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / "llama-server.exe"
    exe.write_bytes(b"placeholder")
    return exe


def _two_profiles(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """A fake profile in the environment plus the real one via %LOCALAPPDATA%."""
    fake = tmp_path / "fake_profile"
    real = tmp_path / "real_profile"
    (real / "AppData" / "Local").mkdir(parents=True, exist_ok=True)
    (real / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
    fake.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("USERPROFILE", str(fake))
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("HOMEDRIVE", fake.drive or "C:")
    monkeypatch.setenv("HOMEPATH", str(fake)[len(fake.drive):])
    monkeypatch.setenv("LOCALAPPDATA", str(real / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(real / "AppData" / "Roaming"))
    return fake, real


# ---------------------------------------------------------------------------
# 1. discovery must not depend on a redirectable home
# ---------------------------------------------------------------------------
def test_backend_discovery_finds_lmstudio_when_the_home_is_redirected(
    monkeypatch, tmp_path
):
    fake, real = _two_profiles(monkeypatch, tmp_path)
    exe = _server(
        real
        / ".lmstudio"
        / "extensions"
        / "backends"
        / "llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.56.0"
    )
    # The fake profile the environment points at has nothing installed.
    assert not (fake / ".lmstudio").exists()

    candidates = runtime._iter_backend_candidates()

    assert [c.path for c in candidates] == [str(exe)]
    assert candidates[0].source == "lmstudio"


def test_model_search_dirs_keep_lmstudio_weights_when_the_home_is_redirected(
    monkeypatch, tmp_path
):
    _fake, real = _two_profiles(monkeypatch, tmp_path)
    weights = real / ".lmstudio" / "models"
    weights.mkdir(parents=True)

    dirs = api_module._model_search_dirs()

    assert str(weights) in dirs


@pytest.mark.skipif(os.name != "nt", reason="asserts Windows profile search paths")
def test_model_search_dirs_are_unchanged_when_the_home_is_real(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    (profile / "AppData" / "Local").mkdir(parents=True)
    (profile / "AppData" / "Roaming").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv("HOMEDRIVE", profile.drive or "C:")
    monkeypatch.setenv("HOMEPATH", str(profile)[len(profile.drive):])
    monkeypatch.setenv("LOCALAPPDATA", str(profile / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(profile / "AppData" / "Roaming"))
    monkeypatch.setenv("GISCODE_HOME", str(profile / ".giscode"))

    dirs = [os.path.normcase(d) for d in api_module._model_search_dirs()]

    assert dirs == [
        os.path.normcase(str(profile / ".giscode" / "models")),
        os.path.normcase(str(profile / ".lmstudio" / "models")),
        os.path.normcase("C:/Users/Public/lmstudio/models"),
    ]


def test_test_mode_still_scans_only_the_profile_models_folder(monkeypatch, tmp_path):
    _two_profiles(monkeypatch, tmp_path)
    monkeypatch.setenv(GISCODE_TEST_MODE_ENV, "1")
    monkeypatch.setenv("GISCODE_HOME", str(tmp_path / "profile"))

    assert api_module._model_search_dirs() == [str(tmp_path / "profile" / "models")]


# ---------------------------------------------------------------------------
# 2. selection must require the chat interface, not hasattr()
# ---------------------------------------------------------------------------
def test_hasattr_is_not_evidence_of_a_chat_interface():
    assert hasattr(runtime.LlamaCppPythonRuntime, "chat_generate")  # inherited stub
    assert runtime.LlamaCppPythonRuntime.supports_chat_generate is False
    assert runtime.LlamaCppRuntime.supports_chat_generate is False
    assert runtime.SubprocessLlamaCppRuntime.supports_chat_generate is True


class _NonChatRuntime:
    """Available and running, but serves no /v1/chat/completions."""

    supports_chat_generate = False

    def is_available(self) -> bool:
        return True

    def is_model_alive(self) -> bool:
        return True

    def is_running(self, port: int = 8080) -> bool:  # pragma: no cover - fallback
        return True

    def get_model_status(self):
        return None

    def start_server(self, *args, **kwargs):  # pragma: no cover - never called
        raise AssertionError("a non-chat runtime must not be asked to load")

    def stop_server(self) -> None:  # pragma: no cover - never called
        pass


def test_chat_requests_are_refused_with_a_clear_reason_on_a_non_chat_runtime():
    api = api_module.InferenceAPI(runtime=_NonChatRuntime())
    api._model = ModelInfo(name="x", path="x", size_bytes=0, state=ModelState.READY)

    with pytest.raises(InferenceUnavailableError) as excinfo:
        api.chat_generate(api_module.ChatRequest(messages=[]))

    message = str(excinfo.value)
    assert "No chat-capable inference runtime" in message
    assert "/v1/chat/completions" in message


def test_a_runtime_without_chat_is_not_selected_and_says_why(monkeypatch):
    class _UnavailableServer:
        supports_chat_generate = True

        def __init__(self, *args, **kwargs):
            pass

        def is_available(self) -> bool:
            return False

        def get_server_path(self):
            return None

    class _AvailablePython:
        supports_chat_generate = False

        def __init__(self, *args, **kwargs):
            pass

        def is_available(self) -> bool:
            return True

    monkeypatch.setattr(api_module, "SubprocessLlamaCppRuntime", _UnavailableServer)
    monkeypatch.setattr(api_module, "LlamaCppPythonRuntime", _AvailablePython)

    api = api_module.InferenceAPI()

    assert isinstance(api._runtime, _UnavailableServer)
    assert "cannot serve /v1/chat/completions" in api.runtime_selection_note


# ---------------------------------------------------------------------------
# 3. GPU-required mode refuses CPU-only execution
# ---------------------------------------------------------------------------
def _ok_probe(monkeypatch):
    ok = backend_selection.ProbeResult(
        True, version=(0, 6, 0), output="CUDA0: test (1 MiB, 1 MiB free)"
    )
    monkeypatch.setattr(runtime, "_probe_version", lambda path: ok)
    monkeypatch.setattr(runtime, "_probe_devices", lambda path: ok)
    monkeypatch.setattr(runtime, "_user_profile_roots", lambda: [])
    monkeypatch.setattr(runtime, "_detect_hardware", lambda: (True, False))


def test_gpu_required_mode_refuses_the_cpu_build(monkeypatch, tmp_path):
    _ok_probe(monkeypatch)
    _server(tmp_path / "llama.cpp-win-x86_64-avx2-2.56.0")
    monkeypatch.setattr(runtime, "_KNOWN_LLAMA_SERVER_PATHS", [tmp_path])
    monkeypatch.setattr(runtime, "gpu_required", lambda: True)

    assert runtime._find_llama_server_exe() is None
    assert "GPU-required mode" in runtime.describe_backend_choice()


def test_automatic_mode_still_uses_the_cpu_build(monkeypatch, tmp_path):
    _ok_probe(monkeypatch)
    cpu = _server(tmp_path / "llama.cpp-win-x86_64-avx2-2.56.0")
    monkeypatch.setattr(runtime, "_KNOWN_LLAMA_SERVER_PATHS", [tmp_path])
    monkeypatch.setattr(runtime, "gpu_required", lambda: False)

    assert runtime._find_llama_server_exe() == str(cpu.resolve())


def test_gpu_required_mode_still_accepts_a_cuda_build(monkeypatch, tmp_path):
    _ok_probe(monkeypatch)
    cuda = _server(tmp_path / "llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.56.0")
    monkeypatch.setattr(runtime, "_KNOWN_LLAMA_SERVER_PATHS", [tmp_path])
    monkeypatch.setattr(runtime, "gpu_required", lambda: True)

    assert runtime._find_llama_server_exe() == str(cuda.resolve())


# --- the model-load side of GPU-required mode ------------------------------
class _Budget:
    def __init__(self, free_vram: int) -> None:
        self.free_vram = free_vram

    def get_system_resources(self):
        return type(
            "R", (), {"available_vram_bytes": self.free_vram, "total_vram_bytes": 16 * 1024 ** 3}
        )()


class _Inference:
    """Records every layer count requested; a GPU request fails like a real one."""

    def __init__(self, free_vram: int, family: str = "cuda12") -> None:
        self._budget = _Budget(free_vram)
        self._n_ctx = 4096
        self._runtime = type("RT", (), {"get_backend_family": lambda _s: family})()
        self.attempts: list[int] = []
        self.failures: list[str] = []

    def get_model_status(self):
        return None

    def _log_load_failure(self, name, message):
        self.failures.append(f"{name}: {message}")

    def load_model(self, path, mmproj_path=None, n_ctx=None, n_gpu_layers=0):
        from giscode.inference.errors import LoadFailureError

        self.attempts.append(n_gpu_layers)
        if int(n_gpu_layers) != 0:
            raise LoadFailureError("CUDA out of memory")
        return type("Info", (), {"size_bytes": 1})()


class _Settings:
    def __init__(self, require_gpu: bool) -> None:
        self.require_gpu = require_gpu

    def get(self, key, default=None):
        if key == "inference.require_gpu":
            return self.require_gpu
        if key == "inference.context_length":
            return 4096
        return default


class _Registry:
    def __init__(self, metadata) -> None:
        self._metadata = metadata

    def get(self, name):
        return self._metadata


def _manager(monkeypatch, tmp_path, *, require_gpu: bool, free_vram: int, family: str = "cuda12"):
    from giscode.models import manager as manager_module
    from giscode.models.types import ModelMetadata, ModelStatus

    model = tmp_path / "tiny.gguf"
    model.write_bytes(b"not a real gguf")
    metadata = ModelMetadata(
        name="tiny",
        family="qwen",
        path=str(model),
        size_bytes=model.stat().st_size,
        status=ModelStatus.REGISTERED,
    )
    inference = _Inference(free_vram, family)
    monkeypatch.setattr(
        manager_module,
        "select_context_size_for_path",
        lambda **kwargs: {
            "chosen": 4096,
            "reason": "fits-as-requested",
            "reason_text": "",
            "estimate_bytes": 8 * 1024 ** 3,
        },
    )
    instance = manager_module.ModelManager.__new__(manager_module.ModelManager)
    instance._settings = _Settings(require_gpu)
    instance._inference = inference
    instance._registry = _Registry(metadata)
    return instance, inference, metadata


def test_load_is_refused_when_the_model_would_run_on_the_cpu(monkeypatch, tmp_path):
    from giscode.inference.errors import InsufficientResourceBudgetError

    manager, inference, metadata = _manager(
        monkeypatch, tmp_path, require_gpu=True, free_vram=2 * 1024 ** 3
    )

    with pytest.raises(InsufficientResourceBudgetError) as excinfo:
        manager.load_model("tiny")

    assert "GPU-required mode" in str(excinfo.value)
    assert "Refusing to load it on the CPU" in str(excinfo.value)
    assert inference.attempts == []  # nothing was ever started
    assert metadata.status.value == "error"


def test_load_is_refused_when_only_a_cpu_build_exists(monkeypatch, tmp_path):
    manager, inference, _metadata = _manager(
        monkeypatch, tmp_path, require_gpu=True, free_vram=15 * 1024 ** 3, family="cpu"
    )

    with pytest.raises(InferenceUnavailableError) as excinfo:
        manager.load_model("tiny")

    assert "only working llama-server build" in str(excinfo.value)
    assert inference.attempts == []


def test_automatic_mode_still_steps_down_to_the_cpu(monkeypatch, tmp_path):
    manager, inference, metadata = _manager(
        monkeypatch, tmp_path, require_gpu=False, free_vram=2 * 1024 ** 3
    )

    manager.load_model("tiny")

    assert inference.attempts == [0]
    assert metadata.gpu_layers == 0


def test_gpu_required_mode_never_retries_with_zero_layers(monkeypatch, tmp_path):
    from giscode.inference.errors import LoadFailureError

    manager, inference, _metadata = _manager(
        monkeypatch, tmp_path, require_gpu=True, free_vram=15 * 1024 ** 3
    )

    with pytest.raises(LoadFailureError):
        manager.load_model("tiny")

    # One GPU attempt failed; no silent zero-layer (CPU) retry followed.
    assert inference.attempts == [-1]
