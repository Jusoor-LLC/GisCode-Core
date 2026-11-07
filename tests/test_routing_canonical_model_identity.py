"""Regression tests: the routing layer must resolve a runtime/file model
identity to the canonical registered model name.

Production failure this locks down:

    registry name : Qwen3-VL-4B-Instruct
    GGUF filename : Qwen3-VL-4B-Instruct-Q4_K_M.gguf
    runtime name  : Path(model_path).stem -> "Qwen3-VL-4B-Instruct-Q4_K_M"

``CodingAgentLoop._preferred_routing_model()`` hands that file-stem identity to
``DeterministicRoutingPolicy.find_model_for_role()``, whose exact registry
lookup missed, and because ``RoutingRequest.fallback_policy`` defaults to
``STRICT`` there was no fallback. The result on the normal production GUI path
was ``planning failure: no planning model available``.

These tests cover:
  A. a quantized file stem resolves to its registered name
  B. canonical registered names keep working unchanged
  C. no unrelated routing behaviour changes
"""
import pytest

from giscode.models.catalog import register_default_models
from giscode.models.manager import ModelManager
from giscode.models.types import ModelMetadata, ModelRegistry, ModelStatus
from giscode.routing.policies import DeterministicRoutingPolicy
from giscode.routing.types import (
    FallbackPolicy,
    ModelAvailability,
    ModelRole,
    RoutingRequest,
)

# The real catalog names, used only as test fixtures.
VL = "Qwen3-VL-4B-Instruct"
CODER_30B = "Qwen3-Coder-30B-A3B-Instruct"
CODER_480B = "Qwen3-Coder-480B-A35B-Instruct"


def _catalog_registry() -> ModelRegistry:
    registry = ModelRegistry()
    register_default_models(registry)
    return registry


# --------------------------------------------------------------------------
# A. the mismatch: quantized file stem -> canonical registered name
# --------------------------------------------------------------------------

def test_quantized_stem_resolves_to_registered_name():
    registry = _catalog_registry()
    assert registry.resolve_canonical_name("Qwen3-VL-4B-Instruct-Q4_K_M").name == VL


@pytest.mark.parametrize(
    "stem",
    [
        "Qwen3-VL-4B-Instruct-Q4_K_M",
        "Qwen3-VL-4B-Instruct-Q8_0",
        "Qwen3-VL-4B-Instruct-Q5_K_M",
        "Qwen3-VL-4B-Instruct-F16",
        "qwen3-vl-4b-instruct-q4_k_m",
    ],
)
def test_every_quantization_suffix_variant_resolves(stem):
    """Any catalog-backed GGUF whose filename carries a quant suffix resolves."""
    assert _catalog_registry().resolve_canonical_name(stem).name == VL


@pytest.mark.parametrize(
    "stem,expected",
    [
        (f"{CODER_30B}-Q4_K_M", CODER_30B),
        (f"{CODER_480B}-Q8_0", CODER_480B),
    ],
)
def test_other_catalog_models_resolve_so_nothing_is_hardcoded(stem, expected):
    assert _catalog_registry().resolve_canonical_name(stem).name == expected


def test_policy_resolves_preferred_file_stem_to_registered_metadata():
    registry = _catalog_registry()
    policy = DeterministicRoutingPolicy()
    metadata, availability = policy.find_model_for_role(
        ModelRole.PLANNING, registry, "Qwen3-VL-4B-Instruct-Q4_K_M"
    )
    assert metadata is not None
    assert metadata.name == VL
    assert availability == ModelAvailability.REGISTERED


def test_strict_route_with_file_stem_preferred_is_not_unavailable():
    """The exact production failure: STRICT fallback, stem identity, PLANNING."""
    registry = _catalog_registry()
    request = RoutingRequest(
        task_id="t1",
        task_type="planning",
        requested_role=ModelRole.PLANNING,
        task_description="create a file",
        preferred_model="Qwen3-VL-4B-Instruct-Q4_K_M",
    )
    assert request.fallback_policy == FallbackPolicy.STRICT
    result = DeterministicRoutingPolicy().route(request, registry)
    assert result.availability != ModelAvailability.UNAVAILABLE
    assert result.selected_model == VL


