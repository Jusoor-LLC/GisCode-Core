"""Focused tests for the tensor-table based load planner.

These build tiny synthetic GGUF files (header + metadata KVs + tensor info
table + padded data) and drive ``plan_model_load`` / ``estimate_load`` with
fake free-memory values, covering:
- a dense 27B-like model (all GPU layers fit)
- a MoE 30B-like model (experts-on-CPU step is chosen when all-layers fails)
- a small 4B-like model (full offload trivially fits)
- a refusal case with only ~4 GiB free RAM
- expert/other/layer byte accounting from the tensor table
- KV block counting (including nextn draft-block exclusion)

Tensor sizes use small synthetic byte counts (the parser derives per-tensor
sizes from consecutive offsets, so absolute magnitudes do not matter).
"""
import struct

from giscode.inference.gguf import read_metadata
from giscode.inference.resource_budget import (
    GIB,
    estimate_load,
    plan_model_load,
)


def _kv(key: str, typ: int, body: bytes) -> bytes:
    raw = key.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw + struct.pack("<I", typ) + body


def _s(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def write_gguf(
    path: str,
    *,
    arch: str = "qwen3moe",
    block_count: int = 2,
    head_count: int = 4,
    head_count_kv: int = 2,
    key_length: int = 128,
    expert_count: int | None = None,
    expert_used_count: int | None = None,
    tensors: list[tuple[str, int]] | None = None,
) -> str:
    """Write a minimal but structurally valid GGUF with a tensor table."""
    kvs = [
        _kv("general.architecture", 8, _s(arch)),
        _kv("general.name", 8, _s("synthetic")),
        _kv(f"{arch}.block_count", 5, _u32(block_count)),
        _kv(f"{arch}.attention.head_count", 5, _u32(head_count)),
        _kv(f"{arch}.attention.head_count_kv", 5, _u32(head_count_kv)),
        _kv(f"{arch}.attention.key_length", 5, _u32(key_length)),
        _kv(f"{arch}.attention.value_length", 5, _u32(key_length)),
    ]
    if expert_count is not None:
        kvs.append(_kv(f"{arch}.expert_count", 5, _u32(expert_count)))
    if expert_used_count is not None:
        kvs.append(_kv(f"{arch}.expert_used_count", 5, _u32(expert_used_count)))
    if tensors is None:
        tensors = []
    header = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", len(tensors)) + struct.pack(
        "<Q", len(kvs)
    )
    meta = b"".join(kvs)
    entries = []
    offset = 0
    for name, size in tensors:
        raw_name = name.encode("utf-8")
        entries.append(
            struct.pack("<Q", len(raw_name))
            + raw_name
            + struct.pack("<I", 2)
            + struct.pack("<Q", 1)
            + struct.pack("<Q", max(int(size), 1))
            + struct.pack("<I", 1)
            + struct.pack("<Q", offset)
        )
        offset += int(size)
    prefix_len = len(header) + len(meta) + sum(len(e) for e in entries)
    data_start = (prefix_len + 31) // 32 * 32
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(meta)
        for entry in entries:
            handle.write(entry)
        handle.write(b"\x00" * (data_start - prefix_len))
        handle.truncate(data_start + offset)
    return path


def _moe_tensors(block_count: int) -> list[tuple[str, int]]:
    tensors = [("token_embd.weight", 5120), ("output.weight", 2560)]
    for i in range(block_count):
        tensors.append((f"blk.{i}.attn_k.weight", 320))
        tensors.append((f"blk.{i}.attn_q.weight", 320))
        tensors.append((f"blk.{i}.ffn_gate_exps.weight", 2560))
        tensors.append((f"blk.{i}.ffn_down_exps.weight", 2560))
        tensors.append((f"blk.{i}.ffn_up_exps.weight", 2560))
    return tensors


def _dense_tensors(block_count: int) -> list[tuple[str, int]]:
    tensors = [("token_embd.weight", 2560), ("output.weight", 1280)]
    for i in range(block_count):
        tensors.append((f"blk.{i}.attn_k.weight", 640))
        tensors.append((f"blk.{i}.attn_q.weight", 640))
        tensors.append((f"blk.{i}.ffn_gate.weight", 640))
        tensors.append((f"blk.{i}.ffn_down.weight", 640))
        tensors.append((f"blk.{i}.ffn_up.weight", 640))
    return tensors


def test_dense_27b_style_full_offload_fits(tmp_path):
    path = str(tmp_path / "dense.gguf")
    write_gguf(path, arch="qwen35", block_count=65, head_count_kv=4, key_length=256,
               tensors=_dense_tensors(65))
    meta = read_metadata(path)
    plan = plan_model_load(meta, requested_ctx=16384,
                           free_vram_bytes=16 * GIB, free_ram_bytes=32 * GIB, has_gpu=True)
    assert plan["fits"] is True
    assert plan["step"] == "all-gpu-layers"
    assert plan["gpu_layers"] == 66  # blocks + embedding pseudo-layer
    assert plan["cpu_moe"] is False
    assert plan["n_ctx"] == 16384


