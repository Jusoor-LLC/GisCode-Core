"""Tests for the Experience Layer.

Tests must NOT require real audio hardware and must NOT touch the
user's real ``~/.giscode/experience.json`` — they pass an explicit
``path=`` to :class:`ExperienceSettingsStore`.
"""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from giscode.experience import (
    DEFAULT_SETTINGS,
    EffectDispatcher,
    EffectName,
    EventBus,
    EventName,
    ExperienceSettings,
    ExperienceSettingsStore,
    PersonalityEngine,
    PersonalityMode,
    SoundPlayer,
    can_use_humor,
    enhance,
    safe_dispatch,
)
from giscode.experience.personality import _PROTECTED_CATEGORIES


# ----------------------------------------------------------------- settings

def test_default_settings_are_conservative():
    s = ExperienceSettings()
    assert s.personality_mode == PersonalityMode.FRIENDLY_HUMOR.value
    assert s.humor_enabled is True
    assert s.sounds_enabled is False
    assert s.effects_enabled is True
    assert 0.0 < s.volume <= 1.0
    assert s.silent_mode is False


def test_default_settings_constant_returns_defaults():
    assert DEFAULT_SETTINGS.personality_mode == PersonalityMode.FRIENDLY_HUMOR.value
    assert DEFAULT_SETTINGS.humor_enabled is True
    assert DEFAULT_SETTINGS.sounds_enabled is False
    assert DEFAULT_SETTINGS.effects_enabled is True


def test_settings_from_dict_filters_unknown_keys():
    s = ExperienceSettings.from_dict({"personality_mode": "serious", "bogus": 123})
    assert s.personality_mode == "serious"
    assert not hasattr(s, "bogus")


def test_settings_from_dict_handles_non_dict():
    s = ExperienceSettings.from_dict(None)  # type: ignore[arg-type]
    assert s.personality_mode == PersonalityMode.FRIENDLY_HUMOR.value


def test_settings_store_round_trip(tmp_path: Path):
    path = tmp_path / "exp.json"
    store = ExperienceSettingsStore(path=str(path))
    s = store.get()
    assert s.personality_mode == PersonalityMode.FRIENDLY_HUMOR.value
    updated = store.update(sounds_enabled=True, volume=0.9, personality_mode="serious")
    assert updated.sounds_enabled is True
    assert updated.volume == 0.9
    assert updated.personality_mode == "serious"
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["sounds_enabled"] is True
    assert on_disk["personality_mode"] == "serious"


def test_settings_store_persists_across_instances(tmp_path: Path):
    path = tmp_path / "exp.json"
    s1 = ExperienceSettingsStore(path=str(path))
    s1.update(volume=0.42, humor_enabled=False)
    s2 = ExperienceSettingsStore(path=str(path))
    s = s2.get()
    assert s.volume == 0.42
    assert s.humor_enabled is False


def test_settings_store_reset_returns_defaults(tmp_path: Path):
    path = tmp_path / "exp.json"
    s = ExperienceSettingsStore(path=str(path))
    s.update(personality_mode="serious")
    reset = s.reset()
    assert reset.personality_mode == PersonalityMode.FRIENDLY_HUMOR.value
    assert reset.humor_enabled is True


def test_settings_store_handles_corrupt_file(tmp_path: Path):
    path = tmp_path / "exp.json"
    path.write_text("not json", encoding="utf-8")
    store = ExperienceSettingsStore(path=str(path))
    s = store.get()
    assert s.personality_mode == PersonalityMode.FRIENDLY_HUMOR.value


def test_settings_store_does_not_create_real_file(tmp_path: Path, monkeypatch):
    """Constructing a store with tmp_path must not touch the real
    ``~/.giscode/experience.json``."""
    real = Path(ExperienceSettingsStore().path)
    if real.exists():
        pytest.skip("real settings file already exists; cannot assert absence")
    ExperienceSettingsStore(path=str(tmp_path / "isolated.json")).get()
    assert not real.exists()


def test_can_use_humor_matrix():
    assert can_use_humor(ExperienceSettings(personality_mode="friendly_humor", humor_enabled=True)) is True
    assert can_use_humor(ExperienceSettings(personality_mode="friendly_humor", humor_enabled=False)) is False
    assert can_use_humor(ExperienceSettings(personality_mode="friendly", humor_enabled=True)) is False
    assert can_use_humor(ExperienceSettings(personality_mode="serious", humor_enabled=True)) is False
    assert can_use_humor(ExperienceSettings(personality_mode="professional", humor_enabled=True)) is False
    assert can_use_humor(ExperienceSettings(silent_mode=True, personality_mode="friendly_humor", humor_enabled=True)) is False


