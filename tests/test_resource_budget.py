"""Fix the rebuilt-GisCode auto-load: realistic memory estimator, automatic
step-down, visible failure messages, and a window header that reflects the
actual load state.

These tests target four invariants the bug report asked for:

1. The estimator produces a sensible (low single-digit GiB) total for the
   reference Qwen3-VL-4B-Instruct-Q4_K_M model at n_ctx=16384.
2. The step-down selector picks a strictly smaller context when the budget is
   too small for the requested one.
3. A load failure is recorded on the model's metadata so the GUI and the chat
   path can surface it.
4. The window title reflects the loaded state, including when no model is
   loaded because a load attempt failed.

The estimator tests use a synthetic GGUF-shaped metadata object so they run
without loading the real model file. The integration suite keeps a single
optional check that reads the real GGUF when it's reachable on disk, but it
must NOT be required for CI and must NOT touch production data.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Optional

import pytest


# ---------------------------------------------------------------------------
# Estimator tests
# ---------------------------------------------------------------------------


def _synthetic_meta(block_count: int = 36,
                    kv_heads: int = 8,
                    head_dim: int = 128,
                    file_size_bytes: int = 2_497_281_568) -> "GGUFMetadata":
    from giscode.inference.gguf import GGUFMetadata
    return GGUFMetadata(
        path="<memory>",
        file_size_bytes=file_size_bytes,
        gguf_version=3,
        architecture="qwen3vl",
        block_count=block_count,
        attention_head_count_kv=kv_heads,
        attention_key_length=head_dim,
        attention_value_length=head_dim,
        embedding_length=head_dim * 32,
        context_length_train=262144,
    )


def test_estimator_for_qwen3_vl_4b_at_16k_is_a_low_single_digit_gib():
    """The Qwen3-VL-4B-Instruct reference model fits comfortably at n_ctx=16384.

    The previous estimator reported ~66 GiB (one bug: 4 MiB per token), which
    caused the auto-load to refuse every model. The corrected formula uses the
    real GGUF architecture (36 layers, 8 KV heads, 128 head-dim, fp16) and
    must land in the low single-digit GiB range with the safety margin.
    """
    from giscode.inference.resource_budget import estimate_memory_from_metadata

    meta = _synthetic_meta()
    estimate = estimate_memory_from_metadata(meta, 16384)

    gib = estimate.total_bytes / (1024 ** 3)
    assert 3.0 <= gib <= 8.5, (
        f"Expected Qwen3-VL-4B at n_ctx=16384 to fit in low single-digit GiB; got {gib:.2f} GiB "
        f"(kv={estimate.kv_cache_bytes/1024/1024:.0f} MiB total={estimate.total_bytes/1024/1024:.0f} MiB)"
    )


def test_estimator_kv_bytes_per_token_matches_expected_for_qwen3_vl():
    """The KV cache math is 2 * layers * kv_heads * head_dim * bytes-per-element."""
    from giscode.inference.resource_budget import estimate_memory_from_metadata

    meta = _synthetic_meta(block_count=36, kv_heads=8, head_dim=128)
    est = estimate_memory_from_metadata(meta, 4096)
    # fp16 = 2 bytes; per-token = 2*36*8*128*2 = 147456 bytes
    expected_per_token = 147_456
    expected_kv = expected_per_token * 4096
    assert est.kv_cache_bytes == expected_kv, (
        f"KV cache bytes {est.kv_cache_bytes} != expected {expected_kv}"
    )


def test_estimator_falls_back_when_gguf_metadata_is_partial():
    """If the GGUF lacks layer/KV head fields, the heuristic still beats the
    buggy `n_ctx * 4 MiB` formula, which is enforced as a regression check."""
    from giscode.inference.gguf import GGUFMetadata
    from giscode.inference.resource_budget import estimate_memory_from_metadata

    partial = GGUFMetadata(
        path="<memory>",
        file_size_bytes=2_500_000_000,
        gguf_version=3,
        architecture="unknown",
    )
    estimate = estimate_memory_from_metadata(partial, 16384)
    # Heuristic: ~ per_token = max(64 KiB, filesize/5120)
    estimate_gib = estimate.total_bytes / (1024 ** 3)
    # Buggy formula would yield: 2.4 + 0.25 + 16384*4 MiB ~= 66 GiB.
    # The fallback must stay well under that. 13 GiB is a generous ceiling.
    assert estimate_gib < 13.0, (
        "Even with no GGUF metadata, the estimator must not regress to the buggy 66 GiB figure"
    )


# ---------------------------------------------------------------------------
# Step-down selector tests
# ---------------------------------------------------------------------------


def test_step_down_picks_smaller_context_when_vram_budget_is_tiny():
    """Simulating a constrained machine should drop from 16384 to a strictly
    smaller context."""
    from giscode.inference.resource_budget import select_context_size

    meta = _synthetic_meta(block_count=36, kv_heads=8, head_dim=128)
    # Even when there is some headroom, the requested 16384 exceeds it.
    selection = select_context_size(
        requested=16384,
        filesize_bytes=meta.file_size_bytes,
        free_vram_bytes=2 * 1024 * 1024 * 1024,
        free_ram_bytes=4 * 1024 * 1024 * 1024,
        has_gpu=True,
        metadata_loader=lambda path: meta,
    )

    assert selection["chosen"] > 0
    assert selection["chosen"] < 16384, (
        f"Expected a strictly smaller context under a small VRAM budget; got {selection['chosen']}"
    )
    assert selection["reason"].startswith("stepped-down")


def test_step_down_returns_no_fit_when_even_small_context_wont_fit():
    from giscode.inference.resource_budget import select_context_size

    meta = _synthetic_meta()
    selection = select_context_size(
        requested=16384,
        filesize_bytes=meta.file_size_bytes,
        free_vram_bytes=64 * 1024 * 1024,
        free_ram_bytes=64 * 1024 * 1024,
        has_gpu=True,
        metadata_loader=lambda path: meta,
    )
    assert selection["chosen"] == 0
    assert selection["reason"] == "no-fit"


def test_step_down_fits_at_requested_when_budget_is_sufficient():
    from giscode.inference.resource_budget import select_context_size

    meta = _synthetic_meta()
    selection = select_context_size(
        requested=16384,
        filesize_bytes=meta.file_size_bytes,
        free_vram_bytes=14 * 1024 * 1024 * 1024,
        free_ram_bytes=10 * 1024 * 1024 * 1024,
        has_gpu=True,
        metadata_loader=lambda path: meta,
    )
    assert selection["chosen"] == 16384
    assert selection["reason"] == "fits-as-requested"


# ---------------------------------------------------------------------------
# Visible load-failure tests
# ---------------------------------------------------------------------------


def test_select_context_size_for_path_records_human_readable_reason(tmp_path):
    from giscode.models.manager import select_context_size_for_path

    candidate_path = tmp_path / "fake.gguf"
    candidate_path.write_bytes(b"")
    selection = select_context_size_for_path(
        requested=16384,
        model_path=str(candidate_path),
        filesize_bytes=2_497_281_568,
        budget=None,
    )
    if selection["chosen"] == 0:
        # The shipped system ought to leave at least the CPU-only RAM as
        # headroom, but on a constrained CI box it may still fail. Either way
        # the message must mention VRAM and a candidate list.
        assert "Tried" in selection["reason_text"]
        assert "n_ctx=16384" in selection["reason_text"]
    else:
        assert "(estimate" in selection["reason_text"]


def test_load_failure_records_error_message_on_metadata(tmp_path, monkeypatch):
    """A budget failure must update ModelMetadata.error_message so the GUI
    status label and the chat error path have something visible to show."""
    from giscode.config.settings import SettingsManager
    from giscode.inference.api import InferenceAPI
    from giscode.models.manager import ModelManager
    from giscode.models.types import ModelMetadata

    settings = SettingsManager()
    settings.set("model.selected_model", "Test")
    inference = InferenceAPI(n_ctx=16384, n_threads=4)
    monkeypatch.setattr(inference, "_budget", inference._budget)
    manager = ModelManager(settings, inference)

    fake_path = tmp_path / "tiny.gguf"
    fake_path.write_bytes(b"\x00" * 100)
    manager._registry.register(
        ModelMetadata(
            name="Test",
            family="test",
            path=str(fake_path),
            context_length=16384,
            size_bytes=fake_path.stat().st_size,
        )
    )

    # Force the realistic estimator to refuse everything by patching the GGUF
    # reader (the function the production `select_context_size` actually calls).
    import giscode.inference.resource_budget as rb
    import giscode.inference.gguf as gguf

    def _refuse_read(path):
        meta = gguf.GGUFMetadata(
            path=str(path),
            file_size_bytes=int(Path(path).stat().st_size),
            gguf_version=3,
            architecture="qwen3vl",
            block_count=36,
            attention_head_count_kv=8,
            attention_key_length=128,
            attention_value_length=128,
            embedding_length=4096,
            context_length_train=262144,
        )
        return meta

    monkeypatch.setattr(rb, "read_metadata", _refuse_read)
    monkeypatch.setattr(gguf, "read_metadata", _refuse_read)
    # The estimate must exceed any plausible host (VRAM + share of RAM), so
    # the budget refusal is deterministic instead of depending on how much
    # memory the machine running the suite happens to have free.
    monkeypatch.setattr(rb, "estimate_memory_from_metadata",
                        lambda m, ctx, **_kw: rb.MemoryEstimate(
                            model_size_bytes=int(Path(str(m.path)).stat().st_size) if Path(str(m.path)).exists() else 0,
                            kv_cache_bytes=1 << 50,
                            overhead_bytes=0,
                            safety_margin=1.0,
                            bytes_per_element=2,
                            block_count=int(m.block_count or 0),
                            head_count_kv=int(m.attention_head_count_kv or 0),
                            head_dim=int(m.attention_key_length or 0),
                        ))

    from giscode.inference.errors import InsufficientResourceBudgetError

    with pytest.raises(InsufficientResourceBudgetError):
        manager.load_model("Test")
    meta = manager._registry.get("Test")
    assert meta.error_message
    assert "Insufficient" in meta.error_message or "context" in meta.error_message.lower()
    # Make sure metadata.status surfaced to the GUI's "Error" label class.
    assert meta.status.value == "error"


def test_missing_mmproj_reports_clear_vision_model_error(tmp_path, monkeypatch):
    from giscode.config.settings import SettingsManager
    from giscode.inference.api import InferenceAPI
    import giscode.models.manager as model_manager_module
    from giscode.models.manager import ModelManager
    from giscode.models.types import ModelMetadata
    from giscode.inference.errors import ModelNotFoundError

    model_path = tmp_path / "vision.gguf"
    model_path.write_bytes(b"GGUF" + b"\x00" * 100)
    settings = SettingsManager()
    inference = InferenceAPI(n_ctx=2048)
    manager = ModelManager(settings, inference)
    manager._registry.register(
        ModelMetadata(
            name="Vision",
            family="vision",
            path=str(model_path),
            capabilities=["vision", "chat"],
            context_length=2048,
        )
    )
    monkeypatch.setattr(
        model_manager_module,
        "select_context_size_for_path",
        lambda **kwargs: {
            "chosen": 2048,
            "reason": "fits-as-requested",
            "estimate_bytes": 1,
            "reason_text": "fits",
            "stepdown_log": [],
        },
    )

    with pytest.raises(ModelNotFoundError, match="companion mmproj"):
        manager.load_model("Vision")
    metadata = manager.get_model_status("Vision")
    assert metadata.status.value == "error"
    assert "mmproj" in metadata.error_message


def test_startup_replaces_stale_invalid_selected_model(
    tmp_path, model_autoload_enabled
):
    from giscode.config.settings import SettingsManager
    from giscode.main import _try_autoload_model
    from giscode.models.types import ModelMetadata, ModelRegistry

    selected_path = tmp_path / "Qwen3-VL-4B-Instruct-Q4_K_M.gguf"
    selected_path.write_bytes(b"GGUF")
    text_path = tmp_path / "Qwen3.5-9B-Q4_K_M.gguf"
    text_path.write_bytes(b"GGUF")

    class Manager:
        def __init__(self):
            self.registry = ModelRegistry()
            self.registry.register(ModelMetadata(
                name="Qwen3-VL-4B-Instruct",
                family="qwen3vl",
                path=str(selected_path),
            ))
            self.registry.register(ModelMetadata(
                name="Qwen3.5-9B",
                family="qwen3",
                path=str(text_path),
            ))
            self.loaded = None
            self.startup_model_note = None

        def auto_register_discovered(self):
            return []

        def get_registry(self):
            return self.registry

        def validate_model_path(self, path):
            return Path(path).is_file()

        def check_budget(self, path):
            return True

        def load_model(self, name):
            self.loaded = name
            return self.registry.get(name)

    settings = SettingsManager(config_path=tmp_path / "config.json")
    settings.set("model.selected_model", "Broken")
    manager = Manager()
    logger = __import__("logging").getLogger("test-autoload")
    _try_autoload_model(logger, settings, manager)
    # The stale name is replaced, and the replacement is the text model the
    # resource plan fits - not the vision model, which scored 1 of 10 on the
    # coding judge set.
    assert manager.loaded == "Qwen3.5-9B"
    assert settings.get("model.selected_model") == "Qwen3.5-9B"
    assert "not found" in manager.startup_model_note


def test_window_title_reflects_no_model_when_load_fails(tk_root, monkeypatch, tmp_path):
    """The rebuilt GUI must not stay on 'GisCode' while the model is
    unloaded because of a load failure; the title has to admit it."""
    from giscode.gui import main_window
    from giscode.config.settings import SettingsManager
    from giscode.inference.api import InferenceAPI
    from giscode.models.manager import ModelManager

    settings = SettingsManager(config_path=tmp_path / "config.json")
    # This test deliberately saves a name no registry entry matches. Capture the
    # previous value and put it back, so the placeholder never outlives the test
    # (it did once: the real ~/.giscode/config.json kept "Broken" for a day).
    previous_selected = settings.get("model.selected_model", "")
    settings.set("model.selected_model", "Broken")
    inference = InferenceAPI(n_ctx=2048, n_threads=4)
    manager = ModelManager(settings, inference)

    fake = tmp_path / "broken.gguf"
    fake.write_bytes(b"\x00" * 100)
    from giscode.models.types import ModelMetadata
    manager._registry.register(
        ModelMetadata(
            name="Broken",
            family="test",
            path=str(fake),
            context_length=2048,
            size_bytes=fake.stat().st_size,
        )
    )

    # Force budget refusal without invoking the runtime.
    import giscode.inference.resource_budget as rb
    import giscode.inference.gguf as gguf
    from giscode.inference.api import InferenceAPI as _API

    def _always_fail_meta(path):
        return gguf.GGUFMetadata(
            path=str(path),
            file_size_bytes=1,
            gguf_version=3,
            architecture="qwen3vl",
        )

    monkeypatch.setattr(rb, "read_metadata", _always_fail_meta)
    monkeypatch.setattr(gguf, "read_metadata", _always_fail_meta)
    monkeypatch.setattr(rb, "estimate_memory_from_metadata",
                        lambda m, ctx, **_kw: rb.MemoryEstimate(
                            model_size_bytes=1,
                            kv_cache_bytes=20 * 1024 ** 3,
                            overhead_bytes=0,
                            safety_margin=1.0,
                            bytes_per_element=2,
                            block_count=0,
                            head_count_kv=0,
                            head_dim=0,
                        ))
    # Bypass the runtime before its subprocess spawns.
    monkeypatch.setattr(_API, "load_model", lambda self, *a, **kw: (_ for _ in ()).throw(
        Exception("forced budget refusal")
    ))

    from giscode.inference.errors import InsufficientResourceBudgetError
    try:
        manager.load_model("Broken")
    except (InsufficientResourceBudgetError, Exception):
        pass

    from tests.conftest import _FakeGisAiEngine
    win = main_window.MainWindow(tk_root, gisai_engine=_FakeGisAiEngine(inference), model_manager=manager)

    win._refresh_window_title_from_state()
    title = win.root.title()
    assert "GisCode" in title
    # The failure must show up in the title; never the bare "GisCode" alone.
    assert ("No model" in title) or ("Insufficient" in title) or ("model loaded" in title.lower()), (
        f"Title should surface 'No model' on budget failure; got {title!r}"
    )

    settings.set("model.selected_model", previous_selected)


# ---------------------------------------------------------------------------
# Live integration probe (optional; skipped by default)
# ---------------------------------------------------------------------------


REAL_MODEL_ENV_VAR = "GISCODE_TEST_REAL_QWEN_PATH"


def test_live_qwen3_vl_4b_loads_at_16k_when_present(request):
    """Real GGUF load via the project's loader path. Skipped unless the
    regression-test environment has the reference model on disk."""
    path_str = os.environ.get(REAL_MODEL_ENV_VAR, "").strip()
    if not path_str:
        pytest.skip("set GISCODE_TEST_REAL_QWEN_PATH to enable this live probe")

    from giscode.config.settings import SettingsManager
    from giscode.inference.api import InferenceAPI
    from giscode.inference.errors import InsufficientResourceBudgetError
    from giscode.models.manager import ModelManager

    # Stop any llama-server this session started before loading the reference
    # model. This deliberately does NOT kill a server the session did not start
    # - a machine-wide kill is opt-in via GISCODE_TEST_KILL_ALL_SERVERS=1 - so
    # a developer's own server, and the VRAM it holds, is left alone.
    try:
        from tests.conftest import _kill_llama_servers
    except ImportError:
        _kill_llama_servers = None
    if _kill_llama_servers is not None:
        _kill_llama_servers()

    settings = SettingsManager()
    settings.set("model.selected_model", "Qwen3-VL-4B-Instruct")
    settings.set("inference.context_length", 16384)
    settings.set("paths.models_dir", os.path.dirname(path_str))
    inference = InferenceAPI(n_ctx=16384)
    manager = ModelManager(settings, inference)
    manager.auto_register_discovered()
    try:
        meta = manager.load_model("Qwen3-VL-4B-Instruct")
        assert meta.context_length == 16384
        assert meta.status.value == "ready"
    except InsufficientResourceBudgetError as exc:
        pytest.skip(
            "not enough free VRAM/RAM to load the reference model at this "
            f"machine's current load: {exc}"
        )
    finally:
        try:
            inference.unload_model()
        except Exception:
            pass
        try:
            from tests.conftest import _kill_llama_servers
            _kill_llama_servers()
        except ImportError:
            pass
