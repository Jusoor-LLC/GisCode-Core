"""The saved model is not always the model that ends up loaded.

Autoload resolves ``model.selected_model`` before the window exists. When that
name is not registered, or its file is gone, the app falls back to a discovered
model and says so, once, in the Model Manager note line, instead of leaving a
config that names a model that is not the one running.

The fallback order no longer prefers the vision model: it takes a non-vision
model the resource plan fits, then a vision model it fits, then no model, and
the note names the choice and the reason. These tests pin that wording and the
choice; whether a model then really loads is the resource planner's business
and is covered where the planner is, so a load attempt is spied on here rather
than depending on this machine's free VRAM.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from giscode.config.settings import SettingsManager
from giscode.gui.main_window import MainWindow
from giscode.inference.api import InferenceAPI
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.models import manager as manager_module
from giscode.models.manager import ModelManager

VISION_GGUF = "Qwen3-VL-4B-Instruct-GGUF/Qwen3-VL-4B-Instruct-Q4_K_M.gguf"
TEXT_GGUF = "Qwen3.5-9B-GGUF/Qwen3.5-9B-Q4_K_M.gguf"


def _manager(tmp_path: Path, monkeypatch, *, saved: str, gguf: str | None):
    settings = SettingsManager(config_path=tmp_path / "config.json")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    manager = ModelManager(settings, inference)
    monkeypatch.setattr(manager_module, "_DEFAULT_MODEL_SEARCH_DIRS", [])
    if gguf:
        (tmp_path / "models").mkdir(exist_ok=True)
        # Both a text and a vision model are discovered, so the choice between
        # them is the thing under test.
        for name in (TEXT_GGUF, VISION_GGUF):
            model_file = tmp_path / "models" / name
            model_file.parent.mkdir(parents=True, exist_ok=True)
            model_file.write_text("fake model")
        # A vision model needs its projector beside it, or a real load is
        # refused for a reason that has nothing to do with what is under test.
        (tmp_path / "models" / Path(VISION_GGUF).parent /
         "mmproj-Qwen3-VL-4B-Instruct-F16.gguf").write_text("fake projector")
        settings.set("paths.models_dir", str(tmp_path / "models"))
    settings.set("model.selected_model", saved)
    manager.auto_register_discovered()
    return settings, manager


def _spy_load(monkeypatch, manager):
    """Record the load attempts without depending on this machine's VRAM."""
    attempted: list[str] = []

    def _load(name):
        attempted.append(name)
        return SimpleNamespace(name=name, path="")

    monkeypatch.setattr(manager, "load_model", _load)
    return attempted


def _fixture_names(manager, tmp_path: Path) -> tuple[str, str]:
    """The (text, vision) names discovered from this fixture's own files.

    The registry is process-wide and carries catalog entries too, so only the
    models whose path really is inside ``tmp_path`` are the ones this test
    controls. Vision comes from the app's own capability rule.
    """
    from giscode.vision.capabilities import is_mmproj_path, is_vision_capable

    models = [
        m
        for m in manager.get_registry().list_all()
        if str(tmp_path) in str(m.path) and not is_mmproj_path(m.path)
    ]
    text = [m.name for m in models if not is_vision_capable(m)]
    vision = [m.name for m in models if is_vision_capable(m)]
    assert text, f"this fixture discovers a text model, got {[m.name for m in models]}"
    return text[0], vision


def test_an_unknown_saved_model_reports_the_model_it_used_instead(
    tmp_path, monkeypatch, model_autoload_enabled
):
    from giscode import main as main_module

    settings, manager = _manager(
        tmp_path, monkeypatch, saved="Ghost-Model-9B", gguf=VISION_GGUF
    )
    # Both a vision and a text model are discovered; the planner's verdict is
    # stubbed so the choice under test does not depend on free VRAM.
    monkeypatch.setattr(manager, "check_budget", lambda path: True)
    attempted = _spy_load(monkeypatch, manager)
    main_module._try_autoload_model(
        logging.getLogger("test.startup.note"), settings, manager
    )

    # The text model is chosen, and the note names it and says why.
    text_name, vision_names = _fixture_names(manager, tmp_path)
    assert attempted == [text_name]
    if vision_names:  # the registry is process-wide; the fixture registers one
        assert attempted != [vision_names[0]]
    note = manager.startup_model_note
    assert note == (
        f"Saved model 'Ghost-Model-9B' was not found; using '{text_name}' "
        "(a non-vision model the resource plan fits)"
    )
    assert settings.get("model.selected_model") == text_name


def test_an_unknown_saved_model_with_nothing_loadable_says_none_was_loaded(
    tmp_path, monkeypatch, model_autoload_enabled
):
    from giscode import main as main_module

    settings, manager = _manager(tmp_path, monkeypatch, saved="Ghost-Model-9B", gguf=None)
    # The model registry is process-wide, so a model discovered by another test
    # can still be registered here; an empty one is what "nothing loadable"
    # means for this assertion.
    monkeypatch.setattr(
        manager,
        "get_registry",
        lambda: SimpleNamespace(list_all=lambda: [], get=lambda name: None),
    )
    main_module._try_autoload_model(
        logging.getLogger("test.startup.note"), settings, manager
    )

    assert manager.get_current_model() is None
    assert manager.startup_model_note == (
        "Saved model 'Ghost-Model-9B' was not found; no model loaded "
        "(no discovered GGUF is loadable)"
    )
    assert settings.get("model.selected_model") == "Ghost-Model-9B", (
        "a failed autoload must not rewrite the saved selection"
    )


def test_a_known_saved_model_produces_no_note(
    tmp_path, monkeypatch, model_autoload_enabled
):
    from giscode import main as main_module

    settings, manager = _manager(tmp_path, monkeypatch, saved="", gguf=TEXT_GGUF)
    text_name, _vision_names = _fixture_names(manager, tmp_path)
    settings.set("model.selected_model", text_name)
    attempted = _spy_load(monkeypatch, manager)
    main_module._try_autoload_model(
        logging.getLogger("test.startup.note"), settings, manager
    )

    # The saved model is the one that is used, and nothing is reported.
    assert attempted == [text_name]
    assert not getattr(manager, "startup_model_note", "")


def test_the_window_shows_the_note_once(monkeypatch):
    window = MainWindow.__new__(MainWindow)
    window._model_manager = SimpleNamespace(
        startup_model_note="Saved model 'X' was not found; using 'Y'"
    )
    shown: list[str] = []
    monkeypatch.setattr(window, "_set_model_note", shown.append)

    window._show_startup_model_note()
    window._show_startup_model_note()

    assert shown == ["Saved model 'X' was not found; using 'Y'"]


def test_a_window_with_nothing_to_report_says_nothing(monkeypatch):
    window = MainWindow.__new__(MainWindow)
    window._model_manager = SimpleNamespace()
    shown: list[str] = []
    monkeypatch.setattr(window, "_set_model_note", shown.append)

    window._show_startup_model_note()

    assert shown == []


def test_a_window_without_a_model_manager_is_safe(monkeypatch):
    window = MainWindow.__new__(MainWindow)
    window._model_manager = None
    shown: list[str] = []
    monkeypatch.setattr(window, "_set_model_note", shown.append)

    window._show_startup_model_note()

    assert shown == []
