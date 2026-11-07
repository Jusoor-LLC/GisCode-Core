"""Startup must not prefer a vision-only model for coding work.

Evidence: the old rule in ``_try_autoload_model`` fell back to a model named
``Qwen3-VL-4B-Instruct`` first, which scored 1 of 10 on the coding judge set.
The replacement rule names no model: a text (non-vision) model the resource
plan fits, then a vision model it fits, then no model.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from giscode.models.types import ModelMetadata

_TEXT = "C:/models/Qwen3.5-9B-Q4_K_M.gguf"
_VISION = "C:/models/Qwen3-VL-4B-Instruct-Q4_K_M.gguf"
_OTHER_TEXT = "C:/models/Qwen3-Coder-14B-Q4_K_M.gguf"


def _model(name: str, path: str) -> ModelMetadata:
    return ModelMetadata(name=name, family="discovered", path=path)


class _Registry:
    def __init__(self, models) -> None:
        self._models = list(models)

    def list_all(self):
        return list(self._models)

    def get(self, name):
        for model in self._models:
            if model.name == name:
                return model
        return None


class _Manager:
    def __init__(self, models, *, loadable=(), fits=()) -> None:
        self._registry = _Registry(models)
        self._loadable = set(loadable)
        self._fits = set(fits)
        self.loaded: list[str] = []
        self.startup_model_note = None

    def auto_register_discovered(self):
        return []

    def get_registry(self):
        return self._registry

    def validate_model_path(self, path: str) -> bool:
        return path in self._loadable

    def check_budget(self, path: str) -> bool:
        return path in self._fits

    def load_model(self, name: str):
        self.loaded.append(name)
        model = self._registry.get(name)
        return SimpleNamespace(name=name, path=getattr(model, "path", ""))


class _Settings:
    def __init__(self, selected: str = "") -> None:
        self._values = {"model.selected_model": selected}

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value) -> None:
        self._values[key] = value


# ---------------------------------------------------------------------------
# The ordering rule
# ---------------------------------------------------------------------------


def _choose(registered, *, loadable, fits, is_vision):
    from giscode.main import _choose_fallback_model

    return _choose_fallback_model(
        registered, loadable=loadable, fits=fits, is_vision=is_vision
    )


def test_a_text_model_that_fits_wins_over_a_vision_model_that_also_fits():
    text = _model("Qwen3.5-9B", _TEXT)
    vision = _model("Qwen3-VL-4B-Instruct", _VISION)

    chosen, why = _choose(
        [vision, text],
        loadable=lambda item: True,
        fits=lambda item: True,
        is_vision=lambda item: item is vision,
    )

    assert chosen is text
    assert "non-vision" in why


def test_a_vision_model_is_used_only_when_no_text_model_fits():
    text = _model("Qwen3.5-9B", _TEXT)
    vision = _model("Qwen3-VL-4B-Instruct", _VISION)

    chosen, why = _choose(
        [text, vision],
        loadable=lambda item: True,
        fits=lambda item: item is vision,
        is_vision=lambda item: item is vision,
    )

    assert chosen is vision
    assert "vision" in why and "fits" in why


def test_no_model_is_chosen_when_nothing_fits_the_resource_plan():
    text = _model("Qwen3.5-9B", _TEXT)
    vision = _model("Qwen3-VL-4B-Instruct", _VISION)

    chosen, why = _choose(
        [text, vision],
        loadable=lambda item: True,
        fits=lambda item: False,
        is_vision=lambda item: item is vision,
    )

    assert chosen is None
    assert "fits" in why


def test_no_model_is_chosen_when_nothing_discovered_is_loadable():
    chosen, why = _choose(
        [],
        loadable=lambda item: True,
        fits=lambda item: True,
        is_vision=lambda item: False,
    )

    assert chosen is None
    assert "loadable" in why


def test_a_projector_file_is_never_chosen_as_a_model():
    projector = _model(
        "mmproj-Qwen3-VL-4B-Instruct-F16",
        "C:/models/mmproj-Qwen3-VL-4B-Instruct-F16.gguf",
    )
    text = _model("Qwen3.5-9B", _TEXT)

    only_projector, why = _choose(
        [projector],
        loadable=lambda item: True,
        fits=lambda item: True,
        is_vision=lambda item: True,
    )
    assert only_projector is None
    assert "loadable" in why

    chosen, _ = _choose(
        [projector, text],
        loadable=lambda item: True,
        fits=lambda item: True,
        is_vision=lambda item: item is projector,
    )
    assert chosen is text


def test_a_modelfile_that_fails_the_budget_check_is_skipped_for_the_next_one():
    first = _model("Qwen3.5-9B", _TEXT)
    second = _model("Qwen3-Coder-14B", _OTHER_TEXT)

    chosen, _ = _choose(
        [first, second],
        loadable=lambda item: True,
        fits=lambda item: item is second,
        is_vision=lambda item: False,
    )

    assert chosen is second


# ---------------------------------------------------------------------------
# The vision rule is generic, not a hardcoded name
# ---------------------------------------------------------------------------


def test_the_vision_rule_comes_from_capabilities_not_a_hardcoded_name():
    from giscode.vision.capabilities import is_vision_capable

    assert is_vision_capable(_model("Qwen3-VL-4B-Instruct", _VISION)) is True
    assert is_vision_capable(_model("SomeVisionModel", "C:/models/mmproj-x.gguf")) is True
    assert is_vision_capable(_model("Qwen3.5-9B", _TEXT)) is False
    assert is_vision_capable(_model("Qwen3-Coder-14B", _OTHER_TEXT)) is False


# ---------------------------------------------------------------------------
# The startup path
# ---------------------------------------------------------------------------


def test_startup_falls_back_to_a_text_model_and_says_why(model_autoload_enabled):
    from giscode import main as main_module

    vision = _model("Qwen3-VL-4B-Instruct", _VISION)
    text = _model("Qwen3.5-9B", _TEXT)
    manager = _Manager(
        [vision, text], loadable={_VISION, _TEXT}, fits={_VISION, _TEXT}
    )
    settings = _Settings("SavedModelThatIsGone")

    main_module._try_autoload_model(logging.getLogger("test.startup"), settings, manager)

    assert manager.loaded == ["Qwen3.5-9B"]
    assert settings.get("model.selected_model") == "Qwen3.5-9B"
    assert manager.startup_model_note == (
        "Saved model 'SavedModelThatIsGone' was not found; using 'Qwen3.5-9B' "
        "(a non-vision model the resource plan fits)"
    )


def test_startup_uses_a_vision_model_when_only_a_vision_model_fits(
    model_autoload_enabled,
):
    from giscode import main as main_module

    text = _model("Qwen3.5-9B", _TEXT)
    vision = _model("Qwen3-VL-4B-Instruct", _VISION)
    manager = _Manager([text, vision], loadable={_TEXT, _VISION}, fits={_VISION})
    settings = _Settings("SavedModelThatIsGone")

    main_module._try_autoload_model(logging.getLogger("test.startup"), settings, manager)

    assert manager.loaded == ["Qwen3-VL-4B-Instruct"]
    assert "only a vision model fits" in (manager.startup_model_note or "")


def test_startup_loads_nothing_when_no_discovered_model_fits(
    model_autoload_enabled,
):
    from giscode import main as main_module

    text = _model("Qwen3.5-9B", _TEXT)
    manager = _Manager([text], loadable={_TEXT}, fits=set())
    settings = _Settings("SavedModelThatIsGone")

    main_module._try_autoload_model(logging.getLogger("test.startup"), settings, manager)

    assert manager.loaded == []
    assert settings.get("model.selected_model") == "SavedModelThatIsGone"
    assert manager.startup_model_note == (
        "Saved model 'SavedModelThatIsGone' was not found; no model loaded "
        "(no discovered model fits the current resource plan)"
    )


def test_a_valid_selection_is_still_loaded_without_a_fallback_note(
    model_autoload_enabled,
):
    from giscode import main as main_module

    text = _model("Qwen3.5-9B", _TEXT)
    manager = _Manager([text], loadable={_TEXT}, fits={_TEXT})
    settings = _Settings("Qwen3.5-9B")

    main_module._try_autoload_model(logging.getLogger("test.startup"), settings, manager)

    assert manager.loaded == ["Qwen3.5-9B"]
    assert manager.startup_model_note is None
    assert settings.get("model.selected_model") == "Qwen3.5-9B"


def test_no_selection_still_loads_nothing(model_autoload_enabled):
    from giscode import main as main_module

    text = _model("Qwen3.5-9B", _TEXT)
    manager = _Manager([text], loadable={_TEXT}, fits={_TEXT})
    settings = _Settings("")

    main_module._try_autoload_model(logging.getLogger("test.startup"), settings, manager)

    assert manager.loaded == []
    assert manager.startup_model_note is None