def test_manager_match_registry_still_returns_registered_name_for_stem():
    manager = ModelManager(settings=None, inference=None)
    assert manager._match_registry("Qwen3-VL-4B-Instruct-Q4_K_M") == VL


# --------------------------------------------------------------------------
# B. canonical names keep working unchanged
# --------------------------------------------------------------------------

def test_canonical_name_resolves_to_itself():
    registry = _catalog_registry()
    assert registry.resolve_canonical_name(VL).name == VL


def test_canonical_preferred_name_routes_unchanged():
    registry = _catalog_registry()
    policy = DeterministicRoutingPolicy()
    metadata, availability = policy.find_model_for_role(
        ModelRole.PLANNING, registry, VL
    )
    assert metadata.name == VL
    assert availability == ModelAvailability.REGISTERED


def test_role_matching_without_preferred_model_is_unchanged():
    registry = ModelRegistry()
    registry.register(
        ModelMetadata(
            name="plan-model",
            family="plan",
            path="",
            tags=["planning"],
            status=ModelStatus.REGISTERED,
        )
    )
    policy = DeterministicRoutingPolicy()
    metadata, availability = policy.find_model_for_role(ModelRole.PLANNING, registry)
    assert metadata.name == "plan-model"
    assert availability == ModelAvailability.REGISTERED


def test_manager_match_registry_still_returns_canonical_name_unchanged():
    manager = ModelManager(settings=None, inference=None)
    assert manager._match_registry(VL) == VL


# --------------------------------------------------------------------------
# C. no unrelated routing behaviour changes
# --------------------------------------------------------------------------

def test_unknown_preferred_model_is_still_unavailable():
    """Resolution must not over-match an identity that matches nothing."""
    registry = _catalog_registry()
    policy = DeterministicRoutingPolicy()
    metadata, availability = policy.find_model_for_role(
        ModelRole.PLANNING, registry, "some-unrelated-model-XL9"
    )
    assert metadata is None
    assert availability == ModelAvailability.UNAVAILABLE


def test_unresolvable_preferred_model_still_fails_under_strict():
    registry = _catalog_registry()
    request = RoutingRequest(
        task_id="t2",
        task_type="planning",
        requested_role=ModelRole.PLANNING,
        preferred_model="some-unrelated-model-XL9",
    )
    result = DeterministicRoutingPolicy().route(request, registry)
    assert result.availability == ModelAvailability.UNAVAILABLE
    assert result.selected_model is None
    assert result.fallback_used is False


def test_empty_identity_resolves_to_none():
    assert _catalog_registry().resolve_canonical_name("") is None


def test_falsy_preferred_model_still_uses_role_matching():
    """``if preferred_model:`` semantics are unchanged for empty/None."""
    registry = ModelRegistry()
    registry.register(
        ModelMetadata(
            name="plan-model",
            family="plan",
            path="",
            tags=["planning"],
            status=ModelStatus.REGISTERED,
        )
    )
    policy = DeterministicRoutingPolicy()
    for falsy in ("", None):
        metadata, availability = policy.find_model_for_role(
            ModelRole.PLANNING, registry, falsy
        )
        assert metadata.name == "plan-model"
        assert availability == ModelAvailability.REGISTERED


def test_fallback_policy_defaults_and_map_untouched():
    """The task forbids broadening fallback; assert it is still STRICT-first."""
    request = RoutingRequest(task_id="t3", task_type="planning")
    assert request.fallback_policy == FallbackPolicy.STRICT
    policy = DeterministicRoutingPolicy()
    fallback_role, metadata, availability = policy.determine_fallback(
        ModelRole.PLANNING, FallbackPolicy.STRICT, _catalog_registry()
    )
    assert fallback_role is None
    assert metadata is None
    assert availability == ModelAvailability.UNAVAILABLE


def test_manager_match_registry_returns_none_for_unknown():
    manager = ModelManager(settings=None, inference=None)
    assert manager._match_registry("totally-unrelated") is None
    assert manager._match_registry("") is None


def test_catalog_registry_contents_unchanged():
    """No second registry: the catalog still holds exactly its three models."""
    registry = _catalog_registry()
    assert sorted(m.name for m in registry.list_all()) == sorted(
        [VL, CODER_30B, CODER_480B]
    )
