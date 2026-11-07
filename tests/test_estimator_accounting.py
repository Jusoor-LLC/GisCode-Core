"""Estimator accounting: GPU weights count only against VRAM."""

from __future__ import annotations

from giscode.inference.gguf import GGUFMetadata
from giscode.inference.resource_budget import GIB, estimate_load, plan_model_load

GIBB = 1024 ** 3


def _meta(**kw):
    defaults = dict(
        path="fake.gguf",
        file_size_bytes=int(15.66 * GIBB),
        gguf_version=3,
        block_count=65,
        attention_head_count=8,
        attention_head_count_kv=8,
        attention_key_length=128,
        embedding_length=5120,
        layer_tensor_bytes=int(15.66 * GIBB),
        expert_tensor_bytes=None,
        other_tensor_bytes=None,
        nonlayer_tensor_bytes=None,
        output_tensor_bytes=None,
        kv_block_count=65,
        expert_count=None,
    )
    defaults.update(kw)
    return GGUFMetadata(**defaults)


def test_27b_fits_at_203_gib_free_ram():
    meta = _meta()
    plan = plan_model_load(
        meta, free_vram_bytes=int(14.6 * GIBB), free_ram_bytes=int(20.3 * GIBB),
        requested_ctx=32768, has_gpu=True,
    )
    assert plan["fits"] is True
    assert plan["gpu_layers"] < 66
    assert plan["n_ctx"] <= 32768


def test_27b_refused_when_ram_below_file_plus_floor():
    """RAM below the mmap'd file plus the 4 GiB floor is a genuine refusal.

    llama-server mmaps the whole model file, so even the smallest context
    needs file_size + 4 GiB. At 14 GiB free the 27B (15.66 GiB file) can
    never fit; at 21 GiB it fits.
    """
    meta = _meta()
    refused = plan_model_load(
        meta, free_vram_bytes=int(14.6 * GIBB), free_ram_bytes=int(14.0 * GIBB),
        requested_ctx=32768, has_gpu=True,
    )
    assert refused["fits"] is False
    assert "no configuration" in refused["reason"].lower()
    assert "gib" in refused["reason"].lower()

    fits = plan_model_load(
        meta, free_vram_bytes=int(14.6 * GIBB), free_ram_bytes=int(21.0 * GIBB),
        requested_ctx=32768, has_gpu=True,
    )
    assert fits["fits"] is True
    assert fits["n_ctx"] <= 32768


def test_4b_fits_easily():
    meta = _meta(
        block_count=36,
        file_size_bytes=int(2.33 * GIBB),
        layer_tensor_bytes=int(2.33 * GIBB),
        kv_block_count=36,
    )
    plan = plan_model_load(
        meta, free_vram_bytes=int(14.6 * GIBB), free_ram_bytes=int(20.3 * GIBB),
        requested_ctx=16384, has_gpu=True,
    )
    assert plan["fits"] is True
    assert plan["gpu_layers"] == 37


def test_moe_experts_on_cpu_counted():
    meta = _meta(
        expert_tensor_bytes=int(5.0 * GIBB),
        other_tensor_bytes=int(0.5 * GIBB),
        nonlayer_tensor_bytes=int(0.2 * GIBB),
        output_tensor_bytes=int(0.5 * GIBB),
        expert_count=128,
    )
    est = estimate_load(meta, gpu_layers=56, cpu_moe=True, context_length=16384)
    # cpu_moe: non-expert layers go on the GPU, experts stay on the CPU.
    assert est.weights_cpu_bytes > 0
    assert est.weights_gpu_bytes > 0
    assert est.weights_cpu_bytes < est.weights_gpu_bytes
    assert est.kv_cache_bytes > 0
    # The 5 GiB of expert tensors must land on the CPU side.
    assert est.weights_cpu_bytes >= int(5.0 * GIBB)


def _hybrid_meta(**kw):
    """A Qwen3.5-style hybrid: 16 attention layers + 48 recurrent layers."""
    defaults = dict(
        block_count=65,
        kv_block_count=16,
        nextn_predict_layers=1,
        ssm_state_size=128,
        ssm_inner_size=6144,
        ssm_conv_kernel=4,
        ssm_group_count=16,
        attention_head_count_kv=4,
    )
    defaults.update(kw)
    return _meta(**defaults)


