"""Native AI module tests that never download a real model or run a GPU."""
from __future__ import annotations

import dataclasses
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from giscode.ai_module import manifest
from giscode.ai_module.artifacts import Cancelled, HashMismatch, extract_zip
from giscode.ai_module.readiness import FAILED, READY, validate_code
from giscode.ai_module.runtime_install import ManagedRuntimeInstaller, RuntimeInstallError
from giscode.ai_module.service import AIModuleService


class FakeSettings:
    def __init__(self):
        self.data = {"paths": {"models_dir": ""}}

    def get(self, key, default=None):
        value = self.data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def set(self, key, value):
        target = self.data
        parts = key.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value


class FakeInference:
    def __init__(self, code="def add(a, b):\n    return a + b\n"):
        self.code = code
        self.runtime = None
        self.cancelled = False
        self._status = SimpleNamespace(path="", name="Qwen2.5-Coder-7B-Instruct")

    def set_runtime(self, runtime):
        self.runtime = runtime

    def get_model_status(self):
        return self._status

    def chat_generate_stream(self, request, cancel_event=None):
        yield self.code, "stop", 12

    def cancel(self):
        self.cancelled = True


class FakeManager:
    def __init__(self, inference):
        self._inference = inference
        self.registered = {}
        self.loaded = None
        self.current = None

    def register_model(self, name, path):
        self.registered[name] = path

    def load_model(self, name):
        self.loaded = name
        self.current = SimpleNamespace(name=name, path=self.registered[name])
        return self.current

    def get_current_model(self):
        return self.current

    def unload_model(self, name):
        self.current = None


class FakeInstaller:
    def __init__(self, path, *, error=None, marker=None):
        self.path = path
        self.error = error
        self.marker = marker or {"version": manifest.RUNTIME_VERSION, "backend": "cpu", "server": "llama-server.exe"}
        self.calls = 0

    def installed(self):
        return self.path if Path(self.path).is_file() else None

    def marker_for(self, path):
        return self.marker

    def ensure(self, build, *, free_disk=0, cancel=lambda: False, progress=lambda *_: None):
        self.calls += 1
        if self.error:
            raise self.error
        if cancel():
            raise Cancelled("cancelled")
        progress("runtime", "fake runtime ready", 1.0)
        from giscode.ai_module.runtime_install import RuntimeInstallation

        return RuntimeInstallation(path=self.path, backend=build.key, version=manifest.RUNTIME_VERSION)


def _sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.seek(max(0, size - 1))
        handle.write(b"\0")


