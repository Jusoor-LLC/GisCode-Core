"""Regression tests for honest llama-server build selection.

Everything here runs on fake folder trees and injected probes: no real
llama-server binary is executed, no model is loaded and no process is
signalled.

Covers:
* every discovered candidate must actually run (``--version`` exit 0);
* the winner is the newest working build in the best family this PC can use;
* CUDA/Vulkan winners are confirmed with ``--list-devices``;
* the DLL folder the previous code never found is now on ``PATH``;
* the decision is explained in one sentence, and a CPU-only load explains
  itself in plain words without touching any process.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from giscode.inference import backend_selection as bs
from giscode.inference import runtime

CUDA_OLD = "llama.cpp-win-x86_64-nvidia-cuda-avx2-2.30.0"
CUDA_NEW = "llama.cpp-win-x86_64-nvidia-cuda-avx2-2.56.0"
CUDA12 = "llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.56.0"
VULKAN = "llama.cpp-win-x86_64-vulkan-avx2-2.64.0"
CPU = "llama.cpp-win-x86_64-avx2-2.56.0"

NVIDIA_DEVICE = "CUDA0: NVIDIA GeForce RTX 5060 Ti (16310 MiB, 4143 MiB free)"
VULKAN_DEVICES = (
    "Vulkan0: NVIDIA GeForce RTX 5060 Ti (16050 MiB, 4311 MiB free)\n"
    "Vulkan1: AMD Radeon(TM) Graphics (16186 MiB, 15376 MiB free)"
)


@pytest.fixture(autouse=True)
def _clean_state():
    bs.reset_probe_cache()
    runtime._LAST_BACKEND_CHOICE = None
    yield
    bs.reset_probe_cache()
    runtime._LAST_BACKEND_CHOICE = None


def make_tree(tmp_path: Path, *names: str) -> dict[str, str]:
    """Create fake backend folders, one empty ``llama-server.exe`` each."""
    paths: dict[str, str] = {}
    for name in names:
        folder = tmp_path / name
        folder.mkdir(parents=True, exist_ok=True)
        exe = folder / "llama-server.exe"
        exe.write_bytes(b"")
        paths[name] = str(exe.resolve())
    return paths


def key(path: str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


class FakeProbes:
    """Injected probe pair: records calls, answers from a table."""

    def __init__(self, versions: dict[str, str], failures: dict[str, str] | None = None,
                 devices: dict[str, str] | None = None, noisy: dict[str, str] | None = None):
        self.versions = {key(k): v for k, v in versions.items()}
        self.failures = {key(k): v for k, v in (failures or {}).items()}
        self.devices = {key(k): v for k, v in (devices or {}).items()}
        self.noisy = {key(k): v for k, v in (noisy or {}).items()}
        self.version_calls: list[str] = []
        self.device_calls: list[str] = []

    def version(self, path: str) -> bs.ProbeResult:
        self.version_calls.append(key(path))
        if key(path) in self.failures:
            return bs.ProbeResult(False, reason=self.failures[key(path)])
        text = self.versions.get(key(path), "")
        return bs.ProbeResult(True, version=bs.parse_version(text), output=text)

    def device(self, path: str) -> bs.ProbeResult:
        self.device_calls.append(key(path))
        if key(path) in self.noisy:
            return bs.ProbeResult(False, reason=self.noisy[key(path)])
        return bs.ProbeResult(True, output=self.devices.get(key(path), ""))


def candidates(paths: list[str], pinned: bool = False, source: str = "test") -> list[bs.Candidate]:
    return [bs.Candidate(path, source, pinned) for path in paths]


# --------------------------------------------------------------------------
# version parsing and ordering
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("version: 0.6.0-dev (build 1, commit 8bb3cf0)", (0, 6, 0)),
        ("version: 0.1.2-dev (build 1, commit 947fd9b)", (0, 1, 2)),
        ("version: 1.2", (1, 2)),
        ("no version here", ()),
        ("", ()),
    ],
)
def test_parse_version(text, expected):
    assert bs.parse_version(text) == expected


def test_family_detection_is_case_insensitive():
    assert bs.family_of(r"C:\x\llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.56.0\llama-server.exe") == "cuda12"
    assert bs.family_of(r"C:\x\llama.cpp-win-x86_64-nvidia-cuda-avx2-2.30.0\llama-server.exe") == "cuda"
    assert bs.family_of(r"C:\x\llama.cpp-win-x86_64-vulkan-avx2-2.64.0\llama-server.exe") == "vulkan"
    assert bs.family_of(r"C:\x\llama.cpp-win-x86_64-avx2-2.56.0\llama-server.exe") == "cpu"
    assert bs.family_of(None) == "unknown"


def test_newest_cuda12_wins_over_newer_vulkan(tmp_path):
    paths = make_tree(tmp_path, CUDA_OLD, CUDA_NEW, CUDA12, VULKAN, CPU)
    probes = FakeProbes(
        {
            paths[CUDA_OLD]: "version: 0.1.2-dev",
            paths[CUDA_NEW]: "version: 0.6.0-dev",
            paths[CUDA12]: "version: 0.6.0-dev",
            paths[VULKAN]: "version: 0.7.0-dev",
            paths[CPU]: "version: 0.6.0-dev",
        },
        devices={paths[CUDA12]: NVIDIA_DEVICE},
    )
    choice = bs.select_backend(
        candidates(list(paths.values())),
        probe=probes.version,
        has_nvidia=True,
        has_vulkan=True,
        confirm_devices=True,
        device_probe=probes.device,
    )
    assert choice.path == paths[CUDA12]
    assert choice.family == "cuda12"
    assert choice.version == (0, 6, 0)
    assert choice.devices == (NVIDIA_DEVICE,)


def test_newest_version_wins_within_a_family(tmp_path):
    paths = make_tree(tmp_path, CUDA_OLD, CUDA_NEW)
    probes = FakeProbes(
        {paths[CUDA_OLD]: "version: 0.1.2-dev", paths[CUDA_NEW]: "version: 0.6.0-dev"},
        devices={paths[CUDA_NEW]: NVIDIA_DEVICE, paths[CUDA_OLD]: NVIDIA_DEVICE},
    )
    choice = bs.select_backend(
        candidates([paths[CUDA_OLD], paths[CUDA_NEW]]),
        probe=probes.version,
        has_nvidia=True,
        has_vulkan=False,
        confirm_devices=True,
        device_probe=probes.device,
    )
    assert choice.path == paths[CUDA_NEW]


def test_broken_cuda_build_is_rejected_and_next_one_wins(tmp_path):
    paths = make_tree(tmp_path, CUDA_OLD, CUDA_NEW)
    probes = FakeProbes(
        {paths[CUDA_OLD]: "version: 0.1.2-dev"},
        failures={paths[CUDA_NEW]: "exit 127: llama-server-impl.dll"},
        devices={paths[CUDA_OLD]: NVIDIA_DEVICE},
    )
    choice = bs.select_backend(
        candidates([paths[CUDA_OLD], paths[CUDA_NEW]]),
        probe=probes.version,
        has_nvidia=True,
        has_vulkan=False,
        confirm_devices=True,
        device_probe=probes.device,
    )
    assert choice.path == paths[CUDA_OLD]
    assert any("127" in reason for _, reason in choice.rejected)


def test_hung_build_is_rejected(tmp_path):
    paths = make_tree(tmp_path, CUDA12, VULKAN)
    probes = FakeProbes(
        {paths[VULKAN]: "version: 0.7.0-dev"},
        failures={paths[CUDA12]: "timed out after 10s"},
        devices={paths[VULKAN]: VULKAN_DEVICES},
    )
    choice = bs.select_backend(
        candidates([paths[CUDA12], paths[VULKAN]]),
        probe=probes.version,
        has_nvidia=True,
        has_vulkan=True,
        confirm_devices=True,
        device_probe=probes.device,
    )
    assert choice.path == paths[VULKAN]
    assert any("timed out" in reason for _, reason in choice.rejected)


def test_all_cuda_failing_falls_back_to_vulkan_then_cpu(tmp_path):
    paths = make_tree(tmp_path, CUDA_OLD, VULKAN, CPU)
    versions = {paths[VULKAN]: "version: 0.7.0-dev", paths[CPU]: "version: 0.6.0-dev"}

    probes = FakeProbes(versions, failures={paths[CUDA_OLD]: "exit 127"}, devices={paths[VULKAN]: VULKAN_DEVICES})
    with_vulkan = bs.select_backend(
        candidates([paths[CUDA_OLD], paths[VULKAN], paths[CPU]]),
        probe=probes.version,
        has_nvidia=True,
        has_vulkan=True,
        confirm_devices=True,
        device_probe=probes.device,
    )
    assert with_vulkan.path == paths[VULKAN]

    probes2 = FakeProbes(
        versions,
        failures={paths[CUDA_OLD]: "exit 127", paths[VULKAN]: "exit 1: no device"},
    )
    cpu_only = bs.select_backend(
        candidates([paths[CUDA_OLD], paths[VULKAN], paths[CPU]]),
        probe=probes2.version,
        has_nvidia=True,
        has_vulkan=True,
        confirm_devices=True,
        device_probe=probes2.device,
    )
    assert cpu_only.path == paths[CPU]


def test_without_nvidia_vulkan_beats_cpu(tmp_path):
    paths = make_tree(tmp_path, VULKAN, CPU)
    probes = FakeProbes(
        {paths[VULKAN]: "version: 0.7.0-dev", paths[CPU]: "version: 0.6.0-dev"},
        devices={paths[VULKAN]: VULKAN_DEVICES},
    )
    choice = bs.select_backend(
        candidates(list(paths.values())),
        probe=probes.version,
        has_nvidia=False,
        has_vulkan=True,
        confirm_devices=True,
        device_probe=probes.device,
    )
    assert choice.path == paths[VULKAN]


def test_without_any_gpu_cpu_build_is_used(tmp_path):
    paths = make_tree(tmp_path, CUDA12, CPU)
    probes = FakeProbes(
        {paths[CUDA12]: "version: 0.6.0-dev", paths[CPU]: "version: 0.6.0-dev"},
        devices={paths[CUDA12]: NVIDIA_DEVICE},
    )
    choice = bs.select_backend(
        candidates(list(paths.values())),
        probe=probes.version,
        has_nvidia=False,
        has_vulkan=False,
        confirm_devices=True,
        device_probe=probes.device,
    )
    assert choice.path == paths[CPU]
    # The CPU build never pays for --list-devices.
    assert probes.device_calls == []


def test_device_probe_rejects_build_that_cannot_see_a_gpu(tmp_path):
    paths = make_tree(tmp_path, CUDA12, CUDA_OLD)
    probes = FakeProbes(
        {paths[CUDA12]: "version: 0.6.0-dev", paths[CUDA_OLD]: "version: 0.1.2-dev"},
        noisy={paths[CUDA12]: "timed out after 10s"},
        devices={paths[CUDA_OLD]: NVIDIA_DEVICE},
    )
    choice = bs.select_backend(
        candidates([paths[CUDA12], paths[CUDA_OLD]]),
        probe=probes.version,
        has_nvidia=True,
        has_vulkan=False,
        confirm_devices=True,
        device_probe=probes.device,
    )
    assert choice.path == paths[CUDA_OLD]
    assert any("--list-devices" in reason for _, reason in choice.rejected)


def test_no_working_build_reports_every_rejection(tmp_path):
    paths = make_tree(tmp_path, CUDA_OLD, CPU)
    probes = FakeProbes({}, failures={paths[CUDA_OLD]: "exit 127", paths[CPU]: "exit 127"})
    choice = bs.select_backend(
        candidates(list(paths.values())),
        probe=probes.version,
        has_nvidia=True,
        has_vulkan=False,
    )
    assert choice.path is None
    assert len(choice.rejected) == 2
    assert "No usable llama-server build" in bs.describe_choice(choice)


# --------------------------------------------------------------------------
# pinned sources
# --------------------------------------------------------------------------
def test_pinned_candidate_wins_even_if_older(tmp_path):
    paths = make_tree(tmp_path, CUDA12, CPU)
    probes = FakeProbes(
        {paths[CUDA12]: "version: 0.6.0-dev", paths[CPU]: "version: 0.6.0-dev"},
        devices={paths[CUDA12]: NVIDIA_DEVICE},
    )
    choice = bs.select_backend(
        [bs.Candidate(paths[CPU], "env", True), bs.Candidate(paths[CUDA12], "lmstudio")],
        probe=probes.version,
        has_nvidia=True,
        has_vulkan=False,
        confirm_devices=True,
        device_probe=probes.device,
    )
    assert choice.path == paths[CPU]
    assert choice.pinned is True
    assert choice.source == "env"


def test_pinned_but_broken_falls_through_to_discovery(tmp_path):
    paths = make_tree(tmp_path, CUDA12, CPU)
    probes = FakeProbes(
        {paths[CUDA12]: "version: 0.6.0-dev"},
        failures={paths[CPU]: "exit 127"},
        devices={paths[CUDA12]: NVIDIA_DEVICE},
    )
    choice = bs.select_backend(
        [bs.Candidate(paths[CPU], "env", True), bs.Candidate(paths[CUDA12], "lmstudio")],
        probe=probes.version,
        has_nvidia=True,
        has_vulkan=False,
        confirm_devices=True,
        device_probe=probes.device,
    )
    assert choice.path == paths[CUDA12]
    assert any(paths[CPU] == rejected for rejected, _ in choice.rejected)


# --------------------------------------------------------------------------
# caching, env, reporting
# --------------------------------------------------------------------------
def test_version_probe_is_cached_for_the_session(monkeypatch):
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_runner(path, args, timeout):
        calls.append((path, tuple(args)))
        return bs.ProbeResult(True, version=(1, 2, 3))

    monkeypatch.setattr(bs, "_probe_runner", fake_runner)
    first = bs.probe_version_cached(r"C:\somewhere\llama-server.exe")
    second = bs.probe_version_cached(r"C:\somewhere\llama-server.exe")
    assert first == second == bs.ProbeResult(True, version=(1, 2, 3))
    assert len(calls) == 1


def test_server_env_puts_sibling_vendor_dlls_on_path(tmp_path):
    paths = make_tree(tmp_path, CUDA12)
    vendor = tmp_path / "vendor" / "win-llama-cuda12-vendor-v2"
    vendor.mkdir(parents=True)
    base_env = {"PATH": "C:\\original"}
    env = bs.server_env(paths[CUDA12], "cuda12", base_env)
    parts = env["PATH"].split(os.pathsep)
    assert str(vendor) in parts
    assert str(Path(paths[CUDA12]).parent) in parts
    assert parts[-1] == "C:\\original"
    assert base_env == {"PATH": "C:\\original"}  # caller's env is not mutated


def test_server_env_omits_missing_vendor_dirs(tmp_path):
    paths = make_tree(tmp_path, CPU)
    env = bs.server_env(paths[CPU], "cpu", {"PATH": ""})
    assert env["PATH"] == str(Path(paths[CPU]).parent)


def test_describe_choice_is_one_sentence_with_rejections(tmp_path):
    paths = make_tree(tmp_path, CUDA12, VULKAN)
    probes = FakeProbes(
        {paths[CUDA12]: "version: 0.6.0-dev"},
        failures={paths[VULKAN]: "exit 127"},
        devices={paths[CUDA12]: NVIDIA_DEVICE},
    )
    choice = bs.select_backend(
        [bs.Candidate(paths[VULKAN], "env", True), bs.Candidate(paths[CUDA12], "test")],
        probe=probes.version,
        has_nvidia=True,
        has_vulkan=True,
        confirm_devices=True,
        device_probe=probes.device,
    )
    text = bs.describe_choice(choice)
    assert "CUDA 12" in text and "0.6.0" in text
    assert paths[CUDA12] in text
    assert "1 other candidate" in text
    assert text.endswith(".") and "\n" not in text


def test_vulkan_note_prefers_largest_free_memory_and_says_it_does_not_choose():
    devices = bs.parse_devices(VULKAN_DEVICES)
    assert len(devices) == 2
    note = bs.vulkan_device_note(devices)
    assert "Vulkan1" in note  # 15376 MiB free beats 4311 MiB
    assert "does not pass --device" in note


# --------------------------------------------------------------------------
# CPU-only load note
# --------------------------------------------------------------------------
def test_explain_cpu_only_load_names_memory_and_processes():
    text = bs.explain_cpu_only_load(
        0,
        "NVIDIA GeForce RTX 5060 Ti",
        int(0.47 * 1024 ** 3),
        int(15.93 * 1024 ** 3),
        [
            {"pid": 2192, "name": "llama-server.exe", "rss_bytes": 2542 * 1024 ** 2},
            {"pid": 6068, "name": "llama-server.exe", "rss_bytes": 2557 * 1024 ** 2},
        ],
    )
    assert "CPU only" in text
    assert "0.47 GiB" in text and "15.93 GiB" in text
    assert "PID 2192" in text and "PID 6068" in text
    assert "2542 MB" in text
    assert "slow" in text


def test_explain_cpu_only_load_is_empty_when_it_does_not_apply():
    assert bs.explain_cpu_only_load(35, "RTX", 0, 0, []) == ""
    assert bs.explain_cpu_only_load(0, None, 0, 0, []) == ""
    text = bs.explain_cpu_only_load(0, "RTX", 0, 0, [])
    assert "No other llama-server process" in text


def test_explain_cpu_only_load_never_spawns_or_signals(monkeypatch):
    """The note is read-only: no spawn, kill, terminate or signal."""
    from giscode.inference import resource_budget

    class _Resources:
        gpu_name = "NVIDIA GeForce RTX 5060 Ti"
        available_vram_bytes = 500 * 1024 ** 2
        total_vram_bytes = 16 * 1024 ** 3

    monkeypatch.setattr(
        resource_budget.SystemResourceDetector,
        "get_system_resources",
        lambda self: _Resources(),
    )
    monkeypatch.setattr(runtime, "report_foreign_servers", lambda: [])

    def _boom(*args, **kwargs):
        raise AssertionError("the CPU-only note must not touch processes")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(os, "kill", _boom)

    text = runtime.explain_cpu_only_load(0, "model")
    assert "CPU only" in text

    # A GPU-offloaded load never builds the note at all.
    assert runtime.explain_cpu_only_load(35, "model") == ""
