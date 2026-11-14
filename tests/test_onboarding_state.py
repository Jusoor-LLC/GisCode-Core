"""First-run state: crash safety, upgrade safety and honest recovery."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from giscode.onboarding.state import (
    DOWNLOAD_INTERRUPTED,
    OnboardingState,
    OnboardingStore,
    STAGES,
)


@pytest.fixture(autouse=True)
def _isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("GISCODE_HOME", str(tmp_path / "profile"))
    return tmp_path


def test_fresh_profile_has_an_incomplete_welcome_state(tmp_path):
    state = OnboardingStore(tmp_path / "config.json").load()
    assert state.stage == "welcome"
    assert state.completed is False
    assert state.legacy is False
    assert state.chosen_model == ""


def test_save_preserves_every_other_config_key(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"model": {"selected_model": "keep-me"}, "ui": {"appearance": "dark"}}),
        encoding="utf-8",
    )
    store = OnboardingStore(config)
    state = store.load()
    state.advance("hardware")
    store.save(state)

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["model"] == {"selected_model": "keep-me"}
    assert data["ui"] == {"appearance": "dark"}
    assert data["onboarding"]["stage"] == "hardware"


def test_save_is_atomic_and_leaves_no_temporary_file(tmp_path):
    config = tmp_path / "config.json"
    store = OnboardingStore(config)
    store.save(store.load())

    assert config.is_file()
    assert json.loads(config.read_text(encoding="utf-8"))["onboarding"]["schema"] == 1
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_advancing_never_moves_backwards():
    state = OnboardingState()
    state.advance("hardware")
    state.advance("runtime")
    assert state.stage == "hardware"


def test_completion_requires_the_complete_stage_and_the_flag():
    state = OnboardingState()
    state.advance("complete")
    assert state.completed is False  # the flag is not implied by the stage
    state.completed = True
    state.advance("complete")
    assert state.completed is True


def test_a_running_download_left_by_a_dead_process_reads_as_interrupted(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "onboarding": {
                    "schema": 1,
                    "stage": "download",
                    "download": {"model": "qwen2.5-coder:7b", "status": "running", "percent": 42},
                }
            }
        ),
        encoding="utf-8",
    )
    state = OnboardingStore(config).load()
    assert state.download["status"] == DOWNLOAD_INTERRUPTED
    assert state.download["percent"] == 42
    assert "Retry" in state.download["detail"]


def test_an_unknown_stage_restarts_instead_of_crashing():
    assert OnboardingState.from_dict({"stage": "not-a-stage"}).stage == "welcome"
    assert OnboardingState.from_dict({"stage": "not-a-stage", "completed": True}).stage == "complete"


def test_a_damaged_document_falls_back_to_a_fresh_state():
    state = OnboardingState.from_dict({"history": "not-a-list", "attempts": [1, 2]})
    assert state.stage == "welcome"
    assert state.history == []
    assert state.attempts == {}


def test_history_and_attempts_are_bounded_and_counted():
    state = OnboardingState()
    for index in range(100):
        state.note("tick", str(index))
    assert len(state.history) == 60
    assert state.bump_attempt("download:x") == 1
    assert state.bump_attempt("download:x") == 2


def test_every_stage_name_is_accepted():
    for stage in STAGES:
        OnboardingState().advance(stage)


def test_an_unknown_field_is_rejected_rather_than_silently_dropped():
    with pytest.raises(AttributeError):
        OnboardingState().advance("hardware", not_a_field=1)