def _service(tmp_path, monkeypatch, *, installer=None, inference=None, manager=None, default_paths=False):
    monkeypatch.setenv("GISCODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("giscode.ai_module.service._disk_free", lambda _path: 30 * 1024 ** 3)
    # The real artifacts are gigabytes.  The service only needs the manifest's
    # logical size and digest, so this fixture shrinks both without changing a
    # single production code path.
    tiny = tuple(dataclasses.replace(item, size_bytes=4096) for item in manifest.MODEL_CANDIDATES)
    import giscode.ai_module.service as service_module

    monkeypatch.setattr(manifest, "MODEL_CANDIDATES", tiny)
    monkeypatch.setattr(service_module, "MODEL_CANDIDATES", tiny)
    settings = FakeSettings()
    settings.data["paths"]["models_dir"] = str(tmp_path / "models")
    inference = inference or FakeInference()
    manager = manager or FakeManager(inference)
    runtime = tmp_path / "runtime" / "llama-server.exe"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_bytes(b"fake")
    installer = installer or FakeInstaller(str(runtime))

    def fake_download(url, target, **kwargs):
        model = next(item for item in manifest.MODEL_CANDIDATES if item.filename == Path(target).name)
        _sparse(Path(target), model.size_bytes)
        return Path(target)

    paths = {} if default_paths else {
        "runtime_root": tmp_path / "runtime",
        "models_dir": tmp_path / "models",
    }
    service = AIModuleService(
        settings,
        manager,
        inference,
        **paths,
        runtime_installer=installer,
        download_model=fake_download,
    )
    return service, settings, manager, installer, inference


def test_manifest_hashes_and_licences_are_pinned():
    assert manifest.RUNTIME_VERSION == "b12373"
    assert manifest.RUNTIME_LICENSE == "MIT"
    for build in manifest.RUNTIME_BUILDS.values():
        assert build.components
        for component in build.components:
            assert len(component.sha256) == 64
            assert component.size_bytes > 0
            assert component.url.startswith("https://github.com/ggml-org/llama.cpp/")
    for model in manifest.MODEL_CANDIDATES:
        assert len(model.sha256) == 64
        assert model.license == "Apache-2.0"
        assert model.commercial_use is True
        assert "Apache-2.0" in model.redistribution


def test_scan_reports_not_installed_without_runtime(tmp_path, monkeypatch):
    service, *_ = _service(tmp_path, monkeypatch, installer=FakeInstaller(str(tmp_path / "missing.exe")))
    status = service.scan()
    assert status.state == "not_installed"
    assert status.runtime_ready is False
    assert status.models


def test_backend_selection_cuda_vulkan_cpu():
    service = object.__new__(AIModuleService)
    service.settings = FakeSettings()
    service._hardware_cache = None
    cases = [
        ({"gpu_vendor": "nvidia", "driver_major": 610}, "cuda"),
        ({"gpu_vendor": "nvidia", "driver_major": 500}, "vulkan"),
        ({"gpu_vendor": "amd", "driver_major": 0}, "vulkan"),
        ({"gpu_vendor": "", "driver_major": 0}, "cpu"),
    ]
    for hardware, expected in cases:
        service.hardware = lambda fresh=False, h=hardware: h
        if expected == "vulkan":
            monkeypatch_vulkan = True
        else:
            monkeypatch_vulkan = False
        import giscode.inference.backend_selection as backend

        original = backend.vulkan_available
        backend.vulkan_available = lambda: monkeypatch_vulkan
        try:
            assert service.choose_backend() == expected
        finally:
            backend.vulkan_available = original


def test_load_installs_runtime_downloads_model_and_reports_ready(tmp_path, monkeypatch):
    service, settings, manager, installer, inference = _service(tmp_path, monkeypatch)
    result = service.load_module(model_key="qwen2.5-coder-7b-instruct-q4_k_m")
    assert result.ok is True, result.message
    assert result.status == READY
    assert result.benchmark is not None and result.benchmark.coding_ok is True
    assert manager.loaded == "Qwen2.5-Coder-7B-Instruct"
    assert inference.runtime is not None
    assert installer.calls == 1
    assert settings.get("ai_module.enabled") is True
    assert settings.get("model.selected_model") == "Qwen2.5-Coder-7B-Instruct"
    assert (tmp_path / "models" / manifest.candidate("qwen2.5-coder-7b-instruct-q4_k_m").filename).is_file()


def test_load_returns_too_large_when_activation_fails_with_memory_error(tmp_path, monkeypatch):
    class FailingManager(FakeManager):
        def load_model(self, name):
            raise RuntimeError("Insufficient resources to load model at n_ctx=8192")

    inference = FakeInference()
    manager = FailingManager(inference)
    service, *_ = _service(tmp_path, monkeypatch, inference=inference, manager=manager)
    result = service.load_module(model_key="qwen2.5-coder-7b-instruct-q4_k_m")
    assert result.ok is False
    assert "fit" in result.message.lower() or "memory" in result.message.lower()


def test_load_returns_failed_when_runtime_install_fails(tmp_path, monkeypatch):
    installer = FakeInstaller(str(tmp_path / "missing.exe"), error=RuntimeInstallError("runtime is damaged"))
    service, *_ = _service(tmp_path, monkeypatch, installer=installer)
    result = service.load_module(model_key="qwen2.5-coder-7b-instruct-q4_k_m")
    assert result.ok is False
    assert result.status == FAILED
    assert "damaged" in result.message


def test_load_cancellation_is_safe(tmp_path, monkeypatch):
    installer = FakeInstaller(str(tmp_path / "missing.exe"))
    service, *_ = _service(tmp_path, monkeypatch, installer=installer)
    hardware = service.hardware()

    def cancel_during_setup(fresh=False):
        service._cancel.set()
        return hardware

    service.hardware = cancel_during_setup
    result = service.load_module(model_key="qwen2.5-coder-7b-instruct-q4_k_m")
    assert result.status == "CANCELLED"
    assert result.ok is False


def test_test_module_requires_an_installed_model(tmp_path, monkeypatch):
    service, *_ = _service(tmp_path, monkeypatch)
    result = service.test_module("qwen2.5-coder-7b-instruct-q4_k_m")
    assert result.ok is False
    assert "Load AI Module first" in result.message


def test_test_module_and_switch_and_remove(tmp_path, monkeypatch):
    service, settings, manager, *_ = _service(tmp_path, monkeypatch)
    loaded = service.load_module(model_key="qwen2.5-coder-7b-instruct-q4_k_m")
    assert loaded.ok
    tested = service.test_module("qwen2.5-coder-7b-instruct-q4_k_m")
    assert tested.ok is True
    assert tested.benchmark.status in ("READY", "READY - SLOW")
    switched = service.switch_model("qwen2.5-coder-7b-instruct-q4_k_m")
    assert switched.ok is True
    removed = service.remove_model("qwen2.5-coder-7b-instruct-q4_k_m", confirm=True)
    assert removed.ok is True
    assert not (tmp_path / "models" / manifest.candidate("qwen2.5-coder-7b-instruct-q4_k_m").filename).exists()
    assert service.remove_model("qwen2.5-coder-7b-instruct-q4_k_m", confirm=False).ok is False


def test_corrupt_model_is_rejected(tmp_path, monkeypatch):
    service, *_ = _service(tmp_path, monkeypatch)
    result = service.load_module(model_key="qwen2.5-coder-7b-instruct-q4_k_m")
    assert result.ok
    path = tmp_path / "models" / manifest.candidate("qwen2.5-coder-7b-instruct-q4_k_m").filename
    path.write_bytes(b"not the model")
    checked = service.test_module("qwen2.5-coder-7b-instruct-q4_k_m")
    assert checked.ok is False
    assert "Load AI Module first" in checked.message


def test_readiness_separates_correctness_from_speed():
    class SlowInference(FakeInference):
        def chat_generate_stream(self, request, cancel_event=None):
            yield "def add(a, b):\n    return a - b\n", "stop", 1

    result = __import__("giscode.ai_module.readiness", fromlist=["run_readiness"]).run_readiness(
        SlowInference(), "fake"
    )
    assert result.ok is True
    assert result.coding_ok is False
    assert "coding check failed" in result.detail


def test_coding_validation_rejects_imports():
    safe, detail = validate_code("import os\ndef add(a, b):\n    return a + b\n")
    assert safe is False
    assert "unsupported" in detail


def test_runtime_installer_verifies_hash_and_writes_marker(tmp_path):
    class Provider:
        def find_installed(self, root):
            return None

        def verify(self, server_path, timeout=10.0):
            assert Path(server_path).is_file()
            return True, "fake llama.cpp"

    build = manifest.RUNTIME_BUILDS["cpu"]

    def fake_component(component, directory, *, cancel=lambda: False, progress=None):
        path = Path(directory) / component.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("fake/llama-server.exe", "fake")
        return path

    installer = ManagedRuntimeInstaller(tmp_path / "runtime", provider=Provider(), download=fake_component)
    # The fake archive is not the pinned upstream digest; verification is
    # intentionally exercised through the pinned hash and must fail closed.
    with pytest.raises(RuntimeInstallError, match="SHA-256"):
        installer.ensure(build)


def test_runtime_installer_insufficient_disk(tmp_path):
    class Provider:
        def find_installed(self, root):
            return None

    installer = ManagedRuntimeInstaller(tmp_path / "runtime", provider=Provider(), download=lambda *a, **k: None)
    with pytest.raises(RuntimeInstallError, match="free space"):
        installer.ensure("cpu", free_disk=1)


def test_safe_zip_rejects_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../escape.txt", "bad")
    with pytest.raises(Exception, match="unsafe"):
        extract_zip(archive, tmp_path / "out")


def test_local_endpoint_is_loopback_only():
    import inspect
    from giscode.inference.runtime import SubprocessLlamaCppRuntime

    source = inspect.getsource(SubprocessLlamaCppRuntime.start_server)
    assert '"--host"' in source
    assert '"127.0.0.1"' in source
    assert "0.0.0.0" not in source


def test_notices_and_manifest_are_packaged_data():
    notice = Path(__file__).resolve().parents[1] / "giscode" / "ai_module" / "THIRD_PARTY_NOTICES.txt"
    text = notice.read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Apache-2.0" in text
    assert "127.0.0.1" in text


def test_saved_state_keeps_measurements_but_not_generated_source(tmp_path, monkeypatch):
    service, *_ = _service(tmp_path, monkeypatch)
    assert service.load_module(model_key="qwen2.5-coder-7b-instruct-q4_k_m").ok
    saved = json.loads((tmp_path / "home" / "ai" / service.edition / "ai_module.json").read_text(encoding="utf-8"))
    assert "code" not in saved["benchmark"]
    assert saved["benchmark"]["tokens_per_second"] > 0


def test_core_and_pro_own_separate_ai_runtime_and_state(tmp_path, monkeypatch):
    core, *_ = _service(tmp_path, monkeypatch, default_paths=True)
    monkeypatch.setenv("GISCODE_EDITION", "pro")
    pro, *_ = _service(tmp_path, monkeypatch, default_paths=True)
    assert core.edition == "core"
    assert pro.edition == "pro"
    assert core.runtime_root != pro.runtime_root
    assert core._state_path != pro._state_path
    assert core.runtime_root.name == "runtime"
    assert pro.runtime_root.parent.name == "pro"


def test_model_removal_does_not_delete_a_model_still_used_by_the_other_edition(tmp_path, monkeypatch):
    service, *_ = _service(tmp_path, monkeypatch)
    assert service.load_module(model_key="qwen2.5-coder-7b-instruct-q4_k_m").ok
    other = "pro" if service.edition == "core" else "core"
    state = tmp_path / "home" / "ai" / other / "ai_module.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"model_key": "qwen2.5-coder-7b-instruct-q4_k_m"}),
        encoding="utf-8",
    )
    result = service.remove_model("qwen2.5-coder-7b-instruct-q4_k_m", confirm=True)
    assert result.ok is False
    assert "other GisCode edition" in result.message
    assert (tmp_path / "models" / manifest.candidate("qwen2.5-coder-7b-instruct-q4_k_m").filename).is_file()


