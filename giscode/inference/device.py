"""Portable answers to "where is this model actually running?".

Three rules, each of which the earlier stages found broken somewhere:

* A GPU claim needs evidence from the *backend* and the *layer count*. A model
  being installed, or a runtime being selected, proves nothing about the device.
* Unified memory (Apple Silicon, and any future SoC) is not separate graphics
  memory: reporting "16 GiB VRAM" on such a machine would be a lie, and the
  budget must be treated as shared.
* An unknown backend is reported as unknown. It is never upgraded to GPU, and it
  is never downgraded to a confident CPU claim either.

Nothing here starts, stops or probes a server; it only interprets facts the
loader already has. That keeps it safe to call from tests, the GUI and the
reporting path.
"""
from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from typing import Optional

#: Backend families that mean "this build executes on a GPU".
#:
#: ``cuda``/``cuda12`` and ``vulkan`` are produced by
#: :mod:`giscode.inference.backend_selection` today; ``rocm``, ``metal``,
#: ``sycl`` and ``hip`` are listed so a future adapter that reports them is
#: classified correctly without touching this module. Listing a family is not a
#: claim that GisCode implements it - see ``docs``/the report.
GPU_FAMILIES = frozenset({"cuda", "cuda12", "cuda11", "rocm", "hip", "metal", "vulkan", "sycl"})

#: Families that mean "CPU only".
CPU_FAMILIES = frozenset({"cpu", "avx2", "avx", "sse"})

DEVICE_GPU = "gpu"
DEVICE_MIXED = "mixed"
DEVICE_CPU = "cpu"
DEVICE_UNKNOWN = "unknown"


def os_family(platform_name: Optional[str] = None) -> str:
    """``windows`` | ``linux`` | ``macos`` | ``other``."""
    name = (platform_name if platform_name is not None else sys.platform).strip().lower()
    if name.startswith(("win", "cygwin", "msys")):
        return "windows"
    if name.startswith("darwin") or name.startswith("mac"):
        return "macos"
    if name.startswith("linux"):
        return "linux"
    return "other"


def machine_arch(machine: Optional[str] = None) -> str:
    value = (machine if machine is not None else platform.machine()).strip().lower()
    aliases = {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}
    return aliases.get(value, value)


def uses_unified_memory(
    platform_name: Optional[str] = None,
    machine: Optional[str] = None,
) -> bool:
    """True when the GPU shares system memory with the CPU.

    Apple Silicon is the case that exists today (Metal on ``arm64`` macOS).
    Nothing else is assumed: an x86 machine with integrated graphics still has
    a separate, smaller graphics aperture, and treating it as unified would
    overstate what can be offloaded.
    """
    return os_family(platform_name) == "macos" and machine_arch(machine) == "arm64"


def _family(value: str) -> str:
    return str(value or "").strip().lower()


def _format_bytes(value: int) -> str:
    try:
        size = float(int(value))
    except (TypeError, ValueError):
        return "unknown"
    if size <= 0:
        return "unknown"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


@dataclass(frozen=True)
class ExecutionReport:
    """Where one loaded model runs, and the evidence for saying so."""

    device: str
    backend_family: str
    gpu_layers: int = 0
    n_ctx: int = 0
    gpu_name: str = ""
    unified_memory: bool = False
    verified: bool = False
    evidence: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_gpu(self) -> bool:
        return self.device in {DEVICE_GPU, DEVICE_MIXED}

    @property
    def label(self) -> str:
        if self.device == DEVICE_GPU:
            return "GPU" + (" (unified memory)" if self.unified_memory else "")
        if self.device == DEVICE_MIXED:
            return "GPU + CPU (layers offloaded)"
        if self.device == DEVICE_CPU:
            return "CPU"
        return "device not verified"

    def describe(self) -> str:
        """One sentence for a log line, a note or a report."""
        parts = [f"execution: {self.label}"]
        if self.backend_family:
            parts.append(f"backend {self.backend_family}")
        if self.verified and self.gpu_layers:
            layers = "all" if self.gpu_layers < 0 else str(self.gpu_layers)
            parts.append(f"gpu_layers={layers}")
        if self.n_ctx:
            parts.append(f"n_ctx={self.n_ctx}")
        if not self.verified:
            parts.append("(unverified)")
        text = ", ".join(parts)
        if self.evidence:
            text += f" - {self.evidence}"
        return text

    def as_dict(self) -> dict:
        return {
            "device": self.device,
            "label": self.label,
            "backend_family": self.backend_family,
            "gpu_layers": self.gpu_layers,
            "n_ctx": self.n_ctx,
            "gpu_name": self.gpu_name,
            "unified_memory": self.unified_memory,
            "verified": self.verified,
            "evidence": self.evidence,
            "warnings": list(self.warnings),
        }


def execution_report(
    *,
    backend_family: str,
    gpu_layers: int = 0,
    n_ctx: int = 0,
    resources=None,
    platform_name: Optional[str] = None,
    machine: Optional[str] = None,
) -> ExecutionReport:
    """Classify the device from the backend family and the offloaded layer count.

    ``resources`` is any object with ``available_vram_bytes``,
    ``total_vram_bytes`` and ``gpu_name`` (see
    :class:`giscode.inference.interfaces.SystemResources`); it is used for
    description only and never as evidence of a GPU.
    """
    family = _family(backend_family)
    layers = int(gpu_layers or 0)
    unified = uses_unified_memory(platform_name, machine)
    gpu_name = str(getattr(resources, "gpu_name", "") or "")
    warnings: list[str] = []

    if family in GPU_FAMILIES:
        if layers == -1:
            device = DEVICE_GPU
            evidence = f"the {family} build loaded every layer on the GPU"
        elif layers > 0:
            device = DEVICE_MIXED
            evidence = f"the {family} build offloaded {layers} layer(s); the rest ran on the CPU"
        else:
            # A GPU build with no offloaded layers is a CPU run in a GPU binary.
            device = DEVICE_CPU
            evidence = f"the {family} build was selected but no layers were offloaded"
            warnings.append(
                "A GPU build was selected but the model ran entirely on the CPU; "
                "the graphics device was not used."
            )
        verified = True
    elif family in CPU_FAMILIES:
        device = DEVICE_CPU
        evidence = f"the only usable build is the {family} build"
        verified = True
        warnings.append("This run uses the CPU; no graphics device was used.")
        if layers > 0:
            # A CPU-only build cannot honour an offload request; saying nothing
            # would let a caller believe the layer count was applied.
            evidence += "; the build has no graphics support, so the offload request was ignored"
            warnings.append(
                f"{layers} layer(s) were requested for offload but the {family} build "
                "cannot use a graphics device; the offload request was ignored and the "
                "model ran on the CPU."
            )
    else:
        device = DEVICE_UNKNOWN
        evidence = "the runtime did not report which backend it used"
        verified = False

    if unified:
        # On a unified-memory machine the GPU has no private memory of its own.
        vram = _format_bytes(getattr(resources, "total_vram_bytes", 0) or 0)
        evidence += (
            f"; on {os_family(platform_name)}/{machine_arch(machine)} graphics memory is shared "
            "with system memory"
            + (f" (reported aperture {vram})" if vram != "unknown" else "")
        )

    return ExecutionReport(
        device=device,
        backend_family=family,
        gpu_layers=layers,
        n_ctx=int(n_ctx or 0),
        gpu_name=gpu_name,
        unified_memory=unified,
        verified=verified,
        evidence=evidence,
        warnings=tuple(warnings),
    )
