"""What this machine actually has, measured rather than assumed.

Detection is not reimplemented here. ``giscode.providers.hardware`` already
probes RAM and VRAM through the same resource budget the loader uses, with a
bounded timeout and a short cache, so a wizard cannot hang on a driver
subprocess. This module adds the two facts that budget does not report -
free disk space and CPU count - and packages everything into one immutable
record that can be persisted in the onboarding state.

``None`` is never used for "unknown": a zero means the probe could not establish
the value, and the recommendation code treats zero as unknown rather than as
"no memory", so a failed probe cannot produce a confident wrong answer.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from giscode.config.paths import giscode_home
from giscode.providers.hardware import Hardware as ProviderHardware
from giscode.providers.hardware import assess as provider_assess
from giscode.providers.hardware import detect_hardware

GIB = 1024 ** 3

#: A pull needs the weights plus manifests and a little scratch space, and the
#: runtime needs room for the KV cache and the OS. Refusing a download that only
#: just fits avoids filling the system drive.
DISK_HEADROOM = 2 * GIB


def _gib(value: float) -> float:
    return round(float(value) / GIB, 1)


@dataclass(frozen=True)
class SystemProfile:
    """One snapshot of the machine, safe to persist and to display."""

    ram_total_bytes: int = 0
    ram_available_bytes: int = 0
    vram_total_bytes: int = 0
    vram_available_bytes: int = 0
    gpu_name: str = ""
    cpu_count: int = 0
    disk_total_bytes: int = 0
    disk_free_bytes: int = 0
    disk_path: str = ""
    os_name: str = ""
    probed_at: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    # -- display ------------------------------------------------------------
    @property
    def ram_total_gb(self) -> float:
        return _gib(self.ram_total_bytes)

    @property
    def ram_available_gb(self) -> float:
        return _gib(self.ram_available_bytes)

    @property
    def vram_total_gb(self) -> float:
        return _gib(self.vram_total_bytes)

    @property
    def disk_free_gb(self) -> float:
        return _gib(self.disk_free_bytes)

    @property
    def has_gpu(self) -> bool:
        return self.vram_total_bytes > 0

    def summary(self) -> str:
        parts = []
        if self.gpu_name:
            parts.append(f"{self.gpu_name} ({self.vram_total_gb:.1f} GB VRAM)")
        elif self.vram_total_bytes:
            parts.append(f"{self.vram_total_gb:.1f} GB VRAM")
        else:
            parts.append("no dedicated GPU detected")
        if self.ram_total_bytes:
            parts.append(f"{self.ram_total_gb:.1f} GB RAM")
        if self.disk_total_bytes:
            parts.append(f"{self.disk_free_gb:.1f} GB free on {self.disk_path or 'the profile drive'}")
        return ", ".join(parts)

    def as_provider_hardware(self) -> ProviderHardware:
        """The budget-shaped record the existing fit estimator consumes."""
        return ProviderHardware(
            total_ram_bytes=self.ram_total_bytes,
            total_vram_bytes=self.vram_total_bytes,
            available_vram_bytes=self.vram_available_bytes or self.vram_total_bytes,
            gpu_name=self.gpu_name,
        )

    def as_dict(self) -> dict:
        return {
            "ram_total_bytes": self.ram_total_bytes,
            "ram_available_bytes": self.ram_available_bytes,
            "vram_total_bytes": self.vram_total_bytes,
            "vram_available_bytes": self.vram_available_bytes,
            "gpu_name": self.gpu_name,
            "cpu_count": self.cpu_count,
            "disk_total_bytes": self.disk_total_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "disk_path": self.disk_path,
            "os_name": self.os_name,
            "probed_at": self.probed_at,
            "notes": list(self.notes),
            "summary": self.summary(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SystemProfile":
        if not isinstance(data, dict):
            return cls()
        notes = data.get("notes")
        return cls(
            ram_total_bytes=int(data.get("ram_total_bytes") or 0),
            ram_available_bytes=int(data.get("ram_available_bytes") or 0),
            vram_total_bytes=int(data.get("vram_total_bytes") or 0),
            vram_available_bytes=int(data.get("vram_available_bytes") or 0),
            gpu_name=str(data.get("gpu_name") or ""),
            cpu_count=int(data.get("cpu_count") or 0),
            disk_total_bytes=int(data.get("disk_total_bytes") or 0),
            disk_free_bytes=int(data.get("disk_free_bytes") or 0),
            disk_path=str(data.get("disk_path") or ""),
            os_name=str(data.get("os_name") or ""),
            probed_at=str(data.get("probed_at") or ""),
            notes=tuple(str(n) for n in notes) if isinstance(notes, list) else (),
        )


def _memory_snapshot() -> tuple[int, int]:
    """Total and available RAM, or ``(0, 0)`` when psutil cannot answer."""
    try:
        import psutil

        memory = psutil.virtual_memory()
        return int(memory.total), int(memory.available)
    except Exception:
        return 0, 0


def _disk_snapshot(path: Optional[str]) -> tuple[int, int, str]:
    target = path or str(giscode_home())
    try:
        usage = shutil.disk_usage(target)
        return int(usage.total), int(usage.free), target
    except OSError:
        return 0, 0, target


def probe(
    *,
    path: Optional[str] = None,
    budget=None,
    hardware=None,
    memory=None,
    disk_usage=None,
) -> SystemProfile:
    """Measure the machine once, without raising.

    Every dependency is injectable so the qualification suite can describe a
    small laptop or a machine with no GPU deterministically, instead of only
    being able to test the computer the tests happen to run on.
    """
    hardware = hardware if hardware is not None else detect_hardware(budget)
    source = memory or _memory_snapshot
    if isinstance(source, (tuple, list)):  # memory=(total_bytes, available_bytes)
        total_ram, available_ram = int(source[0]), int(source[1])
    else:
        total_ram, available_ram = source()
    total_disk, free_disk, disk_path = (disk_usage or _disk_snapshot)(path)

    notes: list[str] = []
    if total_ram <= 0:
        notes.append("System RAM could not be measured; model fit will be reported as unverified.")
    if not getattr(hardware, "gpu_name", "") and not getattr(hardware, "total_vram_bytes", 0):
        notes.append("No NVIDIA GPU was detected; local models will run on the CPU and be slower.")
    if free_disk <= 0:
        notes.append("Free disk space could not be measured; the download is not pre-checked.")

    return SystemProfile(
        ram_total_bytes=int(total_ram),
        ram_available_bytes=int(available_ram),
        vram_total_bytes=int(getattr(hardware, "total_vram_bytes", 0) or 0),
        vram_available_bytes=int(getattr(hardware, "available_vram_bytes", 0) or 0),
        gpu_name=str(getattr(hardware, "gpu_name", "") or ""),
        cpu_count=os.cpu_count() or 0,
        disk_total_bytes=int(total_disk),
        disk_free_bytes=int(free_disk),
        disk_path=disk_path,
        os_name=os.name,
        probed_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        notes=tuple(notes),
    )


def required_disk_bytes(model_size_bytes: int) -> int:
    """Bytes a pull needs free before it is allowed to start."""
    size = max(0, int(model_size_bytes))
    return size + int(size * 0.10) + DISK_HEADROOM


def fits_disk(profile: SystemProfile, model_size_bytes: int) -> bool:
    """False only when free space was measured *and* is insufficient."""
    if profile.disk_free_bytes <= 0:
        return True  # unknown is not a refusal
    return profile.disk_free_bytes >= required_disk_bytes(model_size_bytes)


def assess_fit(profile: SystemProfile, model_size_bytes: int, *, context_length: int = 8192):
    """Delegate the memory verdict to the estimator the loader itself uses."""
    return provider_assess(
        model_size_bytes,
        context_length=context_length,
        hardware=profile.as_provider_hardware(),
    )