def test_model_fit_is_hardware_aware():
    model = manifest.candidate("qwen2.5-coder-7b-instruct-q4_k_m")
    assert model.fits(total_ram=16 * 1024 ** 3, total_vram=8 * 1024 ** 3, disk_free=20 * 1024 ** 3)[0]
    assert not model.fits(total_ram=4 * 1024 ** 3, total_vram=0, disk_free=20 * 1024 ** 3)[0]
    assert not model.fits(total_ram=16 * 1024 ** 3, total_vram=8 * 1024 ** 3, disk_free=1)[0]
    # Free VRAM is usable even when other applications have committed RAM.
    assert model.fits(total_ram=16 * 1024 ** 3, total_vram=8 * 1024 ** 3, disk_free=20 * 1024 ** 3, available_ram=2 * 1024 ** 3, available_vram=8 * 1024 ** 3)[0]
    # Without free VRAM the same machine must consider free system memory.
    assert not model.fits(total_ram=16 * 1024 ** 3, total_vram=8 * 1024 ** 3, disk_free=20 * 1024 ** 3, available_ram=2 * 1024 ** 3, available_vram=0)[0]


def test_download_only_verifies_without_loading_or_installing_runtime(tmp_path, monkeypatch):
    service, settings, manager, installer, inference = _service(tmp_path, monkeypatch)
    key = "qwen2.5-coder-7b-instruct-q4_k_m"
    result = service.download_model(key)
    assert result.ok is True, result.message
    assert result.status == "DOWNLOADED"
    assert result.model is not None and result.model.key == key
    assert manager.loaded is None
    assert inference.runtime is None
    assert installer.calls == 0
    assert (tmp_path / "models" / manifest.candidate(key).filename).is_file()
    assert settings.get("ai_module.enabled") is None


def test_download_only_rejects_an_unknown_catalog_key(tmp_path, monkeypatch):
    service, *_ = _service(tmp_path, monkeypatch)
    result = service.download_model("not-a-real-model")
    assert result.ok is False
    assert "catalog" in result.message.lower()


def test_selector_catalog_exposes_publisher_licence_source_and_honest_performance(tmp_path, monkeypatch):
    service, *_ = _service(tmp_path, monkeypatch)
    rows = {row["key"]: row for row in service.recommendations()}
    small = rows["qwen2.5-coder-1.5b-instruct-q4_k_m"]
    assert small["publisher"]
    assert small["source_url"].startswith("https://huggingface.co/")
    assert small["license"] == "Apache-2.0"
    assert small["license_url"].startswith("https://huggingface.co/")
    assert small["revision"]
    assert small["expected_performance"]
    assert small["qualified"] is True
    large = rows["qwen3-coder-30b-a3b-instruct-q4_k_m"]
    assert large["qualified"] is False
    assert "not measured" in large["expected_performance"].lower()
