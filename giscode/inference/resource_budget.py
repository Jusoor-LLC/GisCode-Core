import logging
import os
import platform
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from giscode.inference.gguf import GGUFMetadata, read_metadata
from giscode.inference.interfaces import ResourceBudget, SystemResources


logger = logging.getLogger(__name__)


KV_CACHE_BYTES_DEFAULT = 2  # fp16 (2 bytes per element)
SAFETY_MARGIN_DEFAULT = 1.20  # 20% slack for runtime buffers and fragmentation

GIB = 1024 ** 3
# Compute buffer (llama-server "sched_reserve: compute buffer size") scales
# with context length, not a flat allowance. Measured on the saved startup
# logs (p4_log_*.txt): dense 4B @16384 -> 144 MiB and dense 27B @32768 ->
# 314 MiB are both ~9-10 KiB per token of context; the MoE 30B @4096 ->
# 100 MiB is ~25 KiB per token (expert state). A flat 256 MiB missed the
# 27B by -18.5% and over-counted the 4B/30B by +78%/+156%.
COMPUTE_BYTES_PER_TOKEN_DENSE = 9 * 1024
COMPUTE_BYTES_PER_TOKEN_MOE = 25 * 1024
VRAM_MARGIN_BYTES = 1 * GIB          # keep ~1 GiB VRAM free
RAM_MARGIN_BYTES = 4 * GIB           # keep ~4 GiB RAM free
VRAM_OVERHEAD_BYTES = 256 * 1024 * 1024   # CUDA context / driver allocations
RAM_OVERHEAD_BYTES = 256 * 1024 * 1024    # process, buffers, page tables

# Hybrid (attention + recurrent/SSM) models keep a per-sequence recurrent state
# (llama.cpp's "RS buffer") in addition to the KV cache. llama-server allocates
# that state for a small default pool of sequences (measured: 4). Ignoring it
# under-counted VRAM/RAM by ~0.5 GiB on the 27B, enough to plan a load that
# then failed to fit.
RECURRENT_CELLS_DEFAULT = 4

# Order of context sizes to try during automatic step-down. Each value must
# satisfy n_ctx >= 256 (llama.cpp minimum) and be strictly decreasing.
DEFAULT_STEPDOWN_SEQUENCE: Sequence[int] = (16384, 12288, 8192, 6144, 4096, 2048, 1024)


@dataclass(frozen=True)
class MemoryEstimate:
    model_size_bytes: int
    kv_cache_bytes: int
    overhead_bytes: int
    safety_margin: float
    bytes_per_element: int
    block_count: int
    head_count_kv: int
    head_dim: int

    @property
    def total_bytes(self) -> int:
        base = self.model_size_bytes + self.kv_cache_bytes + self.overhead_bytes
        return int(base * self.safety_margin)


def _kv_bytes_per_token(meta: GGUFMetadata, bytes_per_element: int) -> int:
    if (
        meta.block_count is None
        or meta.attention_head_count_kv is None
        or meta.head_dim is None
    ):
        return 0
    return (
        2  # K and V caches
        * meta.block_count
        * meta.attention_head_count_kv
        * meta.head_dim
        * bytes_per_element
    )


