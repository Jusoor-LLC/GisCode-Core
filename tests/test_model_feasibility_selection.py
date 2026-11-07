"""Feasibility-aware selection (Stage 3F).

A model that exists but cannot be loaded must never be offered as a usable
coding model. Routing and startup autoload both ask the existing load planner
through ``giscode.models.feasibility``; these tests drive that gate with fakes,
so no weights are read beyond a fake header and no server is started.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from giscode.models import feasibility as feas
from giscode.models.types import ModelMetadata, ModelRegistry
from giscode.routing.router import ModelRouter
from giscode.routing.types import (
    FallbackPolicy,
    ModelAvailability,
    ModelRole,
    RoutingRequest,
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _FakeInference:
    """Serves canned planner output; records every request."""

    def __init__(self, plans=None, *, available=True, error=None) -> None:
        self.plans = plans or {}
        self.available = available
        self.error = error
        self.calls: list[tuple[str, object]] = []

    def plan_load(self, path, n_ctx=None, max_gpu_layers=None):
        self.calls.append((str(path), n_ctx))
        if self.error is not None:
            raise self.error
        plan = self.plans.get(Path(path).name)
        if plan is None:
            raise AssertionError(f"unexpected plan request for {path}")
        return dict(plan)


def _plan(fits=True, *, reason="fits", n_ctx=4096, gpu_layers=1):
    return {
        "fits": fits,
        "reason": reason,
        "n_ctx": n_ctx,
        "gpu_layers": gpu_layers,
        "step": "all-gpu-layers" if fits else "refused",
    }


def _catalog_entry(name, path, *, coding=True, vision=False):
    capabilities = ["text", "chat"]
    if coding:
        capabilities.append("coding")
    if vision:
        capabilities.append("vision")
    return ModelMetadata(
        name=name,
        family="discovered",
        path=path,
        tags=["coding"] if coding else [],
        capabilities=capabilities,
    )


class _StubManager:
    """Only what routing uses: a registry and per-model verdicts."""

    def __init__(self, models, verdicts=None) -> None:
        self._registry = ModelRegistry()
        for model in models:
            self._registry.register(model)
        self._verdicts = verdicts or {}

    def get_registry(self) -> ModelRegistry:
        return self._registry

    def feasibility(self, name, n_ctx=None):
        return self._verdicts[name]


class _LegacyManager:
    """A manager from before Stage 3F: no ``feasibility`` method at all."""

    def __init__(self, models) -> None:
        self._registry = ModelRegistry()
        for model in models:
            self._registry.register(model)

    def get_registry(self) -> ModelRegistry:
        return self._registry


def _verdict(name, *, feasible, category="ok", reason="fits", n_ctx=4096, gpu_layers=1):
    return feas.Feasibility(
        name=name,
        path=f"C:/models/{name}.gguf",
        feasible=feasible,
        category=category,
        reason=reason,
        n_ctx=n_ctx,
        gpu_layers=gpu_layers,
        plan=_plan(feasible, reason=reason, n_ctx=n_ctx, gpu_layers=gpu_layers),
    )


def _route(manager, *, task_type="coding", preferred=None, role=None, policy=None):
    router = ModelRouter(model_manager=manager)
    request = RoutingRequest(
        task_id="t1",
        task_type=task_type,
        requested_role=role,
        preferred_model=preferred,
        fallback_policy=policy or FallbackPolicy.STRICT,
    )
    return router.route(request)


def _general_model(name="general-model"):
    """A perfectly usable chat model with no coding capability at all."""
    return ModelMetadata(
        name=name,
        family="general_reasoning",
        path=f"C:/models/{name}.gguf",
        tags=["chat", "general_reasoning"],
        capabilities=["text", "chat"],
    )


def _coding_model(name, path, *, tags=True):
    """Coding capability, with tags optionally withheld (policy cannot match it)."""
    return ModelMetadata(
        name=name,
        family="discovered",
        path=path,
        tags=["coding"] if tags else [],
        capabilities=["text", "chat", "coding"],
    )


# ---------------------------------------------------------------------------
# the verdict itself
# ---------------------------------------------------------------------------
def test_a_model_that_fits_is_feasible_and_carries_the_plan(tmp_path):
    model = tmp_path / "coder.gguf"
    model.write_bytes(b"GGUF")
    inference = _FakeInference({"coder.gguf": _plan(True, n_ctx=8192, gpu_layers=41)})

    verdict = feas.evaluate(inference, name="coder", path=str(model), n_ctx=32768)

    assert verdict.feasible is True
    assert verdict.category == feas.OK
    assert (verdict.n_ctx, verdict.gpu_layers) == (8192, 41)
    assert inference.calls == [(str(model), 32768)]


def test_a_model_the_planner_refuses_is_infeasible_with_the_planner_reason(tmp_path):
    model = tmp_path / "coder.gguf"
    model.write_bytes(b"GGUF")
    inference = _FakeInference(
        {"coder.gguf": _plan(False, reason="refused: 0 of 49 layers fit at n_ctx=4096")}
    )

    verdict = feas.evaluate(inference, name="coder", path=str(model), n_ctx=4096)

    assert verdict.feasible is False
    assert verdict.category == feas.INSUFFICIENT_MEMORY
    assert "0 of 49 layers fit" in verdict.reason
    assert "insufficient memory" in verdict.describe()


def test_a_missing_file_is_reported_without_asking_the_planner(tmp_path):
    inference = _FakeInference({})

    verdict = feas.evaluate(inference, name="gone", path=str(tmp_path / "gone.gguf"))

    assert verdict.category == feas.MISSING
    assert inference.calls == []


def test_no_usable_runtime_is_reported_honestly(tmp_path):
    model = tmp_path / "coder.gguf"
    model.write_bytes(b"GGUF")
    inference = _FakeInference({}, available=False)

    verdict = feas.evaluate(inference, name="coder", path=str(model))

    assert verdict.category == feas.RUNTIME_UNAVAILABLE
    assert "runtime" in verdict.reason


def test_an_unreadable_header_is_unverified_not_a_memory_refusal(tmp_path):
    """A header the planner cannot read proves nothing about memory.

    The loader has its own fallback for this case, so selection must not refuse
    the model on the planner's behalf.
    """
    model = tmp_path / "broken.gguf"
    model.write_bytes(b"GGUF")
    inference = _FakeInference({}, error=ValueError("buffer too small to be a GGUF header"))

    verdict = feas.evaluate(inference, name="broken", path=str(model))

    assert verdict.category == feas.UNVERIFIED
    assert verdict.feasible is True
    assert "could not evaluate" in verdict.reason


# ---------------------------------------------------------------------------
# one source of truth: the API planner
# ---------------------------------------------------------------------------
def test_plan_load_delegates_to_the_only_planner(monkeypatch):
    from giscode.inference.api import InferenceAPI
    from giscode.inference.mocks import MockLlamaCppRuntime

    api = InferenceAPI(runtime=MockLlamaCppRuntime(), n_ctx=4096)
    seen: list[tuple[str, int, object]] = []

    def fake_plan(path, n_ctx, max_gpu_layers=None):
        seen.append((path, n_ctx, max_gpu_layers))
        return {"fits": True}

    monkeypatch.setattr(api, "_plan_load", fake_plan)

    api.plan_load("m.gguf")
    api.plan_load("m.gguf", n_ctx=8192, max_gpu_layers=12)

    assert seen == [("m.gguf", 4096, None), ("m.gguf", 8192, 12)]


def test_planning_is_repeated_so_changed_resources_are_seen(monkeypatch):
    from giscode.inference.api import InferenceAPI
    from giscode.inference.mocks import MockLlamaCppRuntime

    api = InferenceAPI(runtime=MockLlamaCppRuntime(), n_ctx=4096)
    readings = iter([{"fits": True}, {"fits": False, "reason": "refused"}])
    monkeypatch.setattr(api, "_plan_load", lambda *a, **k: next(readings))

    assert api.plan_load("m.gguf")["fits"] is True
    assert api.plan_load("m.gguf")["fits"] is False


# ---------------------------------------------------------------------------
# coding-role routing
# ---------------------------------------------------------------------------
def test_an_infeasible_coding_model_is_replaced_by_a_feasible_one():
    big = _catalog_entry("coder-large", "C:/models/big.gguf")
    small = _catalog_entry("coder-small", "C:/models/small.gguf")
    manager = _StubManager(
        [big, small],
        {
            "coder-large": _verdict(
                "coder-large",
                feasible=False,
                category=feas.INSUFFICIENT_MEMORY,
                reason="refused: 0 of 49 layers fit",
            ),
            "coder-small": _verdict("coder-small", feasible=True),
        },
    )

    result = _route(manager)

    assert result.selected_model == "coder-small"
    assert result.fallback_used is True
    assert "cannot be loaded" in result.reason
    assert result.metadata["feasibility"]["category"] == feas.OK


def test_no_feasible_coding_model_is_reported_rather_than_guessed():
    big = _catalog_entry("coder-large", "C:/models/big.gguf")
    manager = _StubManager(
        [big],
        {
            "coder-large": _verdict(
                "coder-large",
                feasible=False,
                category=feas.INSUFFICIENT_MEMORY,
                reason="refused: needs 18.0 GiB RAM",
            )
        },
    )

    result = _route(manager)

    assert result.selected_model is None
    assert result.availability == ModelAvailability.UNAVAILABLE
    assert "no other coding model can be loaded" in result.reason
    assert "needs 18.0 GiB RAM" in result.reason


def test_a_vision_only_model_is_never_a_coding_fallback():
    big = _catalog_entry("coder-large", "C:/models/big.gguf")
    vision = _catalog_entry("vision-4b", "C:/models/vl.gguf", coding=False, vision=True)
    manager = _StubManager(
        [big, vision],
        {
            "coder-large": _verdict("coder-large", feasible=False),
            "vision-4b": _verdict("vision-4b", feasible=True),
        },
    )

    result = _route(manager)

    assert result.selected_model is None
    assert result.availability == ModelAvailability.UNAVAILABLE


def test_an_explicit_override_is_refused_not_silently_replaced():
    big = _catalog_entry("coder-large", "C:/models/big.gguf")
    small = _catalog_entry("coder-small", "C:/models/small.gguf")
    manager = _StubManager(
        [big, small],
        {
            "coder-large": _verdict("coder-large", feasible=False),
            "coder-small": _verdict("coder-small", feasible=True),
        },
    )

    result = _route(manager, preferred="coder-large")

    assert result.selected_model is None
    assert result.availability == ModelAvailability.UNAVAILABLE
    assert "explicitly requested" in result.reason
    assert "coder-small" not in (result.selected_model or "")


def test_an_explicit_override_that_fits_is_kept():
    small = _catalog_entry("coder-small", "C:/models/small.gguf")
    manager = _StubManager(
        [small], {"coder-small": _verdict("coder-small", feasible=True, n_ctx=6144, gpu_layers=33)}
    )

    result = _route(manager, preferred="coder-small")

    assert result.selected_model == "coder-small"
    assert result.metadata["feasibility"]["n_ctx"] == 6144
    assert result.metadata["feasibility"]["gpu_layers"] == 33


def test_a_generic_text_model_is_never_selected_for_a_coding_task():
    manager = _StubManager(
        [_general_model()],
        {"general-model": _verdict("general-model", feasible=True)},
    )

    result = _route(manager, policy=FallbackPolicy.GENERAL_REASONING)

    assert result.selected_model is None
    assert result.availability == ModelAvailability.UNAVAILABLE
    assert "declares coding capability" in result.reason
    assert "not substituted" in result.reason


def test_a_coding_task_never_falls_back_to_a_general_role_model():
    general = _general_model()
    # No coding tag, so the policy's own role match fails and it falls back to
    # the general role - the substitution path this test closes.
    unloadable_coder = _coding_model("coder-large", "C:/models/big.gguf", tags=False)
    manager = _StubManager(
        [general, unloadable_coder],
        {
            "general-model": _verdict("general-model", feasible=True),
            "coder-large": _verdict(
                "coder-large",
                feasible=False,
                category=feas.INSUFFICIENT_MEMORY,
                reason="refused: needs 18.0 GiB RAM",
            ),
        },
    )

    result = _route(manager, policy=FallbackPolicy.GENERAL_REASONING)

    assert result.selected_model is None
    assert "no coding-capable model can be loaded" in result.reason
    assert "needs 18.0 GiB RAM" in result.reason


def test_a_feasible_coding_model_replaces_a_general_fallback():
    general = _general_model()
    # No coding tag, so the policy falls back to the general role; only the
    # capability list (not the filename) lets the router find the coder.
    coder = _coding_model("coder-small", "C:/models/small.gguf", tags=False)
    manager = _StubManager(
        [general, coder],
        {
            "general-model": _verdict("general-model", feasible=True),
            "coder-small": _verdict("coder-small", feasible=True),
        },
    )

    result = _route(manager, policy=FallbackPolicy.GENERAL_REASONING)

    assert result.selected_model == "coder-small"
    assert result.selected_role == ModelRole.CODING
    assert result.fallback_used is True


def test_an_explicit_non_coding_model_is_refused_for_a_coding_task():
    manager = _StubManager(
        [_general_model()],
        {"general-model": _verdict("general-model", feasible=True)},
    )

    result = _route(manager, preferred="general-model")

    assert result.selected_model is None
    assert "does not declare coding capability" in result.reason
    assert "explicitly requested" in result.reason


def test_a_non_coding_task_is_unchanged():
    manager = _StubManager(
        [_general_model()],
        {"general-model": _verdict("general-model", feasible=True)},
    )

    result = _route(manager, task_type="chat")

    assert result.selected_model == "general-model"
    assert result.metadata["feasibility"]["category"] == feas.OK


def test_a_manager_without_feasibility_keeps_the_previous_behaviour():
    entry = _catalog_entry("coder", "C:/models/coder.gguf")
    manager = _LegacyManager([entry])

    result = _route(manager)

    assert result.selected_model == "coder"
    assert result.availability == ModelAvailability.REGISTERED


# ---------------------------------------------------------------------------
# startup autoload
# ---------------------------------------------------------------------------
_TEXT = "C:/models/Qwen3.5-9B-Q4_K_M.gguf"
_CODER = "C:/models/Qwen3-Coder-30B.gguf"
_SMALL_CODER = "C:/models/Qwen3-Coder-14B.gguf"
_VISION = "C:/models/Qwen3-VL-4B.gguf"


class _StartupManager:
    def __init__(self, models, *, loadable, fits, verdicts=None) -> None:
        self._registry = ModelRegistry()
        for model in models:
            self._registry.register(model)
        self._loadable = set(loadable)
        self._fits = set(fits)
        self._verdicts = verdicts or {}
        self.loaded: list[str] = []
        self.startup_model_note = None

    def auto_register_discovered(self):
        return []

    def get_registry(self) -> ModelRegistry:
        return self._registry

    def validate_model_path(self, path: str) -> bool:
        return path in self._loadable

    def check_budget(self, path: str) -> bool:
        return path in self._fits

    def feasibility(self, name, n_ctx=None):
        return self._verdicts[name]

    def load_model(self, name: str):
        self.loaded.append(name)
        return SimpleNamespace(name=name, path=self._registry.get(name).path)


class _Settings:
    def __init__(self, selected: str = "") -> None:
        self._values = {"model.selected_model": selected}

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value) -> None:
        self._values[key] = value


def _autoload(manager, selected="Qwen3-Coder-30B"):
    from giscode.main import _try_autoload_model

    _try_autoload_model(logging.getLogger("test.feasibility"), _Settings(selected), manager)


def _startup_models():
    return [
        _catalog_entry("Qwen3-Coder-30B", _CODER),
        _catalog_entry("Qwen3-Coder-14B", _SMALL_CODER),
        _catalog_entry("Qwen3.5-9B", _TEXT, coding=False),
        _catalog_entry("Qwen3-VL-4B", _VISION, coding=False, vision=True),
    ]


def test_startup_still_notes_a_missing_saved_model(model_autoload_enabled):
    manager = _StartupManager(
        _startup_models(), loadable=set(), fits=set(), verdicts={}
    )

    _autoload(manager)

    assert manager.loaded == []
    assert manager.startup_model_note.startswith("Saved model 'Qwen3-Coder-30B' was not found")


def test_startup_falls_back_when_the_saved_model_cannot_fit(model_autoload_enabled):
    manager = _StartupManager(
        _startup_models(),
        loadable={_CODER, _SMALL_CODER, _TEXT, _VISION},
        fits={_SMALL_CODER, _TEXT, _VISION},
        verdicts={
            "Qwen3-Coder-30B": _verdict(
                "Qwen3-Coder-30B",
                feasible=False,
                category=feas.INSUFFICIENT_MEMORY,
                reason="refused: needs 18.0 GiB RAM",
            ),
            "Qwen3-Coder-14B": _verdict("Qwen3-Coder-14B", feasible=True),
        },
    )

    _autoload(manager)

    assert manager.loaded == ["Qwen3-Coder-14B"]
    note = manager.startup_model_note
    assert "cannot be loaded" in note
    assert "insufficient memory" in note
    assert "needs 18.0 GiB RAM" in note
    assert "coding-capable model the resource plan fits" in note


def test_startup_does_not_substitute_a_vision_model_for_a_coding_model(
    model_autoload_enabled,
):
    manager = _StartupManager(
        _startup_models(),
        loadable={_CODER, _VISION},
        fits={_VISION},
        verdicts={"Qwen3-Coder-30B": _verdict("Qwen3-Coder-30B", feasible=False)},
    )

    _autoload(manager)

    assert manager.loaded == []
    assert "no coding-capable model fits" in manager.startup_model_note


def test_startup_never_loads_a_generic_text_model_for_a_coding_model(
    model_autoload_enabled,
):
    """A saving of a coding model must not be answered with a chat model."""
    manager = _StartupManager(
        _startup_models(),
        loadable={_CODER, _TEXT},
        fits={_TEXT},
        verdicts={"Qwen3-Coder-30B": _verdict("Qwen3-Coder-30B", feasible=False)},
    )

    _autoload(manager)

    assert manager.loaded == []
    assert "not substituted" in manager.startup_model_note


def test_startup_fallback_for_a_non_coding_saved_model_is_unchanged(
    model_autoload_enabled,
):
    """Only coding replacements are strict; ordinary autoload still works."""
    manager = _StartupManager(
        _startup_models(),
        loadable={_CODER},
        fits={_CODER},
        verdicts={},
    )

    _autoload(manager, selected="Qwen3.5-9B")

    assert manager.loaded == ["Qwen3-Coder-30B"]
    assert manager.startup_model_note.startswith("Saved model 'Qwen3.5-9B' was not found")


def test_startup_loads_a_feasible_saved_model_without_a_note(model_autoload_enabled):
    manager = _StartupManager(
        _startup_models(),
        loadable={_CODER},
        fits={_CODER},
        verdicts={"Qwen3-Coder-30B": _verdict("Qwen3-Coder-30B", feasible=True)},
    )

    _autoload(manager)

    assert manager.loaded == ["Qwen3-Coder-30B"]
    assert manager.startup_model_note is None