def test_hybrid_recurrent_state_counted():
    """Recurrent (SSM) layers add a state buffer independent of context."""
    meta = _hybrid_meta()
    est = estimate_load(meta, gpu_layers=52, cpu_moe=False, context_length=32768)
    # 48 recurrent layers * (6144*128*4 + 4*6144*4) bytes * 4 cells.
    expected = 48 * (6144 * 128 * 4 + 4 * 6144 * 4) * 4
    assert est.recurrent_state_bytes == expected
    assert est.recurrent_gpu_bytes > 0
    # The recurrent state is part of the demand and of the margin-inclusive total.
    assert est.vram_demand_bytes >= est.recurrent_gpu_bytes
    comp = est.components()
    assert comp["recurrent_state_gib"] > 0.5
    assert comp["vram_demand_gib"] >= comp["recurrent_gpu_gib"]


def test_pure_attention_has_no_recurrent_state():
    meta = _meta()  # kv_block_count == block_count
    est = estimate_load(meta, gpu_layers=52, cpu_moe=False, context_length=32768)
    assert est.recurrent_state_bytes == 0
    assert est.recurrent_gpu_bytes == 0


def test_compute_buffer_scales_with_context():
    """Measured compute buffers: ~9 KiB/token dense, ~25 KiB/token MoE."""
    meta = _meta(block_count=36, file_size_bytes=int(2.33 * GIBB),
                 layer_tensor_bytes=int(2.33 * GIBB), kv_block_count=36)
    est = estimate_load(meta, gpu_layers=37, cpu_moe=False, context_length=16384)
    # 16384 * 9 KiB = 144 MiB (measured 144.04 on the 4B).
    assert est.compute_buffer_bytes == 16384 * 9 * 1024
    est2 = estimate_load(meta, gpu_layers=37, cpu_moe=False, context_length=32768)
    assert est2.compute_buffer_bytes == 2 * est.compute_buffer_bytes

    moe = _meta(expert_tensor_bytes=int(5.0 * GIBB), other_tensor_bytes=int(0.5 * GIBB),
                nonlayer_tensor_bytes=int(0.2 * GIBB), output_tensor_bytes=int(0.5 * GIBB),
                expert_count=128)
    est_moe = estimate_load(moe, gpu_layers=56, cpu_moe=True, context_length=4096)
    # 4096 * 25 KiB = 100 MiB (measured 100.02 on the 30B coder).
    assert est_moe.compute_buffer_bytes == 4096 * 25 * 1024


def test_ram_demand_counts_the_mmaped_file():
    """Private bytes track the file size, not just the CPU-resident weights."""
    meta = _meta()
    est = estimate_load(meta, gpu_layers=52, cpu_moe=False, context_length=32768)
    assert est.model_file_bytes == meta.file_size_bytes
    # 27B measured: private 16841 MiB vs file 16032 MiB.
    assert est.ram_demand_bytes >= meta.file_size_bytes
    assert est.ram_total_bytes == est.ram_demand_bytes + est.ram_margin_bytes


def test_run2_27b_no_longer_plans_an_unloadable_all_gpu_load():
    """Regression: at 15.34 GiB free VRAM the 27B must step down.

    The measured run2 plan picked 54 GPU layers (estimated 15.20 GiB, "fits")
    and the real load failed. With the recurrent state counted the estimate for
    that configuration exceeds the freed VRAM, so the planner must choose fewer
    layers (or refuse) instead of planning a load that cannot fit.
    """
    meta = _hybrid_meta()
    free_vram = int(15.34 * GIBB)
    plan = plan_model_load(
        meta, free_vram_bytes=free_vram, free_ram_bytes=int(20.3 * GIBB),
        requested_ctx=32768, has_gpu=True,
    )
    assert plan["fits"] is True
    assert plan["gpu_layers"] < 54, plan
    # Whatever it picked must actually fit the freed VRAM.
    assert plan["estimate"]["vram_total_gib"] <= 15.34

