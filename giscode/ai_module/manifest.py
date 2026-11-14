"""Pinned runtime and model artifacts.

Every executable component is named with an exact size and SHA-256 from the
upstream release.  A download that does not match is deleted, never run.  The
runtime is llama.cpp b12373 (MIT); the model entries carry their own weight
licences, which are separate from the runtime licence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

GIB = 1024 ** 3
MIB = 1024 ** 2

RUNTIME_VERSION = "b12373"
RUNTIME_LICENSE = "MIT"
RUNTIME_SOURCE = "https://github.com/ggml-org/llama.cpp"
RUNTIME_RELEASE_URL = "https://github.com/ggml-org/llama.cpp/releases/tag/b12373"
#: CUDA 13.5 needs a 580-series or newer NVIDIA driver.  A machine with an
#: older driver gets Vulkan instead of a CUDA build that cannot start.
CUDA_MIN_DRIVER_MAJOR = 580


@dataclass(frozen=True)
class RuntimeComponent:
    filename: str
    url: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class RuntimeBuild:
    key: str
    label: str
    family: str
    components: tuple[RuntimeComponent, ...]
    description: str

    @property
    def download_bytes(self) -> int:
        return sum(component.size_bytes for component in self.components)


def _llama(name: str, size: int, sha: str) -> RuntimeComponent:
    return RuntimeComponent(
        filename=name,
        url=f"https://github.com/ggml-org/llama.cpp/releases/download/{RUNTIME_VERSION}/{name}",
        size_bytes=size,
        sha256=sha,
    )


RUNTIME_BUILDS: dict[str, RuntimeBuild] = {
    "cuda": RuntimeBuild(
        key="cuda",
        label="CUDA 13.5",
        family="cuda",
        components=(
            _llama(
                "llama-b12373-bin-win-cuda-13.5-x64.zip",
                202785240,
                "ac1f9cbdb461a5862048af17cc9715ce66afe760f24c612587a9b75f800f3855",
            ),
            _llama(
                "cudart-llama-bin-win-cuda-13.5-x64.zip",
                426314199,
                "c2bf4cc202bfbaaa7a0b75f5082457c3811326947bb216059b72839d844e9cba",
            ),
        ),
        description="NVIDIA GPU acceleration (CUDA 13.5 runtime included).",
    ),
    "vulkan": RuntimeBuild(
        key="vulkan",
        label="Vulkan",
        family="vulkan",
        components=(
            _llama(
                "llama-b12373-bin-win-vulkan-x64.zip",
                38113263,
                "ccc20527784e11e02b65d59d3201f3bc0c91b3a99688d1eec32c49d7126f82ae",
            ),
        ),
        description="AMD or Intel graphics (and most modern NVIDIA cards) via Vulkan.",
    ),
    "cpu": RuntimeBuild(
        key="cpu",
        label="CPU",
        family="cpu",
        components=(
            _llama(
                "llama-b12373-bin-win-cpu-x64.zip",
                25116679,
                "e2625c3a00424f141d1ff5d2b196597060bd812694226e7b45bf4c75b3a1515d",
            ),
        ),
        description="Portable CPU build; slower, but works without a supported GPU.",
    ),
}


@dataclass(frozen=True)
class ModelCandidate:
    key: str
    name: str
    filename: str
    url: str
    size_bytes: int
    sha256: str
    license: str
    license_url: str
    commercial_use: bool
    redistribution: str
    quantization: str
    context_length: int
    min_ram_bytes: int
    recommended_vram_bytes: int
    description: str
    #: Metadata shown in the selector.  ``expected_performance`` is always an
    #: estimate unless the candidate has a measured qualification result; the
    #: UI labels it as such and never presents it as a benchmark.
    publisher: str = "Qwen"
    source_url: str = ""
    revision: str = "main"
    parameter_count: str = ""
    expected_performance: str = ""
    #: False until this exact artifact has passed a real GisCode inference and
    #: coding-correctness run.  A candidate is never silently promoted.
    qualified: bool = False
    qualification: str = ""

    def fits(
        self,
        *,
        total_ram: int,
        total_vram: int,
        disk_free: int,
        available_ram: int = 0,
        available_vram: int = 0,
    ) -> tuple[bool, str]:
        if disk_free and disk_free < self.size_bytes * 1.10:
            return False, "Not enough free disk space for the download."
        if total_ram and total_ram < self.min_ram_bytes:
            return False, "This PC does not have enough system memory."
        # A model that fits in currently-free VRAM does not need free system
        # RAM for its weights.  The old order checked available RAM first and
        # refused a model on a GPU-rich machine whenever other applications had
        # committed RAM, even though the model could run fully on the GPU.
        if available_vram and available_vram >= self.recommended_vram_bytes:
            return True, "Fits entirely in available graphics memory."
        # Without enough free VRAM the loader may spill to RAM, so available
        # system memory now matters.  A large total with almost all of it
        # already committed is not a fit.
        if available_ram and available_ram < self.min_ram_bytes:
            return False, "Not enough memory is free right now; close other applications."
        if total_vram and total_vram >= self.recommended_vram_bytes:
            return True, "Fits entirely in graphics memory."
        if total_ram and total_ram >= self.min_ram_bytes:
            return True, "Runs with partial GPU offload or on the CPU."
        return False, "Memory could not be confirmed."


MODEL_CANDIDATES: tuple[ModelCandidate, ...] = (
    ModelCandidate(
        key="qwen2.5-coder-1.5b-instruct-q4_k_m",
        name="Qwen2.5-Coder-1.5B-Instruct",
        filename="qwen2.5-coder-1.5b-instruct-q4_k_m.gguf",
        url=(
            "https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF/"
            "resolve/main/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf"
        ),
        size_bytes=1117320768,
        sha256="cc324af070c2ecbfd324a30884d2f951a7ff756aba85cb811a6ec436933bb046",
        license="Apache-2.0",
        license_url="https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct",
        commercial_use=True,
        redistribution=(
            "Apache-2.0 permits redistribution and modification; retain the "
            "licence and attribution notices with redistributed weights."
        ),
        quantization="Q4_K_M",
        context_length=32768,
        min_ram_bytes=6 * GIB,
        recommended_vram_bytes=2 * GIB,
        description=(
            "Small, fast coding model for low-memory PCs. Use it as a starter; "
            "the 7B model is the better coding choice when enough memory is free."
        ),
        publisher="Qwen (Alibaba)",
        source_url="https://huggingface.co/Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF",
        parameter_count="1.5B",
        expected_performance="Measured on this qualification PC: ~288 tokens/s, first token <0.1 s.",
        qualified=True,
        qualification=(
            "GisCode real-inference qualification 2026-11-13: llama.cpp b12373 "
            "CUDA 13.5, RTX 5060 Ti, first token 0.06 s, 289 tok/s; generated "
            "add(a, b) passed two isolated checks."
        ),
    ),
    ModelCandidate(
        key="qwen3-coder-30b-a3b-instruct-q4_k_m",
        name="Qwen3-Coder-30B-A3B-Instruct",
        filename="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
        url=(
            "https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/"
            "resolve/main/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"
        ),
        size_bytes=18556689568,
        sha256="fadc3e5f8d42bf7e894a785b05082e47daee4df26680389817e2093056f088ad",
        license="Apache-2.0",
        license_url="https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct",
        commercial_use=True,
        redistribution=(
            "Apache-2.0 permits redistribution and modification; retain the "
            "licence and attribution notices with redistributed weights."
        ),
        quantization="Q4_K_M",
        context_length=32768,
        min_ram_bytes=24 * GIB,
        recommended_vram_bytes=18 * GIB,
        description=(
            "Coding-focused 30B mixture-of-experts model. Best quality of the "
            "two candidates, but needs a large download and substantial memory."
        ),
        publisher="Qwen (Alibaba)",
        source_url="https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF",
        parameter_count="30B / 3B active",
        expected_performance="Not measured by GisCode; estimated high quality and slower than the 1.5B model. Requires a high-memory GPU.",
    ),
    ModelCandidate(
        key="qwen2.5-coder-7b-instruct-q4_k_m",
        name="Qwen2.5-Coder-7B-Instruct",
        filename="qwen2.5-coder-7b-instruct-q4_k_m.gguf",
        url=(
            "https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF/"
            "resolve/main/qwen2.5-coder-7b-instruct-q4_k_m.gguf"
        ),
        size_bytes=4683073536,
        sha256="509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c",
        license="Apache-2.0",
        license_url="https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct",
        commercial_use=True,
        redistribution=(
            "Apache-2.0 permits redistribution and modification; retain the "
            "licence and attribution notices with redistributed weights."
        ),
        quantization="Q4_K_M",
        context_length=32768,
        min_ram_bytes=9 * GIB,
        recommended_vram_bytes=6 * GIB,
        description=(
            "Fast, widely used coding model that fits the 8 GB+ memory class. "
            "The recommended first module for most PCs."
        ),
        publisher="Qwen (Alibaba)",
        source_url="https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
        parameter_count="7B",
        expected_performance="Not measured by GisCode; expected to be slower than 1.5B and better at coding. Qualification is pending on a machine with enough free RAM.",
    ),
)


def candidate(key: str) -> ModelCandidate | None:
    return next((item for item in MODEL_CANDIDATES if item.key == key), None)


def candidate_for_name(name: str) -> ModelCandidate | None:
    wanted = (name or "").strip().lower()
    return next(
        (
            item
            for item in MODEL_CANDIDATES
            if item.key.lower() == wanted or item.name.lower() == wanted
        ),
        None,
    )


def runtime_build(key: str) -> RuntimeBuild:
    try:
        return RUNTIME_BUILDS[key]
    except KeyError as exc:
        raise KeyError(f"unknown runtime backend: {key!r}") from exc


#: Licences that must ship with the installer and stay next to an installed
#: runtime.  The model licence is fetched from the model repository and is
#: recorded in the installed module metadata; it is not the runtime licence.
THIRD_PARTY_NOTICES: ClassVar[str] = ""