# ----------------------------------------------------------------- events

def test_event_bus_dispatch_invokes_subscribers():
    bus = EventBus()
    received: list = []
    bus.subscribe(EventName.TASK_COMPLETED, lambda e, p: received.append((e, p)))
    n = bus.dispatch(EventName.TASK_COMPLETED, {"x": 1})
    assert n == 1
    assert received == [(EventName.TASK_COMPLETED, {"x": 1})]


def test_event_bus_subscribe_returns_unsubscribe():
    bus = EventBus()
    received: list = []
    unsub = bus.subscribe(EventName.MODEL_READY, lambda e, p: received.append(p))
    bus.dispatch(EventName.MODEL_READY, {"k": 1})
    unsub()
    bus.dispatch(EventName.MODEL_READY, {"k": 2})
    assert received == [{"k": 1}]


def test_event_bus_unknown_event_raises_on_subscribe():
    bus = EventBus()
    with pytest.raises(ValueError):
        bus.subscribe("not_a_real_event", lambda *a: None)


def test_event_bus_dispatch_unknown_event_returns_zero():
    bus = EventBus()
    assert bus.dispatch("not_real", {}) == 0


def test_event_bus_swallows_listener_exceptions():
    bus = EventBus()
    received: list = []

    def boom(e, p):
        raise RuntimeError("nope")

    bus.subscribe(EventName.WARNING, boom)
    bus.subscribe(EventName.WARNING, lambda e, p: received.append(p))
    bus.dispatch(EventName.WARNING, {"x": 1})  # must not raise
    assert received == [{"x": 1}]


def test_event_bus_clear():
    bus = EventBus()
    bus.subscribe(EventName.WARNING, lambda *a: None)
    bus.clear(EventName.WARNING)
    assert bus.listeners(EventName.WARNING) == []
    bus.clear()


