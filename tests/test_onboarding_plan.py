"""Hardware measurement and model recommendation, for machines we do not own."""
from __future__ import annotations

import pytest

from giscode.onboarding.hardware import (
    GIB,
    SystemProfile,
    fits_disk,
    probe,
    required_disk_bytes,
)
from giscode.onboarding.recommend import (
    CATALOG,
    describe_insufficient,
    recommend,
)
from giscode.providers.hardware import Hardware


def _profile(ram_gb: float, vram_gb: float, disk_free_gb: float = 200.0, gpu: str = "Test GPU"):
    return probe(
        hardware=Hardware(
            total_ram_bytes=int(ram_gb * GIB),
            total_vram_bytes=int(vram_gb * GIB),
            available_vram_bytes=int(vram_gb * GIB),
            gpu_name=gpu if vram_gb else "",
        ),
        memory=(int(ram_gb * GIB), int(ram_gb * GIB * 0.6)),
        disk_usage=lambda _path: (int(500 * GIB), int(disk_free_gb * GIB), "F:/profile"),
    )


def test_probe_reports_what_it_measured():
    profile = _profile(32, 16)
    assert profile.ram_total_gb == pytest.approx(32.0, abs=0.2)
    assert profile.vram_total_gb == pytest.approx(16.0, abs=0.2)
    assert profile.disk_free_gb == pytest.approx(200.0, abs=0.2)
    assert profile.has_gpu is True
    assert "RAM" in profile.summary()


def test_a_machine_without_a_gpu_says_so_instead_of_pretending():
    profile = _profile(16, 0)
    assert profile.has_gpu is False
    assert any("CPU" in note for note in profile.notes)


def test_required_disk_space_exceeds_the_download_size():
    size = 5 * GIB
    assert required_disk_bytes(size) > size
    assert required_disk_bytes(size) - size >= 2 * GIB


def test_unknown_free_space_is_not_treated_as_insufficient():
    profile = _profile(16, 8, disk_free_gb=0)
    assert fits_disk(profile, 5 * GIB) is True


def test_sixteen_gigabyte_gpu_gets_the_fourteen_b_model():
    plan = recommend(_profile(32, 16))
    assert plan.best is not None
    assert plan.best.model == "qwen2.5-coder:14b"
    assert plan.best.ok


def test_eight_gigabyte_gpu_gets_the_seven_b_model():
    plan = recommend(_profile(16, 8))
    assert plan.best is not None
    assert plan.best.model == "qwen2.5-coder:7b"


def test_eight_gigabyte_ram_without_a_gpu_gets_a_small_model():
    plan = recommend(_profile(8, 0))
    assert plan.best is not None
    assert plan.best.model in {"qwen2.5-coder:3b", "qwen2.5-coder:1.5b"}
    assert plan.best.level in {"may_fit", "likely_suitable"}


def test_an_installed_model_is_offered_before_a_download():
    plan = recommend(_profile(32, 16), installed={"llama3.2:3b"})
    assert plan.best is not None
    assert plan.best.model == "llama3.2:3b"
    assert plan.best.installed is True


def test_a_full_disk_rejects_every_download_with_the_reason():
    plan = recommend(_profile(32, 16, disk_free_gb=1))
    assert plan.recommended == ()
    assert plan.rejected
    assert all(item.level == "insufficient_disk" for item in plan.rejected)
    assert "GB free" in describe_insufficient(plan, _profile(32, 16, disk_free_gb=1))


def test_every_recommendation_states_size_and_disk_requirement():
    for item in recommend(_profile(32, 16)).recommended:
        assert item.size_gb > 0
        assert item.required_disk_gb > item.size_gb
        assert item.model


def test_the_catalogue_only_names_real_ollama_tags():
    for entry in CATALOG:
        assert ":" in entry["model"], entry
        assert entry["size_gb"] > 0


def test_a_stored_profile_round_trips_through_json():
    profile = _profile(32, 8)
    restored = SystemProfile.from_dict(profile.as_dict())
    assert restored.ram_total_bytes == profile.ram_total_bytes
    assert restored.gpu_name == profile.gpu_name
    assert restored.disk_free_bytes == profile.disk_free_bytes
