"""Model-storage preflight: measured space, honest refusals and OLLAMA_MODELS."""
from __future__ import annotations

import os
from collections import namedtuple
from pathlib import Path

import pytest

from giscode.onboarding.hardware import (
    GIB,
    OLLAMA_MODELS_ENV,
    SystemProfile,
    advise_storage,
    ollama_models_path,
    storage_candidates,
)

_Usage = namedtuple("_Usage", "total used free")


def test_models_path_honours_ollama_models_environment(tmp_path):
    configured = tmp_path / "elsewhere"
    assert ollama_models_path(env={OLLAMA_MODELS_ENV: str(configured)}, home=tmp_path) == configured


def test_models_path_defaults_to_the_ollama_profile(tmp_path):
    assert ollama_models_path(env={}, home=tmp_path) == tmp_path / ".ollama" / "models"


def test_candidates_include_the_current_store_and_giscode_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("GISCODE_HOME", str(tmp_path / "profile"))
    candidates = storage_candidates(env={}, home=tmp_path / "home")
    assert candidates[0] == tmp_path / "home" / ".ollama" / "models"
    assert any(path.name == "ollama-models" for path in candidates)


def test_enough_space_keeps_the_current_models_folder(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    advice = advise_storage(
        SystemProfile(),
        5 * GIB,
        env={},
        home=home,
        disk_usage=lambda _path: _Usage(500 * GIB, 400 * GIB, 100 * GIB),
    )
    assert advice.sufficient is True
    assert advice.needs_override is False
    assert advice.current_path == str(home / ".ollama" / "models")
    assert "fits" in advice.detail


def test_insufficient_space_explains_ollama_models_and_restart(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    advice = advise_storage(
        SystemProfile(),
        5 * GIB,
        env={},
        home=home,
        disk_usage=lambda _path: _Usage(100 * GIB, 99 * GIB, 1 * GIB),
    )
    assert advice.sufficient is False
    assert advice.needs_override is False
    text = " ".join(advice.instructions)
    assert OLLAMA_MODELS_ENV in text
    assert "restart" in text.lower() or "start it again" in text.lower()
    assert "not moved automatically" in text
    assert "nothing was downloaded" in advice.detail


def test_a_roomier_folder_is_recommended_without_moving_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("GISCODE_HOME", str(profile))

    def usage(path):
        # The system-drive Ollama store is full; the GisCode profile drive has room.
        if str(profile) in str(path):
            return _Usage(500 * GIB, 100 * GIB, 400 * GIB)
        return _Usage(100 * GIB, 99 * GIB, 1 * GIB)

    advice = advise_storage(SystemProfile(), 5 * GIB, env={}, home=home, disk_usage=usage)
    assert advice.sufficient is False
    assert advice.needs_override is True
    assert advice.recommended_path == str(profile / "ollama-models")
    assert any(advice.recommended_path in line for line in advice.instructions)


def test_unknown_free_space_is_not_a_confident_refusal(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    advice = advise_storage(
        SystemProfile(),
        5 * GIB,
        env={},
        home=home,
        disk_usage=lambda _path: _Usage(0, 0, 0),
    )
    assert advice.sufficient is True
    assert "could not be measured" in advice.detail


def test_storage_advice_is_serialisable(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    advice = advise_storage(
        SystemProfile(),
        5 * GIB,
        env={},
        home=home,
        disk_usage=lambda _path: _Usage(10 * GIB, 9 * GIB, 1 * GIB),
    )
    payload = advice.as_dict()
    assert payload["free_gb"] == pytest.approx(1.0, abs=0.1)
    assert payload["required_gb"] > payload["free_gb"]
    assert payload["sufficient"] is False
    assert isinstance(payload["instructions"], list)