def estimate_memory_from_metadata(
    meta: GGUFMetadata,
    context_length: int,
    *,
    bytes_per_element: int = KV_CACHE_BYTES_DEFAULT,
    safety_margin: float = SAFETY_MARGIN_DEFAULT,
    overhead_bytes: int = 256 * 1024 * 1024,
) -> MemoryEstimate:
    """Return a concrete estimate derived from real GGUF metadata.

    The KV cache size is computed from the model's actual layer count, KV
    head count and head dimension. If any of those fields are missing (e.g.
    an exotic GGUF), the KV cache size is conservatively set to one fifth
    of the model file size per 1024 tokens, which is still orders of
    magnitude smaller than the buggy `n_ctx * 4 MiB` formula.
    """
    file_size = meta.file_size_bytes
    per_token = _kv_bytes_per_token(meta, bytes_per_element)
    if per_token <= 0:
        per_token = max(64 * 1024, file_size // 5120)
    kv = per_token * max(0, context_length)
    return MemoryEstimate(
        model_size_bytes=file_size,
        kv_cache_bytes=kv,
        overhead_bytes=overhead_bytes,
        safety_margin=safety_margin,
        bytes_per_element=bytes_per_element,
        block_count=int(meta.block_count or 0),
        head_count_kv=int(meta.attention_head_count_kv or 0),
        head_dim=int(meta.head_dim or 0),
    )


def estimate_memory_from_path(
    path: str,
    context_length: int,
    *,
    bytes_per_element: int = KV_CACHE_BYTES_DEFAULT,
    safety_margin: float = SAFETY_MARGIN_DEFAULT,
    metadata_loader: Optional[Callable[[str], GGUFMetadata]] = None,
) -> MemoryEstimate:
    p = os.fspath(path)
    loader = metadata_loader or read_metadata
    meta = loader(p)
    return estimate_memory_from_metadata(
        meta,
        context_length,
        bytes_per_element=bytes_per_element,
        safety_margin=safety_margin,
    )


class SystemResourceDetector(ResourceBudget):
    def __init__(self, metadata_loader: Optional[Callable[[str], GGUFMetadata]] = None):
        self._metadata_loader = metadata_loader

    def get_system_resources(self) -> SystemResources:
        resources = SystemResources()
        system = platform.system()

        if system == "Windows":
            resources = self._populate_windows(resources)
        elif system == "Linux":
            resources = self._populate_linux(resources)
        elif system == "Darwin":
            resources = self._populate_macos(resources)

        # Make sure bytes-per-token plumbing exists even when nvidia-smi is
        # missing, so can_load never compares against unbounded RAM.
        if resources.available_vram_bytes <= 0:
            resources.available_vram_bytes = 0
        return resources

    def _populate_windows(self, resources: SystemResources) -> SystemResources:
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("dwTotalPhys", ctypes.c_ulonglong),
                    ("dwAvailPhys", ctypes.c_ulonglong),
                    ("dwTotalPageFile", ctypes.c_ulonglong),
                    ("dwAvailPageFile", ctypes.c_ulonglong),
                    ("dwTotalVirtual", ctypes.c_ulonglong),
                    ("dwAvailVirtual", ctypes.c_ulonglong),
                    ("dwAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            resources.total_ram_bytes = stat.dwTotalPhys
            resources.available_ram_bytes = stat.dwAvailPhys
        except Exception:
            pass

        gpu_name = self._detect_nvidia_gpu_windows()
        if gpu_name:
            resources.gpu_name = gpu_name
            total = self._get_nvidia_vram_windows()
            free = self._get_nvidia_vram_free_windows()
            if total is not None:
                resources.total_vram_bytes = total
            if free is not None:
                resources.available_vram_bytes = free

        return resources

    def _populate_linux(self, resources: SystemResources) -> SystemResources:
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        resources.total_ram_bytes = int(line.split()[1]) * 1024
                    elif line.startswith("MemAvailable:"):
                        resources.available_ram_bytes = int(line.split()[1]) * 1024
        except Exception:
            pass

        gpu_name = self._detect_nvidia_gpu_linux()
        if gpu_name:
            resources.gpu_name = gpu_name
            total = self._get_nvidia_vram_linux()
            free = self._get_nvidia_vram_free_linux()
            if total is not None:
                resources.total_vram_bytes = total
            if free is not None:
                resources.available_vram_bytes = free

        return resources

    def _populate_macos(self, resources: SystemResources) -> SystemResources:
        try:
            import subprocess
            result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
            resources.total_ram_bytes = int(result.stdout.strip())
        except Exception:
            pass
        return resources

    def _detect_nvidia_gpu_windows(self) -> Optional[str]:
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=3.0,
            )
            if result.returncode == 0:
                return result.stdout.strip().split("\n")[0]
        except Exception:
            pass
        return None

    def _detect_nvidia_gpu_linux(self) -> Optional[str]:
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=3.0,
            )
            if result.returncode == 0:
                return result.stdout.strip().split("\n")[0]
        except Exception:
            pass
        return None

    def _get_nvidia_vram_windows(self) -> Optional[int]:
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3.0,
            )
            if result.returncode == 0:
                return int(result.stdout.strip().split("\n")[0]) * 1024 * 1024
        except Exception:
            pass
        return None

    def _get_nvidia_vram_free_windows(self) -> Optional[int]:
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3.0,
            )
            if result.returncode == 0:
                return int(result.stdout.strip().split("\n")[0]) * 1024 * 1024
        except Exception:
            pass
        return None

    def _get_nvidia_vram_linux(self) -> Optional[int]:
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3.0,
            )
            if result.returncode == 0:
                return int(result.stdout.strip().split("\n")[0]) * 1024 * 1024
        except Exception:
            pass
        return None

    def _get_nvidia_vram_free_linux(self) -> Optional[int]:
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3.0,
            )
            if result.returncode == 0:
                return int(result.stdout.strip().split("\n")[0]) * 1024 * 1024
        except Exception:
            pass
        return None

    def estimate_model_memory(self, model_size_bytes: int, context_length: int) -> int:
        # Backwards-compatible overload when no GGUF path is available.
        # Estimate KV at ~150 KiB / token, which is correct for typical 4-7B
        # class GGUF chat models with fp16 KV cache and GQA.
        per_token = int(model_size_bytes // 100_000) if model_size_bytes > 0 else 0
        if per_token < 64 * 1024:
            per_token = 64 * 1024
        if per_token > 4 * 1024 * 1024:
            per_token = 4 * 1024 * 1024
        kv = per_token * max(0, context_length)
        overhead = 256 * 1024 * 1024
        return int((model_size_bytes + kv + overhead) * SAFETY_MARGIN_DEFAULT)

    def can_load(self, model_size_bytes: int, context_length: int) -> bool:
        resources = self.get_system_resources()
        estimated = self.estimate_model_memory(model_size_bytes, context_length)
        # Prefer holding the workload in VRAM when there is a discrete GPU;
        # only spill into RAM when VRAM is missing or exhausted.
        if resources.gpu_name and resources.available_vram_bytes > 0:
            budget = resources.available_vram_bytes
            spill = max(resources.available_ram_bytes, 0) // 2
            return estimated <= budget + spill
        ram = resources.available_ram_bytes or resources.total_ram_bytes
        return estimated <= ram


def select_context_size(
    *,
    requested: int,
    filesize_bytes: int,
    free_vram_bytes: int,
    free_ram_bytes: int,
    has_gpu: bool,
    sequence: Sequence[int] = DEFAULT_STEPDOWN_SEQUENCE,
    metadata_loader: Optional[Callable[[str], GGUFMetadata]] = None,
    bytes_per_element: int = KV_CACHE_BYTES_DEFAULT,
    safety_margin: float = SAFETY_MARGIN_DEFAULT,
    path: Optional[str] = None,
) -> dict:
    """Find the largest context size that fits the system budget.

    Returns a dict with keys:
        requested: the originally requested size
        chosen:    the size that fits (or 0 when nothing fits)
        reason:    'fits-as-requested' | 'stepped-down: dropped from N to M' | 'no-fit'
        estimate_bytes: estimate for the chosen size
        stepdown_log:   list of (size, estimated_bytes, fits?) tuples
    """
    candidates: List[int] = []
    seen: set[int] = set()
    for n in sequence:
        if requested >= n > 0 and n not in seen:
            candidates.append(n)
            seen.add(n)
    if requested > 0 and requested not in seen:
        candidates.append(requested)
        seen.add(requested)

    if path is not None:
        try:
            meta = (metadata_loader or read_metadata)(path)
            estimate_for = lambda ctx: estimate_memory_from_metadata(
                meta, ctx,
                bytes_per_element=bytes_per_element,
                safety_margin=safety_margin,
            ).total_bytes
        except Exception as exc:
            logger.warning("Failed to read GGUF metadata for %s: %s", path, exc)
            meta = None
            estimate_for = lambda ctx: SystemResourceDetector().estimate_model_memory(
                filesize_bytes, ctx,
            )
    else:
        meta = None
        detector = SystemResourceDetector(metadata_loader=metadata_loader)
        estimate_for = lambda ctx: detector.estimate_model_memory(filesize_bytes, ctx)

    budget = free_vram_bytes if has_gpu and free_vram_bytes > 0 else 0
    headroom = budget + (free_ram_bytes // 2 if free_ram_bytes > 0 else 0)
    log_entries: List[dict] = []

    result: dict = {
        "requested": requested,
        "chosen": 0,
        "reason": "no-fit",
        "estimate_bytes": 0,
        "stepdown_log": log_entries,
    }

    for size in candidates:
        est = estimate_for(size)
        fits = est <= headroom
        log_entries.append({"size": size, "estimated_bytes": int(est), "fits": bool(fits)})
        if fits:
            result["chosen"] = size
            result["reason"] = (
                "fits-as-requested" if size == requested else f"stepped-down: dropped from {requested} to {size}"
            )
            result["estimate_bytes"] = int(est)
            return result

    result["reason"] = "no-fit"
    return result


@dataclass(frozen=True)
class LoadEstimate:
    """Memory breakdown for one concrete load configuration."""

    context_length: int
    gpu_layers: int
    layer_count: int
    cpu_moe: bool
    weights_gpu_bytes: int
    weights_cpu_bytes: int
    kv_cache_bytes: int
    kv_gpu_bytes: int
    compute_buffer_bytes: int
    vram_overhead_bytes: int
    ram_overhead_bytes: int
    vram_margin_bytes: int
    ram_margin_bytes: int
    model_file_bytes: int = 0
    recurrent_state_bytes: int = 0
    recurrent_gpu_bytes: int = 0

    @property
    def kv_cpu_bytes(self) -> int:
        return max(0, self.kv_cache_bytes - self.kv_gpu_bytes)

    @property
    def recurrent_cpu_bytes(self) -> int:
        return max(0, self.recurrent_state_bytes - self.recurrent_gpu_bytes)

    @property
    def vram_total_bytes(self) -> int:
        return (
            self.weights_gpu_bytes
            + self.kv_gpu_bytes
            + self.recurrent_gpu_bytes
            + self.compute_buffer_bytes
            + self.vram_overhead_bytes
            + self.vram_margin_bytes
        )

    @property
    def ram_total_bytes(self) -> int:
        return self.ram_demand_bytes + self.ram_margin_bytes

    @property
    def vram_demand_bytes(self) -> int:
        """VRAM actually consumed (no margin)."""
        return (
            self.weights_gpu_bytes
            + self.kv_gpu_bytes
            + self.recurrent_gpu_bytes
            + self.compute_buffer_bytes
            + self.vram_overhead_bytes
        )

    @property
    def ram_demand_bytes(self) -> int:
        """RAM actually consumed (no margin).

        llama-server mmaps the whole model file, so private bytes track the
        file size (measured: 27B private 16841 MiB vs file 16032 MiB; 30B
        15177 vs 13907) regardless of how many layers are offloaded. The
        mmap'd file is therefore counted once here instead of the CPU-resident
        weights, which are a subset of it.
        """
        return (
            self.model_file_bytes
            + self.kv_cpu_bytes
            + self.recurrent_cpu_bytes
            + self.ram_overhead_bytes
        )

    def fits(self, free_vram_bytes: int, free_ram_bytes: int) -> bool:
        if self.vram_total_bytes > free_vram_bytes:
            return False
        return self.ram_total_bytes <= free_ram_bytes

    def components(self) -> dict:
        return {
            "model_file_gib": round(self.model_file_bytes / GIB, 3),
            "weights_gpu_gib": round(self.weights_gpu_bytes / GIB, 3),
            "weights_cpu_gib": round(self.weights_cpu_bytes / GIB, 3),
            "kv_cache_gib": round(self.kv_cache_bytes / GIB, 3),
            "kv_gpu_gib": round(self.kv_gpu_bytes / GIB, 3),
            "kv_cpu_gib": round(self.kv_cpu_bytes / GIB, 3),
            "recurrent_state_gib": round(self.recurrent_state_bytes / GIB, 3),
            "recurrent_gpu_gib": round(self.recurrent_gpu_bytes / GIB, 3),
            "recurrent_cpu_gib": round(self.recurrent_cpu_bytes / GIB, 3),
            "compute_buffer_gib": round(self.compute_buffer_bytes / GIB, 3),
            "vram_overhead_gib": round(self.vram_overhead_bytes / GIB, 3),
            "ram_overhead_gib": round(self.ram_overhead_bytes / GIB, 3),
            "vram_margin_gib": round(self.vram_margin_bytes / GIB, 3),
            "ram_margin_gib": round(self.ram_margin_bytes / GIB, 3),
            "vram_total_gib": round(self.vram_total_bytes / GIB, 3),
            "ram_total_gib": round(self.ram_total_bytes / GIB, 3),
            "vram_demand_gib": round(self.vram_demand_bytes / GIB, 3),
            "ram_demand_gib": round(self.ram_demand_bytes / GIB, 3),
        }


def _kv_total_bytes(meta: GGUFMetadata, context_length: int, bytes_per_element: int) -> int:
    kv_layers = int(meta.kv_block_count or 0) or int(meta.block_count or 0)
    if kv_layers and meta.attention_head_count_kv and meta.head_dim:
        return (
            2
            * kv_layers
            * int(meta.attention_head_count_kv)
            * int(meta.head_dim)
            * max(0, context_length)
            * bytes_per_element
        )
    per_token = int(meta.file_size_bytes // 100_000) if meta.file_size_bytes else 0
    per_token = max(64 * 1024, min(4 * 1024 * 1024, per_token))
    return per_token * max(0, context_length)


def _recurrent_state_bytes(meta: GGUFMetadata, cells: int = RECURRENT_CELLS_DEFAULT) -> int:
    """Recurrent (SSM / linear-attention) state size for hybrid models.

    Blocks with an attention KV tensor keep a KV cache (sized by context); the
    remaining blocks keep a constant-size recurrent state (a convolution state
    plus an SSM state) that does not grow with context. llama.cpp allocates one
    such state per sequence slot (the "RS buffer"). Pure-attention models
    (kv_block_count == block_count) report zero.
    """
    block_count = int(meta.block_count or 0)
    kv_blocks = int(meta.kv_block_count or 0)
    if block_count <= 0 or kv_blocks <= 0 or kv_blocks >= block_count:
        return 0
    inner = int(meta.ssm_inner_size or 0)
    state = int(meta.ssm_state_size or 0)
    if inner <= 0 or state <= 0:
        return 0
    nextn = int(meta.nextn_predict_layers or 0)
    recurrent_layers = max(0, block_count - nextn - kv_blocks)
    # f32 recurrent/convolution state, as llama.cpp allocates it.
    state_bytes = inner * state * 4
    conv_bytes = int(meta.ssm_conv_kernel or 0) * inner * 4
    return recurrent_layers * (state_bytes + conv_bytes) * max(1, cells)


def estimate_load(
    meta: GGUFMetadata,
    *,
    gpu_layers: int,
    cpu_moe: bool,
    context_length: int,
    bytes_per_element: int = KV_CACHE_BYTES_DEFAULT,
) -> LoadEstimate:
    """Estimate VRAM/RAM for one load configuration from the GGUF tensor table.

    `gpu_layers` is llama.cpp's --n-gpu-layers value (which counts the
    embedding pseudo-layer): block layers on the GPU = gpu_layers - 1, capped
    at the block count. Expert tensors and the rest are counted separately;
    `output.weight` stays on the CPU; KV cache follows layer placement
    proportionally and is derived from the KV-bearing block count, KV heads
    and head size at the requested context and cache type. A 256 MiB compute
    buffer allowance, a ~1 GiB VRAM margin and a 4 GiB RAM margin are added.
    """
    layer_count = int(meta.block_count or 0)
    has_split = (
        meta.expert_tensor_bytes is not None
        and meta.other_tensor_bytes is not None
        and meta.layer_tensor_bytes is not None
        and meta.nonlayer_tensor_bytes is not None
    )
    if has_split:
        expert = int(meta.expert_tensor_bytes or 0)
        layer = int(meta.layer_tensor_bytes or 0)
        nonlayer = int(meta.nonlayer_tensor_bytes or 0)
        output_bytes = int(meta.output_tensor_bytes or 0)
    else:
        expert = 0
        layer = int(meta.file_size_bytes)
        nonlayer = 0
        output_bytes = 0
    non_expert_layer = max(0, layer - expert)

    ngl = max(0, int(gpu_layers))
    blk_gpu = min(max(ngl - 1, 0), layer_count) if layer_count > 0 else 0
    frac = (blk_gpu / layer_count) if layer_count > 0 else 0.0

    gpu_eligible_nonlayer = max(0, nonlayer - output_bytes)
    layer_gpu_eligible = non_expert_layer if cpu_moe else layer
    weights_gpu = int((gpu_eligible_nonlayer if ngl > 0 else 0) + layer_gpu_eligible * frac)
    # Expert tensors are a subset of the layer tensors; the CPU side is simply
    # everything that is not on the GPU.
    weights_cpu = int(layer + nonlayer - weights_gpu)

    kv_total = _kv_total_bytes(meta, context_length, bytes_per_element)
    kv_gpu = int(kv_total * frac) if ngl > 0 else 0
    recurrent_total = _recurrent_state_bytes(meta)
    recurrent_gpu = int(recurrent_total * frac) if ngl > 0 else 0
    compute = 0
    if weights_gpu > 0:
        per_token = COMPUTE_BYTES_PER_TOKEN_MOE if (has_split and expert > 0) else COMPUTE_BYTES_PER_TOKEN_DENSE
        compute = context_length * per_token
    return LoadEstimate(
        context_length=context_length,
        gpu_layers=ngl,
        layer_count=layer_count,
        cpu_moe=bool(cpu_moe),
        weights_gpu_bytes=weights_gpu,
        weights_cpu_bytes=weights_cpu,
        kv_cache_bytes=kv_total,
        kv_gpu_bytes=kv_gpu,
        recurrent_state_bytes=recurrent_total,
        recurrent_gpu_bytes=recurrent_gpu,
        compute_buffer_bytes=compute,
        vram_overhead_bytes=VRAM_OVERHEAD_BYTES,
        ram_overhead_bytes=RAM_OVERHEAD_BYTES,
        vram_margin_bytes=VRAM_MARGIN_BYTES,
        ram_margin_bytes=RAM_MARGIN_BYTES,
        model_file_bytes=int(meta.file_size_bytes or 0),
    )


def plan_model_load(
    meta: GGUFMetadata,
    *,
    free_vram_bytes: int,
    free_ram_bytes: int,
    requested_ctx: int,
    has_gpu: bool = True,
    bytes_per_element: int = KV_CACHE_BYTES_DEFAULT,
    stepdown_sequence: Sequence[int] = DEFAULT_STEPDOWN_SEQUENCE,
    max_gpu_layers: Optional[int] = None,
) -> dict:
    """Choose load settings: all GPU layers, else experts on CPU, else fewer
    GPU layers, else a smaller context; refuse clearly when nothing fits.

    Returns a dict with keys: fits, reason, step, n_ctx, gpu_layers, cpu_moe,
    layer_count, requested_ctx, estimate (component breakdown).
    """
    layer_count = int(meta.block_count or 0)
    if layer_count <= 0:
        return {
            "fits": False,
            "reason": "refused: GGUF layer count missing; cannot plan a load",
            "step": "refused",
            "n_ctx": 0,
            "gpu_layers": 0,
            "cpu_moe": False,
            "layer_count": 0,
            "requested_ctx": requested_ctx,
            "estimate": {},
        }
    # llama.cpp counts the embedding pseudo-layer: "all layers" = blocks + 1.
    ngl_all = layer_count + 1
    cap = ngl_all if max_gpu_layers is None else max(1, min(int(max_gpu_layers), ngl_all))
    is_moe = bool(getattr(meta, "is_moe", False)) or (meta.expert_tensor_bytes or 0) > 0

    ctxs: List[int] = [requested_ctx]
    for n in stepdown_sequence:
        if n < requested_ctx and n not in ctxs:
            ctxs.append(n)

    best_reject: Optional[dict] = None

    def _fits(est: LoadEstimate) -> bool:
        if not has_gpu:
            ram = est.ram_total_bytes
            if platform.system() == "Windows":
                ram += est.vram_demand_bytes
            return ram <= free_ram_bytes
        return est.fits(free_vram_bytes, free_ram_bytes)

    for ctx in ctxs:
        # Step 1: all layers on the GPU.
        est = estimate_load(meta, gpu_layers=cap, cpu_moe=False, context_length=ctx,
                            bytes_per_element=bytes_per_element)
        if _fits(est):
            step = "all-gpu-layers" if cap == ngl_all else "requested-gpu-layers"
            return _plan(True, f"{step} fit at n_ctx={ctx}", step, ctx, cap, False,
                         layer_count, requested_ctx, est)
        best_reject = {"ctx": ctx, "step": "all-gpu-layers", "est": est}

        # Step 2: experts on the CPU (MoE only).
        if is_moe:
            est = estimate_load(meta, gpu_layers=cap, cpu_moe=True, context_length=ctx,
                                bytes_per_element=bytes_per_element)
            if _fits(est):
                return _plan(True, f"experts-on-cpu fit at n_ctx={ctx}", "experts-on-cpu",
                             ctx, cap, True, layer_count, requested_ctx, est)
            best_reject = {"ctx": ctx, "step": "experts-on-cpu", "est": est}

        # Step 3: fewer GPU layers (largest that fits).
        est = estimate_load(meta, gpu_layers=cap, cpu_moe=False, context_length=ctx,
                            bytes_per_element=bytes_per_element)
        for n in range(cap - 1, 0, -1):
            est = estimate_load(meta, gpu_layers=n, cpu_moe=False, context_length=ctx,
                                bytes_per_element=bytes_per_element)
            if _fits(est):
                return _plan(True, f"fewer-gpu-layers (n_gpu_layers={n} of {ngl_all}) fit at n_ctx={ctx}",
                             "fewer-gpu-layers", ctx, n, False, layer_count, requested_ctx, est)
        # remember the smallest-ctx rejection for the refusal message
        best_reject = {"ctx": ctx, "step": "fewer-gpu-layers", "est": est}

    est = best_reject["est"] if best_reject else estimate_load(
        meta, gpu_layers=cap, cpu_moe=False, context_length=requested_ctx,
        bytes_per_element=bytes_per_element)
    ram_need = est.ram_total_bytes
    if platform.system() == "Windows":
        ram_need += est.vram_demand_bytes
    reason = (
        "refused: no configuration fits at n_ctx>={min_ctx}: best attempt needs "
        "{vram:.2f} GiB VRAM / {ram:.2f} GiB RAM but only {fv:.2f} GiB VRAM / {fr:.2f} GiB RAM are free"
    ).format(
        min_ctx=ctxs[-1],
        vram=est.vram_total_bytes / GIB,
        ram=ram_need / GIB,
        fv=free_vram_bytes / GIB,
        fr=free_ram_bytes / GIB,
    )
    return _plan(False, reason, "refused", 0, 0, False, layer_count, requested_ctx, est)


def _plan(fits: bool, reason: str, step: str, n_ctx: int, gpu_layers: int, cpu_moe: bool,
          layer_count: int, requested_ctx: int, est: LoadEstimate) -> dict:
    return {
        "fits": fits,
        "reason": reason,
        "step": step,
        "n_ctx": n_ctx,
        "gpu_layers": gpu_layers,
        "cpu_moe": cpu_moe,
        "layer_count": layer_count,
        "requested_ctx": requested_ctx,
        "estimate": est.components(),
    }


class DefaultResourceBudget(SystemResourceDetector):
    """Production detector. Defers to SystemResourceDetector."""


__all__ = [
    "SystemResourceDetector",
    "DefaultResourceBudget",
    "MemoryEstimate",
    "LoadEstimate",
    "estimate_load",
    "plan_model_load",
    "estimate_memory_from_metadata",
    "estimate_memory_from_path",
    "select_context_size",
    "DEFAULT_STEPDOWN_SEQUENCE",
    "KV_CACHE_BYTES_DEFAULT",
    "SAFETY_MARGIN_DEFAULT",
    "VRAM_MARGIN_BYTES",
    "RAM_MARGIN_BYTES",
]
