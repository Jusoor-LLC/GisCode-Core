"""A blocked hardware probe must not freeze the caller.

The full suite stopped while constructing a Tk window because hardware
detection waited inside a driver subprocess. Detection now runs in a daemon
thread with a bounded wait and returns unknown if the probe does not answer.
"""
from __future__ import annotations

import time

from giscode.providers.hardware import Hardware, detect_hardware


class _BlockingBudget:
    def get_system_resources(self):
        time.sleep(30)


class _FastBudget:
    class Resources:
        total_ram_bytes = 8 * 1024**3
        total_vram_bytes = 4 * 1024**3
        available_vram_bytes = 3 * 1024**3
        gpu_name = "Test GPU"

    def get_system_resources(self):
        return self.Resources()


def test_a_blocked_probe_returns_unknown_without_waiting():
    started = time.monotonic()
    result = detect_hardware(_BlockingBudget())
    elapsed = time.monotonic() - started

    assert isinstance(result, Hardware)
    assert result == Hardware()
    assert elapsed < 5.0


def test_a_fast_probe_still_returns_its_values():
    result = detect_hardware(_FastBudget())

    assert result.total_ram_bytes == 8 * 1024**3
    assert result.total_vram_bytes == 4 * 1024**3
    assert result.available_vram_bytes == 3 * 1024**3
    assert result.gpu_name == "Test GPU"