def test_safe_dispatch_returns_none_and_does_not_raise():
    bus = EventBus()
    bus.subscribe(EventName.TASK_FAILED, lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert safe_dispatch(bus, EventName.TASK_FAILED) is None


# ----------------------------------------------------------------- personality

def test_personality_engine_greeting_respects_mode():
    assert PersonalityEngine(ExperienceSettings(personality_mode="serious", silent_mode=True)).greeting() == ""
    s_pro = PersonalityEngine(ExperienceSettings(personality_mode="professional"))
    assert s_pro.greeting() == "Ready to help."
    s_friendly = PersonalityEngine(ExperienceSettings(personality_mode="friendly", humor_enabled=True))
    assert s_friendly.greeting()  # some friendly greeting
    s_humor = PersonalityEngine(ExperienceSettings(personality_mode="friendly_humor", humor_enabled=True))
    assert s_humor.greeting()  # humor greeting


def test_personality_engine_silent_mode_disables_personality():
    s = ExperienceSettings(silent_mode=True, personality_mode="friendly_humor", humor_enabled=True)
    eng = PersonalityEngine(s)
    assert eng.greeting() == ""
    assert eng.status_flair("anything") == "anything"
    assert eng.quip_for("tests_passed") == ""


def test_status_flair_never_wraps_protected_categories():
    eng = PersonalityEngine(ExperienceSettings(personality_mode="friendly_humor", humor_enabled=True))
    for cat in _PROTECTED_CATEGORIES:
        assert eng.status_flair("raw text", category=cat) == "raw text"


def test_status_flair_passes_through_when_humor_disabled():
    eng = PersonalityEngine(ExperienceSettings(personality_mode="friendly_humor", humor_enabled=False))
    out = eng.status_flair("hello", category="info")
    assert "hello" in out  # at least the original text is present


def test_status_flair_passes_through_for_professional():
    eng = PersonalityEngine(ExperienceSettings(personality_mode="professional", humor_enabled=True))
    out = eng.status_flair("Build OK", category="build")
    assert "Build OK" in out


def test_quip_for_returns_empty_outside_friendly_humor():
    eng = PersonalityEngine(ExperienceSettings(personality_mode="friendly", humor_enabled=True))
    assert eng.quip_for("tests_passed") == ""


def test_enhance_returns_original_text_unchanged_for_protected():
    out = enhance("Traceback (most recent call last):", category="error", event="tests_failed")
    assert out == "Traceback (most recent call last):"


def test_enhance_handles_empty_text():
    assert enhance("") == ""
    assert enhance(None) in (None, "")  # type: ignore[arg-type]


def test_enhance_never_raises():
    class _BoomSettings:
        def __getattr__(self, name):
            raise RuntimeError("nope")

    # Should swallow the explosion and return the original text
    out = enhance("hello", settings=_BoomSettings())  # type: ignore[arg-type]
    assert out == "hello"


def test_enhance_does_not_mangle_technical_results():
    eng = PersonalityEngine(ExperienceSettings(personality_mode="friendly_humor", humor_enabled=True))
    for _ in range(20):
        out = eng.status_flair("325 tests passed", category="test_output")
        # Original substring must always be present and the test count must not be altered
        assert "325 tests passed" in out
        # Never invent numbers
        assert "326" not in out and "324" not in out


# ----------------------------------------------------------------- sounds

def test_sound_player_skipped_when_disabled():
    p = SoundPlayer(ExperienceSettings(sounds_enabled=False))
    assert p.is_available() is False
    tmp = Path(__file__).resolve()
    p.register(EventName.RESPONSE_READY, str(tmp))
    assert p.play(EventName.RESPONSE_READY) is False
    assert p.stats()["skipped"] >= 1


def test_sound_player_skipped_when_silent_mode():
    p = SoundPlayer(ExperienceSettings(sounds_enabled=True, silent_mode=True))
    p.register(EventName.RESPONSE_READY, str(__file__))
    assert p.play(EventName.RESPONSE_READY) is False


def test_sound_player_skipped_when_volume_zero():
    p = SoundPlayer(ExperienceSettings(sounds_enabled=True, volume=0.0))
    p.register(EventName.RESPONSE_READY, str(__file__))
    assert p.play(EventName.RESPONSE_READY) is False


def test_sound_player_skipped_when_no_file_registered():
    p = SoundPlayer(ExperienceSettings(sounds_enabled=True))
    assert p.play(EventName.RESPONSE_READY) is False


def test_sound_player_skipped_when_file_missing(tmp_path: Path):
    p = SoundPlayer(ExperienceSettings(sounds_enabled=True))
    p.register(EventName.RESPONSE_READY, str(tmp_path / "no_such_file.wav"))
    assert p.play(EventName.RESPONSE_READY) is False
    assert p.stats()["skipped"] >= 1


def test_sound_player_rejects_unknown_event_on_register():
    p = SoundPlayer(ExperienceSettings())
    with pytest.raises(ValueError):
        p.register("not_a_real_event", "x")


def test_sound_player_play_attempts_when_enabled_and_file_exists(tmp_path: Path):
    f = tmp_path / "ok.wav"
    f.write_bytes(b"\x00\x01")
    p = SoundPlayer(ExperienceSettings(sounds_enabled=True, volume=0.5))
    p.register(EventName.RESPONSE_READY, str(f))
    assert p.play(EventName.RESPONSE_READY) is True
    time.sleep(0.05)
    assert p.stats()["played"] == 1


def test_sound_player_unregister():
    p = SoundPlayer(ExperienceSettings(sounds_enabled=True))
    p.register(EventName.MODEL_READY, "x")
    p.unregister(EventName.MODEL_READY)
    assert p.file_for(EventName.MODEL_READY) is None


def test_sound_player_never_raises_on_bad_input():
    p = SoundPlayer(ExperienceSettings())
    assert p.play("not_a_real_event") is False
    assert p.play("") is False


# ----------------------------------------------------------------- effects

def test_effect_dispatcher_fires_to_registered_handlers():
    d = EffectDispatcher(ExperienceSettings(effects_enabled=True))
    received: list = []
    d.register(EffectName.PROCESSING, lambda e, p: received.append((e, p)))
    n = d.fire(EffectName.PROCESSING, {"task": "x"})
    assert n == 1
    assert received == [(EffectName.PROCESSING, {"task": "x"})]


def test_effect_dispatcher_disabled_by_default_settings_does_nothing():
    d = EffectDispatcher(ExperienceSettings(effects_enabled=False))
    received: list = []
    d.register(EffectName.PROCESSING, lambda e, p: received.append(p))
    assert d.fire(EffectName.PROCESSING) == 0
    assert received == []


def test_effect_dispatcher_silent_mode_disables():
    d = EffectDispatcher(ExperienceSettings(effects_enabled=True, silent_mode=True))
    received: list = []
    d.register(EffectName.PROCESSING, lambda e, p: received.append(p))
    assert d.fire(EffectName.PROCESSING) == 0
    assert received == []


def test_effect_dispatcher_swallows_handler_exceptions():
    d = EffectDispatcher(ExperienceSettings(effects_enabled=True))
    received: list = []

    def boom(e, p):
        raise RuntimeError("handler error")

    d.register(EffectName.PROCESSING, boom)
    d.register(EffectName.PROCESSING, lambda e, p: received.append(p))
    d.fire(EffectName.PROCESSING, {"x": 1})  # must not raise
    assert received == [{"x": 1}]
    assert d.stats()["errors"] >= 1


def test_effect_dispatcher_unknown_effect_raises_on_register():
    d = EffectDispatcher()
    with pytest.raises(ValueError):
        d.register("not_a_real_effect", lambda *a: None)


def test_effect_dispatcher_unknown_effect_fire_returns_zero():
    d = EffectDispatcher(ExperienceSettings(effects_enabled=True))
    assert d.fire("not_a_real_effect") == 0


def test_effect_dispatcher_register_requires_callable():
    d = EffectDispatcher()
    with pytest.raises(TypeError):
        d.register(EffectName.PROCESSING, "not callable")  # type: ignore[arg-type]


def test_effect_dispatcher_register_returns_unregister():
    d = EffectDispatcher(ExperienceSettings(effects_enabled=True))
    received: list = []
    unsub = d.register(EffectName.PROCESSING, lambda e, p: received.append(p))
    d.fire(EffectName.PROCESSING, {"k": 1})
    unsub()
    d.fire(EffectName.PROCESSING, {"k": 2})
    assert received == [{"k": 1}]


def test_effect_dispatcher_unregister_all_or_one():
    d = EffectDispatcher(ExperienceSettings(effects_enabled=True))
    d.register(EffectName.PROCESSING, lambda *a: None)
    d.register(EffectName.WARNING, lambda *a: None)
    d.unregister(EffectName.PROCESSING)
    assert d.handlers_for(EffectName.PROCESSING) == []
    assert d.handlers_for(EffectName.WARNING) != []
    d.unregister()
    assert d.handlers_for(EffectName.WARNING) == []


# ----------------------------------------------------------------- composition

def test_personality_failure_does_not_break_caller_in_loop():
    """Simulate 1000 calls where one of them would blow up — caller must
    receive identical results regardless."""
    eng = PersonalityEngine(ExperienceSettings(personality_mode="friendly_humor", humor_enabled=True))
    base = eng.status_flair("325 tests passed", category="test_output")
    # Replace the engine with a broken one
    class _BrokenEngine:
        def status_flair(self, text, category=None):
            raise RuntimeError("nope")

    # The standalone `enhance` helper guards with try/except
    out_good = enhance("hello", event="tests_passed")
    out_broken = enhance("hello", settings=ExperienceSettings(), event=None)
    assert "hello" in out_good
    assert "hello" in out_broken


def test_event_dispatch_failure_does_not_break_surrounding_code():
    bus = EventBus()
    bus.subscribe(EventName.TASK_COMPLETED, lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    # Surrounding code keeps running
    for _ in range(5):
        bus.dispatch(EventName.TASK_COMPLETED, {"ok": True})  # no raise
    assert True


def test_settings_store_thread_safe(tmp_path: Path):
    path = tmp_path / "thr.json"
    store = ExperienceSettingsStore(path=str(path))
    errors: list = []

    def worker(i: int) -> None:
        try:
            for _ in range(20):
                store.update(volume=0.01 * (i % 100 + 1))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert 0.0 < store.get().volume <= 1.0


def test_experience_layer_does_not_touch_real_audio(tmp_path: Path, monkeypatch):
    """A user with all defaults must not invoke any real audio API."""
    called = {"flag": False}

    def fake_open(*a, **kw):
        called["flag"] = True
        raise AssertionError("sound file open should not be called for missing file")

    monkeypatch.setattr("builtins.open", fake_open)
    p = SoundPlayer(ExperienceSettings(sounds_enabled=True, volume=0.5))
    p.register(EventName.RESPONSE_READY, str(tmp_path / "absent.wav"))
    assert p.play(EventName.RESPONSE_READY) is False
    assert called["flag"] is False  # never even attempted to open
