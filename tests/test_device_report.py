"""Where a model actually runs, on any platform.

Three claims must hold everywhere, not only on this PC:

1. A GPU claim needs the backend *and* the offloaded layer count. A model being
   installed, or a runtime being selected, proves nothing.
2. Unified memory is not separate graphics memory (Apple Silicon today).
3. An unknown backend is reported as unknown - never upgraded to GPU, never
   downgraded to a confident CPU claim.

The discovery-folder tests cover the one hard-coded Windows path that used to
sit in shared logic (``C:/Users/Public/lmstudio/models``).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from giscode.config import paths
from giscode.inference import api as api_module
from giscode.inference import device
from giscode.models import manager as manager_module
from giscode.models.manager import ModelManager


# ---------------------------------------------------------------------------
# 1. GPU only with evidence
# ---------------------------------------------------------------------------
def test_every_layer_on_the_gpu_is_reported_as_gpu():
    report = device.execution_report(backend_family="cuda12", gpu_layers=-1, n_ctx=8192)

    assert report.device == device.DEVICE_GPU
    assert report.verified is True
    assert report.warnings == ()
    assert "GPU" in report.label
    assert "gpu_layers=all" in report.describe()


def test_a_gpu_build_with_no_offloaded_layers_is_reported_as_cpu():
    report = device.execution_report(backend_family="cuda12", gpu_layers=0)

    assert report.device == device.DEVICE_CPU
    assert report.is_gpu is False
    assert report.verified is True
    assert any("entirely on the CPU" in warning for warning in report.warnings)


def test_some_layers_on_the_gpu_is_reported_as_mixed():
    report = device.execution_report(backend_family="vulkan", gpu_layers=12)

    assert report.device == device.DEVICE_MIXED
    assert "layers offloaded" in report.label


def test_an_unknown_backend_is_never_upgraded_to_gpu():
    report = device.execution_report(backend_family="", gpu_layers=-1)

    assert report.device == device.DEVICE_UNKNOWN
    assert report.verified is False
    assert report.is_gpu is False
    assert "not verified" in report.label


def test_a_cpu_build_is_reported_as_cpu_with_a_warning():
    report = device.execution_report(backend_family="cpu", gpu_layers=0)

    assert report.device == device.DEVICE_CPU
    assert report.verified is True
    assert any("uses the CPU" in warning for warning in report.warnings)


def test_a_cpu_build_does_not_pretend_an_offload_request_was_applied():
    """A layer count means nothing when the build has no graphics support."""
    report = device.execution_report(backend_family="cpu", gpu_layers=12)

    assert report.device == device.DEVICE_CPU
    assert report.is_gpu is False
    assert any("offload" in warning and "ignored" in warning for warning in report.warnings)
    assert "ignored" in report.evidence


def test_families_giscode_does_not_implement_are_still_classified_honestly():
    """The classifier is portable even though only CUDA/Vulkan/CPU exist today.

    A future ROCm or Metal adapter reporting its family must be read as a GPU
    without touching this module - and listing a family is not a claim that
    GisCode supports it.
    """
    for family in ("rocm", "metal", "sycl"):
        report = device.execution_report(backend_family=family, gpu_layers=-1)
        assert report.device == device.DEVICE_GPU, family


# ---------------------------------------------------------------------------
# 2. unified memory and platform names
# ---------------------------------------------------------------------------
def test_apple_silicon_reports_unified_memory():
    assert device.uses_unified_memory("darwin", "arm64") is True

    report = device.execution_report(
        backend_family="metal", gpu_layers=-1, platform_name="darwin", machine="arm64"
    )

    assert report.unified_memory is True
    assert "shared with system memory" in report.evidence


def test_an_intel_mac_and_a_windows_pc_keep_separate_graphics_memory():
    assert device.uses_unified_memory("darwin", "x86_64") is False
    assert device.uses_unified_memory("win32", "AMD64") is False
    assert device.uses_unified_memory("linux", "aarch64") is False


@pytest.mark.parametrize(
    "platform_name, expected",
    [("win32", "windows"), ("cygwin", "windows"), ("linux", "linux"),
     ("linux2", "linux"), ("darwin", "macos"), ("freebsd13", "other")],
)
def test_os_family_is_reported_portably(platform_name, expected):
    assert device.os_family(platform_name) == expected


def test_the_report_is_a_plain_dict_for_logs_and_reports():
    payload = device.execution_report(backend_family="cuda", gpu_layers=8, n_ctx=4096).as_dict()

    assert payload["device"] == device.DEVICE_MIXED
    assert payload["backend_family"] == "cuda"
    assert payload["verified"] is True
    assert payload["warnings"] == []


# ---------------------------------------------------------------------------
# 3. discovery folders carry no Windows-only path
# ---------------------------------------------------------------------------
def test_a_non_windows_platform_has_no_machine_wide_model_folder():
    assert paths.public_models_dir(os_name="posix") is None


def test_windows_resolves_the_machine_wide_model_folder_from_the_environment(monkeypatch):
    monkeypatch.setenv("PUBLIC", r"D:\Shared")

    resolved = paths.public_models_dir(os_name="nt")

    assert resolved == Path(r"D:\Shared") / "lmstudio" / "models"


def test_windows_falls_back_when_public_is_not_defined(monkeypatch):
    monkeypatch.delenv("PUBLIC", raising=False)

    assert str(paths.public_models_dir(os_name="nt")).endswith(
        os.path.join("lmstudio", "models")
    )


def test_discovery_dirs_match_the_platform(monkeypatch):
    monkeypatch.setattr(paths, "public_models_dir", lambda **_: None)

    assert api_module._default_model_dirs() == ["~/.giscode/models", "~/.lmstudio/models"]
    assert manager_module._default_model_search_dirs() == [
        "~/.giscode/models",
        "~/.lmstudio/models",
    ]


def test_discovery_dirs_include_the_public_folder_when_there_is_one(monkeypatch, tmp_path):
    public = tmp_path / "Public" / "lmstudio" / "models"
    monkeypatch.setattr(paths, "public_models_dir", lambda **_: public)

    assert str(public) in api_module._default_model_dirs()
    assert str(public) in manager_module._default_model_search_dirs()


def test_an_isolated_test_run_still_scans_only_the_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("GISCODE_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("GISCODE_TEST_MODE", "1")

    assert api_module._model_search_dirs() == [str(tmp_path / "profile" / "models")]


# ---------------------------------------------------------------------------
# 4. the manager records what the load actually did
# ---------------------------------------------------------------------------
class _Resources:
    available_vram_bytes = 12 * 1024 ** 3
    total_vram_bytes = 16 * 1024 ** 3
    gpu_name = "Test GPU"


class _Inference:
    def __init__(self, family):
        self._budget = self
        self._runtime = type("RT", (), {"get_backend_family": lambda _self: family})()

    def get_system_resources(self):
        return _Resources()


@pytest.mark.parametrize(
    "family, layers, expected",
    [("cuda12", -1, device.DEVICE_GPU), ("cuda12", 0, device.DEVICE_CPU),
     ("vulkan", 4, device.DEVICE_MIXED), ("", -1, device.DEVICE_UNKNOWN)],
)
def test_the_manager_reports_the_device_it_actually_used(family, layers, expected):
    manager = ModelManager.__new__(ModelManager)
    manager._inference = _Inference(family)

    report = manager._execution_report(layers, 4096)

    assert report.device == expected
    assert report.backend_family == family


def test_no_report_exists_before_a_model_is_loaded():
    manager = ModelManager.__new__(ModelManager)

    assert manager.execution_report is None