def test_moe_30b_style_uses_experts_on_cpu_when_all_layers_fail(tmp_path, monkeypatch):
    import giscode.inference.resource_budget as rb

    monkeypatch.setattr(rb, "VRAM_MARGIN_BYTES", 0)
    monkeypatch.setattr(rb, "RAM_MARGIN_BYTES", 0)
    monkeypatch.setattr(rb, "VRAM_OVERHEAD_BYTES", 0)
    monkeypatch.setattr(rb, "RAM_OVERHEAD_BYTES", 0)
    monkeypatch.setattr(rb, "COMPUTE_BYTES_PER_TOKEN_DENSE", 0)
    monkeypatch.setattr(rb, "COMPUTE_BYTES_PER_TOKEN_MOE", 0)
    path = str(tmp_path / "moe.gguf")
    write_gguf(path, arch="qwen3moe", block_count=48, head_count_kv=4,
               expert_count=128, expert_used_count=8, tensors=_moe_tensors(48))
    meta = read_metadata(path)
    assert meta.is_moe is True
    assert meta.kv_block_count == 48
    est_all = estimate_load(meta, gpu_layers=49, cpu_moe=False, context_length=16384)
    est_cmoe = estimate_load(meta, gpu_layers=49, cpu_moe=True, context_length=16384)
    assert est_cmoe.vram_demand_bytes < est_all.vram_demand_bytes
    # VRAM budget that only the experts-on-CPU placement can satisfy
    free_vram = est_cmoe.vram_demand_bytes + (
        est_all.vram_demand_bytes - est_cmoe.vram_demand_bytes
    ) // 2
    plan = plan_model_load(meta, requested_ctx=16384, free_vram_bytes=free_vram,
                           free_ram_bytes=64 * GIB, has_gpu=True)
    assert plan["fits"] is True
    assert plan["step"] == "experts-on-cpu"
    assert plan["cpu_moe"] is True
    assert plan["gpu_layers"] == 49
    # experts keep most weights on the CPU side (bytes, not rounded GiB)
    assert est_cmoe.weights_cpu_bytes > est_cmoe.weights_gpu_bytes


def test_small_4b_style_full_offload_fits_with_little_ram(tmp_path):
    path = str(tmp_path / "small.gguf")
    tensors = [("token_embd.weight", 2560), ("output.weight", 2560),
               ("blk.0.attn_k.weight", 640), ("blk.0.ffn_down.weight", 640)]
    write_gguf(path, arch="qwen3vl", block_count=1, tensors=tensors)
    meta = read_metadata(path)
    plan = plan_model_load(meta, requested_ctx=4096,
                           free_vram_bytes=8 * GIB, free_ram_bytes=8 * GIB, has_gpu=True)
    assert plan["fits"] is True
    assert plan["step"] == "all-gpu-layers"


def test_refusal_when_free_ram_is_only_4_gib(tmp_path):
    path = str(tmp_path / "dense2.gguf")
    write_gguf(path, arch="qwen35", block_count=65, head_count_kv=4, key_length=256,
               tensors=_dense_tensors(65))
    meta = read_metadata(path)
    plan = plan_model_load(meta, requested_ctx=16384,
                           free_vram_bytes=16 * GIB, free_ram_bytes=4 * GIB, has_gpu=True)
    assert plan["fits"] is False
    assert plan["step"] == "refused"
    assert "refused" in plan["reason"]


def test_estimate_load_excludes_output_weight_from_gpu_side(tmp_path):
    path = str(tmp_path / "dense3.gguf")
    write_gguf(path, arch="qwen35", block_count=4, head_count_kv=2, key_length=128,
               tensors=_dense_tensors(4))
    meta = read_metadata(path)
    est = estimate_load(meta, gpu_layers=5, cpu_moe=False, context_length=2048)
    # token_embd (2560) goes to the GPU; output.weight (1280) stays on CPU
    assert est.weights_gpu_bytes == 2560 + 4 * 5 * 640
    assert est.weights_cpu_bytes == 1280
    assert est.kv_gpu_bytes == est.kv_cache_bytes  # all layers on GPU
    assert est.vram_demand_bytes > 0 and est.ram_demand_bytes > 0


def test_estimate_load_cpu_moe_keeps_experts_off_gpu(tmp_path):
    path = str(tmp_path / "moe2.gguf")
    write_gguf(path, arch="qwen3moe", block_count=4, head_count_kv=2,
               expert_count=8, expert_used_count=2, tensors=_moe_tensors(4))
    meta = read_metadata(path)
    est_gpu = estimate_load(meta, gpu_layers=5, cpu_moe=False, context_length=2048)
    est_cpu = estimate_load(meta, gpu_layers=5, cpu_moe=True, context_length=2048)
    assert est_cpu.weights_gpu_bytes < est_gpu.weights_gpu_bytes
    assert est_cpu.weights_cpu_bytes > est_gpu.weights_cpu_bytes
    expert_bytes = 4 * 3 * 2560
    assert expert_bytes <= est_cpu.weights_cpu_bytes


def test_nextn_tensors_excluded_and_draft_kv_block_dropped(tmp_path):
    path = str(tmp_path / "nextn.gguf")
    tensors = [
        ("token_embd.weight", 1280),
        ("output.weight", 640),
        ("blk.0.attn_k.weight", 320),
        ("blk.1.attn_k.weight", 320),
        ("blk.2.attn_k.weight", 320),
        ("blk.2.nextn.ffn_gate.weight", 160),
    ]
    write_gguf(path, arch="qwen35", block_count=3, head_count_kv=2, tensors=tensors)
    meta = read_metadata(path)
    assert meta.nextn_tensor_bytes == 160
    assert meta.kv_block_count == 2  # blk.2 dropped as draft/nextn block
    est = estimate_load(meta, gpu_layers=4, cpu_moe=False, context_length=2048)
    # nextn bytes are not counted anywhere (neither GPU nor CPU)
    assert est.weights_gpu_bytes + est.weights_cpu_bytes == 1280 + 640 + 3 * 320
