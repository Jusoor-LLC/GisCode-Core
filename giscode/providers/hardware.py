"""Hardware guidance before a model is downloaded or activated.

The numbers come from the machinery that already decides real loads:
``DefaultResourceBudget.get_system_resources()`` reports RAM and VRAM, and
``estimate_model_memory()`` is the same estimator the loader uses. Nothing here
invents a second estimate.

The verdict is deliberately qualified. "Likely suitable" means the estimate fits
with headroom on this machine *now*; it is not a guarantee, because the estimator
cannot see drivers, other applications, or the model's real allocation pattern.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

SUITABLE = "likely_suitable"
MAY_FIT = "may_fit"
INSUFFICIENT = "likely_insufficient"
UNKNOWN = "unknown"

LABELS = {
    SUITABLE: "Likely suitable",
    MAY_FIT: "May fit with limitations",
    INSUFFICIENT: "Likely insufficient memory",
    UNKNOWN: "Not enough information",
}


@dataclass(frozen=True)
class Hardware:
    """What this machine has, as far as detection could establish."""

    total_ram_bytes: int = 0
    total_vram_bytes: int = 0
    available_vram_bytes: int = 0
    gpu_name: str = ""

    @property
    def has_gpu(self) -> bool:
        return self.total_vram_bytes > 0

    def summary(self) -> str:
        parts = []
        if self.gpu_name:
            parts.append(self.gpu_name)
        if self.total_vram_bytes:
            parts.append(f"{_gib(self.total_vram_bytes)} VRAM")
        if self.total_ram_bytes:
            parts.append(f"{_gib(self.total_ram_bytes)} RAM")
        return ", ".join(parts) if parts else "hardware not detected"


@dataclass(frozen=True)
class Compatibility:
    level: str
    headline: str
    detail: str
    estimate_bytes: int = 0
    model_size_bytes: int = 0

    @property
    def ok(self) -> bool:
        return self.level in (SUITABLE, MAY_FIT)

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "headline": self.headline,
            "detail": self.detail,
            "estimate_bytes": self.estimate_bytes,
            "model_size_bytes": self.model_size_bytes,
        }


def _gib(value: int) -> str:
    return f"{value / (1024 ** 3):.1f} GiB"


def _read_attr(obj, *names, default=0):
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return default


#: A hardware probe can involve a driver subprocess. Never let that subprocess
#: hold the Tk main thread: a full-suite run stalled inside nvidia-smi while a
#: window was being constructed. Detection is therefore bounded and cached.
_HARDWARE_TIMEOUT_S = 2.0
_HARDWARE_CACHE_TTL_S = 30.0
_HARDWARE_CACHE_LOCK = threading.Lock()
_HARDWARE_CACHE: Optional[Hardware] = None
_HARDWARE_CACHE_AT = 0.0


def _hardware_from_budget(budget) -> Hardware:
    try:
        resources = budget.get_system_resources()
    except Exception:
        return Hardware()
    return Hardware(
        total_ram_bytes=_read_attr(resources, "total_ram_bytes", "ram_bytes"),
        total_vram_bytes=_read_attr(resources, "total_vram_bytes", "vram_bytes"),
        available_vram_bytes=_read_attr(
            resources, "available_vram_bytes", "free_vram_bytes"
        ),
        gpu_name=str(getattr(resources, "gpu_name", "") or ""),
    )


def _detect_with_timeout(budget, timeout: float = _HARDWARE_TIMEOUT_S) -> Hardware:
    """Run a budget probe in a daemon thread and never wait past *timeout*.

    ``subprocess.run(timeout=...)`` alone is not sufficient on Windows: a
    driver command that leaves its output pipe open can keep ``communicate``
    blocked after the timeout. The UI only needs an answer, not the probe's
    process, so a timed-out probe returns unknown and is allowed to finish (or
    not) in the background.
    """
    result: dict[str, Hardware] = {}

    def work() -> None:
        result["value"] = _hardware_from_budget(budget)

    worker = threading.Thread(target=work, name="giscode-hardware-probe", daemon=True)
    worker.start()
    worker.join(max(0.0, float(timeout)))
    return result.get("value", Hardware())


def detect_hardware(budget=None) -> Hardware:
    """Detect RAM/VRAM through the existing resource budget.

    A budget that cannot answer yields zeros, which the assessment treats as
    unknown rather than assuming the machine can run anything. The default
    probe is also cached briefly: a GUI rebuild must not launch one driver
    process per window.
    """
    global _HARDWARE_CACHE, _HARDWARE_CACHE_AT

    if budget is None:
        now = time.monotonic()
        with _HARDWARE_CACHE_LOCK:
            if _HARDWARE_CACHE is not None and now - _HARDWARE_CACHE_AT < _HARDWARE_CACHE_TTL_S:
                return _HARDWARE_CACHE
        try:
            from giscode.inference.resource_budget import DefaultResourceBudget

            budget = DefaultResourceBudget()
        except Exception:
            return Hardware()

    detected = _detect_with_timeout(budget)
    if budget is not None:
        with _HARDWARE_CACHE_LOCK:
            _HARDWARE_CACHE = detected
            _HARDWARE_CACHE_AT = time.monotonic()
    return detected


def _estimate(size_bytes: int, context_length: int, budget) -> int:
    if budget is not None and hasattr(budget, "estimate_model_memory"):
        try:
            return int(budget.estimate_model_memory(size_bytes, context_length))
        except Exception:
            pass
    # Fallback: weights plus a proportional allowance for runtime overhead and
    # KV cache. This is a coarse figure and is labelled as such in the text.
    return int(size_bytes * 1.15) + int(context_length * 96 * 1024)


def assess(
    size_bytes: int,
    *,
    context_length: int = 4096,
    hardware: Optional[Hardware] = None,
    budget=None,
) -> Compatibility:
    """Estimate whether a model of *size_bytes* can run here."""
    if size_bytes <= 0:
        return Compatibility(
            level=UNKNOWN,
            headline=LABELS[UNKNOWN],
            detail="The model's size is not known yet, so memory use cannot be estimated.",
        )
    hardware = hardware or detect_hardware(budget)
    estimate = _estimate(size_bytes, max(1, context_length), budget)
    estimate_text = _gib(estimate)

    if hardware.has_gpu:
        free = hardware.available_vram_bytes or hardware.total_vram_bytes
        if estimate <= free * 0.9:
            return Compatibility(
                level=SUITABLE,
                headline=LABELS[SUITABLE],
                detail=(
                    f"About {estimate_text} at {context_length} tokens, against "
                    f"{_gib(free)} of free VRAM on {hardware.gpu_name or 'the GPU'}."
                ),
                estimate_bytes=estimate,
                model_size_bytes=size_bytes,
            )
        if hardware.total_ram_bytes and estimate <= hardware.total_ram_bytes * 0.8:
            return Compatibility(
                level=MAY_FIT,
                headline=LABELS[MAY_FIT],
                detail=(
                    f"About {estimate_text} does not fit the {_gib(free)} of free VRAM, "
                    "so generation would run partly or entirely on the CPU and be slow."
                ),
                estimate_bytes=estimate,
                model_size_bytes=size_bytes,
            )
        return Compatibility(
            level=INSUFFICIENT,
            headline=LABELS[INSUFFICIENT],
            detail=(
                f"About {estimate_text} exceeds both the free VRAM "
                f"({_gib(free)}) and a safe share of system RAM."
            ),
            estimate_bytes=estimate,
            model_size_bytes=size_bytes,
        )

    if hardware.total_ram_bytes:
        if estimate <= hardware.total_ram_bytes * 0.7:
            return Compatibility(
                level=MAY_FIT,
                headline=LABELS[MAY_FIT],
                detail=(
                    f"No GPU detected; about {estimate_text} of {_gib(hardware.total_ram_bytes)} "
                    "system RAM. CPU inference works but is much slower."
                ),
                estimate_bytes=estimate,
                model_size_bytes=size_bytes,
            )
        return Compatibility(
            level=INSUFFICIENT,
            headline=LABELS[INSUFFICIENT],
            detail=(
                f"About {estimate_text} is too much for {_gib(hardware.total_ram_bytes)} "
                "of system RAM with no GPU to offload to."
            ),
            estimate_bytes=estimate,
            model_size_bytes=size_bytes,
        )

    return Compatibility(
        level=UNKNOWN,
        headline=LABELS[UNKNOWN],
        detail="RAM and VRAM could not be detected, so this model's fit is unverified.",
        estimate_bytes=estimate,
        model_size_bytes=size_bytes,
    )
