"""Hardware probes must never block the UI indefinitely.

A full-suite run stopped with the main thread inside
``subprocess.run(["nvidia-smi", ...])`` while a Tk window was being built.
These tests pin a bounded timeout on every NVIDIA probe and fallback to
unknown instead of propagating the timeout.
"""
from __future__ import annotations

import subprocess

import pytest

from giscode.inference.resource_budget import DefaultResourceBudget


@pytest.mark.parametrize(
    "method_name",
    [
        "_detect_nvidia_gpu_windows",
        "_detect_nvidia_gpu_linux",
        "_get_nvidia_vram_windows",
        "_get_nvidia_vram_free_windows",
        "_get_nvidia_vram_linux",
        "_get_nvidia_vram_free_linux",
    ],
)
def test_nvidia_probe_is_bounded_and_falls_back(monkeypatch, method_name):
    calls = []

    def hang(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", hang)

    result = getattr(DefaultResourceBudget(), method_name)()

    assert result is None
    assert calls, "the probe did not run"
    assert calls[0][1].get("timeout") is not None
    assert 0 < calls[0][1]["timeout"] <= 5.0
