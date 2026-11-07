from pathlib import Path

import pytest

from giscode.inference.api import InferenceAPI
from giscode.inference import backend_selection
from giscode.inference import runtime


def _fake_server(tmp_path: Path) -> Path:
    path = tmp_path / "llama-server.exe"
    path.write_bytes(b"test executable placeholder")
    return path


@pytest.fixture(autouse=True)
def _fresh_backend_choice(monkeypatch):
    """Every test resolves the backend from its own fake candidates.

    ``_KNOWN_LLAMA_SERVER_PATHS`` alone no longer isolates discovery: LM
    Studio's backends are resolved from the user profile per call, and this
    machine really has them. Hide the profiles so only the paths a test sets
    can be found.
    """
    monkeypatch.setattr(runtime, "_user_profile_roots", lambda: [])
    backend_selection.reset_probe_cache()
    runtime._LAST_BACKEND_CHOICE = None
    yield
    backend_selection.reset_probe_cache()
    runtime._LAST_BACKEND_CHOICE = None


def _probe_ok(monkeypatch):
    """Pretend every candidate answers ``--version`` (and ``--list-devices``).

    The placeholder files in this module are not real executables; selection
    now requires a successful probe, so the prober is stubbed. Precedence and
    fall-through assertions are unchanged.
    """
    ok = backend_selection.ProbeResult(True, version=(0, 6, 0), output="CUDA0: test (1 MiB, 1 MiB free)")
    monkeypatch.setattr(runtime, "_probe_version", lambda path: ok)
    monkeypatch.setattr(runtime, "_probe_devices", lambda path: ok)


def test_explicit_server_path_wins(monkeypatch, tmp_path):
    _probe_ok(monkeypatch)
    explicit = _fake_server(tmp_path)
    monkeypatch.setenv(runtime.LLAMA_SERVER_PATH_ENV, str(explicit))
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(runtime, "_KNOWN_LLAMA_SERVER_PATHS", [])

    assert runtime._find_llama_server_exe() == str(explicit.resolve())
    subprocess_runtime = runtime.SubprocessLlamaCppRuntime()
    assert subprocess_runtime.get_server_path() == str(explicit.resolve())


def test_absent_override_preserves_automatic_discovery(monkeypatch, tmp_path):
    _probe_ok(monkeypatch)
    discovered = _fake_server(tmp_path)
    monkeypatch.delenv(runtime.LLAMA_SERVER_PATH_ENV, raising=False)
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(runtime, "_KNOWN_LLAMA_SERVER_PATHS", [tmp_path])

    assert runtime._find_llama_server_exe() == str(discovered.resolve())


def test_invalid_override_does_not_select_invalid_path(monkeypatch, tmp_path):
    monkeypatch.setenv(runtime.LLAMA_SERVER_PATH_ENV, str(tmp_path / "missing.exe"))
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(runtime, "_KNOWN_LLAMA_SERVER_PATHS", [])

    assert runtime._find_llama_server_exe() is None
    assert runtime.SubprocessLlamaCppRuntime().is_available() is False


def test_inference_api_selects_subprocess_runtime_for_explicit_path(monkeypatch, tmp_path):
    _probe_ok(monkeypatch)
    explicit = _fake_server(tmp_path)
    monkeypatch.setenv(runtime.LLAMA_SERVER_PATH_ENV, str(explicit))
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(runtime, "_KNOWN_LLAMA_SERVER_PATHS", [])

    api = InferenceAPI(n_gpu_layers=35)

    assert isinstance(api._runtime, runtime.SubprocessLlamaCppRuntime)
    assert api._runtime.get_server_path() == str(explicit.resolve())
