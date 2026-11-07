"""What a model can do, and how strongly that is known.

Stage 3G made *selection* strict: a coding task is answered only by a model that
declares coding capability and passes the load planner. This module pins the
other half of the honesty requirement:

* a file name is a hint, never a verification
  (``compute_capabilities_for_discovered`` used to mark any "coder" file
  ``coding`` with nothing to distinguish that from a catalog declaration);
* a verification belongs to the model's *content*, so it follows a rename but
  is not inherited by different weights;
* a guess is never written to the profile.
"""
from __future__ import annotations

import json
import os

import pytest

from giscode.config.paths import giscode_home
from giscode.models import capability as cap
from giscode.models.capability_store import CapabilityStore
from giscode.models.catalog import QWEN3_CODER_30B, QWEN3_VL_4B
from giscode.models.feasibility import evaluate
from giscode.models.manager import ModelManager
from giscode.models.types import ModelMetadata, ModelRegistry
from giscode.routing.router import ModelRouter
from giscode.routing.types import FallbackPolicy, ModelRole, RoutingRequest


def _model_file(tmp_path, name="Qwen3-Coder-30B-Q3_K_L.gguf", payload=b"GGUF" + b"\0" * 64):
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def _content(path):
    """The full-file identity a record needs, computed the way the store does."""
    return cap.content_identity_for(path)


def _big_model_file(tmp_path, name="big.gguf", size=20 * 1024 * 1024):
    """A file large enough that the header digest cannot see its tail."""
    path = tmp_path / name
    path.write_bytes(b"GGUF" + b"\0" * (size - 4))
    return path


def _patch_and_touch(path, offset):
    """Change tensor data in place and make sure the change is visible."""
    with path.open("r+b") as handle:
        handle.seek(offset)
        handle.write(b"\xff" * 64)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def _discovered(path, capabilities=("text", "chat", "coding")):
    return ModelMetadata(
        name=path.stem,
        family="discovered",
        path=str(path),
        size_bytes=path.stat().st_size,
        tags=[cap for cap in capabilities if cap == "coding"],
        capabilities=list(capabilities),
        source="discovered",
    )


# ---------------------------------------------------------------------------
# 1. evidence: a name is a hint, not a verification
# ---------------------------------------------------------------------------
def test_a_file_name_is_a_hint_not_a_verification(tmp_path):
    metadata = _discovered(_model_file(tmp_path))

    record = cap.evidence_for(metadata)["coding"]

    assert record.level == cap.INFERRED_NAME
    assert record.verified is False
    assert record.trustworthy is False
    assert "file name" in record.describe()


def test_text_support_is_evidence_from_the_file_itself(tmp_path):
    record = cap.evidence_for(_discovered(_model_file(tmp_path)))["text"]

    assert record.level == cap.METADATA
    assert record.trustworthy is True
    # ...but "it is a text model" is still not "it is a coding model".
    assert record.verified is False


def test_a_catalog_entry_is_declared_and_not_verified():
    record = cap.evidence_for(QWEN3_CODER_30B)["coding"]

    assert record.level == cap.DECLARED
    assert record.source == "catalog"
    assert record.trustworthy is True
    assert record.verified is False  # nothing has run it yet


def test_a_vision_model_is_not_a_coding_model():
    records = cap.evidence_for(QWEN3_VL_4B)

    assert "coding" not in records
    assert records["vision"].level == cap.DECLARED


def test_an_unreadable_model_reports_unknown_evidence(tmp_path):
    metadata = ModelMetadata(
        name="ghost",
        family="discovered",
        path=str(tmp_path / "missing.gguf"),
        capabilities=["text", "coding"],
        source="discovered",
    )

    records = cap.evidence_for(metadata)

    assert records["text"].level == cap.UNKNOWN
    assert records["coding"].level == cap.UNKNOWN  # not even a name hint applies
    assert records["text"].verified is False


# ---------------------------------------------------------------------------
# 2. identity: verification follows content, not the path
# ---------------------------------------------------------------------------
def test_verification_follows_a_renamed_file(tmp_path):
    original = _model_file(tmp_path)
    moved = tmp_path / "renamed-but-identical.gguf"
    original.replace(moved)
    assert cap.identity_for(original).digest == ""  # gone
    identity = cap.content_identity_for(moved)
    store = CapabilityStore(tmp_path / "caps.json")
    assert store.record(identity, "coding", cap.VERIFIED, source="test") is True

    record = cap.evidence_for(_discovered(moved), store.for_identity(identity))["coding"]

    assert record.level == cap.VERIFIED
    assert record.verified is True
    # ...and the validated lookup still recognises the renamed file.
    assert store.for_path(moved)["coding"]["level"] == cap.VERIFIED


def test_replaced_weights_do_not_inherit_a_verification(tmp_path):
    path = _model_file(tmp_path)
    old_identity = cap.content_identity_for(path)
    store = CapabilityStore(tmp_path / "caps.json")
    assert store.record(old_identity, "coding", cap.VERIFIED, source="test") is True

    path.write_bytes(b"GGUF" + b"\1" * 96)  # different quantization / new file
    new_identity = cap.identity_for(path)

    assert new_identity.digest != old_identity.digest
    assert store.for_path(path) == {}
    record = cap.evidence_for(_discovered(path), store.for_identity(new_identity))["coding"]
    assert record.level == cap.INFERRED_NAME


def test_the_header_digest_alone_cannot_see_a_changed_tail(tmp_path):
    """Why the full content hash exists: same size, same header, new weights."""
    path = _big_model_file(tmp_path)
    header_before = cap.identity_for(path)
    content_before = cap.content_identity_for(path)

    _patch_and_touch(path, 19 * 1024 * 1024)

    assert cap.identity_for(path).digest == header_before.digest  # unchanged
    assert content_before.complete is True
    assert cap.content_identity_for(path).content != content_before.content


def test_different_weights_in_the_same_file_are_refused_the_record(tmp_path):
    path = _big_model_file(tmp_path, name="Qwen3-Coder-30B-Q3_K_L.gguf")
    store = CapabilityStore(tmp_path / "caps.json")

    assert store.record_for_path(path, "coding", cap.VERIFIED, source="test") is True
    assert store.for_path(path)["coding"]["level"] == cap.VERIFIED

    # Same file, same size, same header - only the tensor data differs.
    _patch_and_touch(path, 19 * 1024 * 1024)

    assert store.for_path(path) == {}
    record = cap.evidence_for(
        _discovered(path), store.for_path(path)
    )["coding"]
    assert record.level == cap.INFERRED_NAME


def test_a_record_without_a_full_content_anchor_cannot_be_written(tmp_path):
    path = _model_file(tmp_path)
    store = CapabilityStore(tmp_path / "caps.json")

    # A header digest locates the file; it is not proof of the weights.
    assert store.record(cap.identity_for(path), "coding", cap.USER_CONFIRMED) is False
    assert not (tmp_path / "caps.json").exists()


def test_a_version_1_record_is_ignored_rather_than_trusted(tmp_path):
    path = _model_file(tmp_path)
    identity = cap.identity_for(path)
    store_path = tmp_path / "caps.json"
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "records": {identity.digest: {"coding": {"level": "verified", "source": "old"}}},
            }
        ),
        encoding="utf-8",
    )

    store = CapabilityStore(store_path)

    assert store.for_path(path) == {}
    assert store.for_identity(identity) == {}
    assert cap.evidence_for(_discovered(path), store.for_path(path))["coding"].level == cap.INFERRED_NAME


# ---------------------------------------------------------------------------
# 3. the store: only decisions are persisted
# ---------------------------------------------------------------------------
def test_the_store_refuses_a_guess(tmp_path):
    path = _model_file(tmp_path)
    store = CapabilityStore(tmp_path / "caps.json")

    identity = _content(path)
    assert store.record(identity, "coding", cap.INFERRED_NAME) is False
    assert store.record(identity, "coding", cap.DECLARED) is False
    assert not (tmp_path / "caps.json").exists()


def test_a_stored_record_can_only_strengthen_evidence(tmp_path):
    path = _model_file(tmp_path)
    identity = cap.identity_for(path)
    # A hand-edited store claiming "unknown" must not weaken what the file proves.
    stored = {identity.digest: {"coding": {"level": "unknown", "source": "hand edit"}}}

    record = cap.evidence_for(_discovered(path), stored[identity.digest])["coding"]

    assert record.level == cap.INFERRED_NAME
    assert record.source == "filename"


def test_a_corrupt_store_is_ignored_instead_of_crashing(tmp_path):
    store_path = tmp_path / "caps.json"
    store_path.write_text("{not json", encoding="utf-8")
    store = CapabilityStore(store_path)

    assert store.for_identity("sha256:anything") == {}
    anchor = cap.ModelIdentity(
        digest="sha256:abc", content="sha256:content", complete=True, size_bytes=10
    )
    assert store.record(anchor, "coding", cap.USER_CONFIRMED) is True
    assert json.loads(store_path.read_text(encoding="utf-8"))["records"]["sha256:abc"][
        "capabilities"
    ]


def test_the_store_lives_in_the_profile_not_beside_the_models(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    path = model_dir / "tiny.gguf"
    path.write_bytes(b"GGUF")

    store = CapabilityStore()
    store.record(
        cap.content_identity_for(path), "coding", cap.USER_CONFIRMED, source="user"
    )

    assert store.path == giscode_home() / "capabilities.json"
    assert store.path.parent == giscode_home()
    assert list(model_dir.iterdir()) == [path]  # nothing written beside the model


# ---------------------------------------------------------------------------
# 4. wiring: the manager attaches, records and reports
# ---------------------------------------------------------------------------
class _FakeInference:
    """A planner that always fits, and no runtime at all."""

    available = True

    def plan_load(self, model_path, n_ctx=None, max_gpu_layers=None):
        return {
            "fits": True,
            "reason": "every layer fits in graphics memory",
            "n_ctx": int(n_ctx or 4096),
            "gpu_layers": -1,
            "estimate_bytes": 1024,
        }


class _FakeSettings:
    def get(self, key, default=None):
        return default


def _manager(tmp_path, models):
    registry = ModelRegistry()
    for model in models:
        registry.register(model)
    manager = ModelManager.__new__(ModelManager)
    manager._registry = registry
    manager._settings = _FakeSettings()
    manager._inference = _FakeInference()
    manager._capability_store = CapabilityStore(tmp_path / "caps.json")
    return manager


def test_the_manager_attaches_identity_and_evidence(tmp_path):
    path = _model_file(tmp_path)
    metadata = _discovered(path)
    manager = _manager(tmp_path, [metadata])

    metadata = manager._attach_capability_evidence(metadata)

    assert metadata.identity.startswith("sha256:")
    assert metadata.capability_evidence["coding"]["level"] == cap.INFERRED_NAME
    assert metadata.capability_evidence["coding"]["verified"] is False
    report = {item["capability"]: item for item in manager.capability_report(metadata.name)}
    assert report["text"]["level"] == cap.METADATA


def test_the_manager_records_a_confirmation_and_the_report_upgrades(tmp_path):
    path = _model_file(tmp_path)
    manager = _manager(tmp_path, [_discovered(path)])
    name = path.stem

    assert manager.record_capability(name, "coding", cap.USER_CONFIRMED, detail="owner says it codes") is True
    report = {item["capability"]: item for item in manager.capability_report(name)}

    assert report["coding"]["level"] == cap.USER_CONFIRMED
    # A person's word is actionable, but it is not an independent verification.
    assert report["coding"]["confirmed"] is True
    assert report["coding"]["verified"] is False
    # A guess is still refused, and an unknown model is simply not recorded.
    assert manager.record_capability(name, "coding", cap.INFERRED_NAME) is False
    assert manager.record_capability("no-such-model", "coding", cap.VERIFIED) is False


# ---------------------------------------------------------------------------
# 5. routing: say how the coding capability is known (and stay strict)
# ---------------------------------------------------------------------------
def _route(manager, task_type="coding"):
    return ModelRouter(model_manager=manager).route(
        RoutingRequest(task_id="t1", task_type=task_type, fallback_policy=FallbackPolicy.STRICT)
    )


def test_router_says_how_it_knows_the_model_can_code(tmp_path):
    path = _model_file(tmp_path)
    metadata = _discovered(path)
    manager = _manager(tmp_path, [metadata])
    manager._attach_capability_evidence(metadata)

    result = _route(manager)

    assert result.selected_model == metadata.name
    assert result.metadata["coding_evidence"]["level"] == cap.INFERRED_NAME
    assert result.metadata["coding_evidence"]["verified"] is False


def test_router_reports_unrecorded_evidence_rather_than_claiming_verification(tmp_path):
    path = _model_file(tmp_path)
    metadata = _discovered(path)  # registered without evidence
    manager = _manager(tmp_path, [metadata])

    result = _route(manager)

    assert result.selected_model == metadata.name
    assert result.metadata["coding_evidence"] == {"level": "unrecorded", "source": "", "verified": False}


def _chat_only_model():
    return ModelMetadata(
        name="plain-chat",
        family="general_reasoning",
        path="",
        tags=["chat", "general_reasoning"],
        capabilities=["text", "chat"],
        source="catalog",
    )


def test_a_chat_model_is_still_refused_for_coding(tmp_path):
    manager = _manager(tmp_path, [_chat_only_model()])

    result = _route(manager)

    assert result.selected_model is None
    assert "coding" in result.reason.lower()


def test_a_cross_role_fallback_is_refused_with_the_stage_3g_reason(tmp_path):
    """A policy that crosses into a general role must not smuggle a chat model in."""
    manager = _manager(tmp_path, [_chat_only_model()])

    result = ModelRouter(model_manager=manager).route(
        RoutingRequest(
            task_id="t1",
            task_type="coding",
            fallback_policy=FallbackPolicy.GENERAL_REASONING,
        )
    )

    assert result.selected_model is None
    assert "no model declares coding capability" in result.reason


# ---------------------------------------------------------------------------
# 6. the strict gate is unaffected by the new evidence bookkeeping
# ---------------------------------------------------------------------------
def test_feasibility_still_reports_the_planner_reason(tmp_path):
    path = _model_file(tmp_path)

    verdict = evaluate(_FakeInference(), name=path.stem, path=str(path))

    assert verdict.feasible is True
    assert verdict.gpu_layers == -1


# ---------------------------------------------------------------------------
# 7. confirmation and verification are different claims
# ---------------------------------------------------------------------------
def test_a_user_confirmation_is_not_an_independent_verification():
    record = cap.CapabilityEvidence("coding", cap.USER_CONFIRMED, "user", "owner says so")

    assert record.confirmed is True
    assert record.verified is False
    assert record.as_dict()["confirmed"] is True
    assert record.as_dict()["verified"] is False
    assert "confirmed by the user" in record.describe()

    only_verified = cap.CapabilityEvidence("coding", cap.VERIFIED, "test")
    assert only_verified.verified is True
    assert only_verified.confirmed is True


def test_the_manager_drops_a_record_when_the_weights_change(tmp_path):
    path = _big_model_file(tmp_path, name="Qwen3-Coder-30B-Q3_K_L.gguf")
    metadata = _discovered(path)
    manager = _manager(tmp_path, [metadata])
    assert manager.record_capability(metadata.name, "coding", cap.VERIFIED, source="test") is True
    manager._attach_capability_evidence(metadata)
    assert metadata.capability_evidence["coding"]["level"] == cap.VERIFIED

    _patch_and_touch(path, 19 * 1024 * 1024)

    manager._attach_capability_evidence(metadata)
    assert metadata.capability_evidence["coding"]["level"] == cap.INFERRED_NAME
    assert metadata.capability_evidence["coding"]["verified"] is False


# ---------------------------------------------------------------------------
# 8. a projector is evidence about the model it belongs to
# ---------------------------------------------------------------------------
def _vision_metadata(path, capabilities=("text", "chat", "vision")):
    return ModelMetadata(
        name=path.stem,
        family="discovered",
        path=str(path),
        size_bytes=path.stat().st_size,
        capabilities=list(capabilities),
        source="discovered",
    )


def test_a_projector_named_for_another_model_is_not_evidence(tmp_path):
    mine = _model_file(tmp_path, name="Qwen3-Coder-30B-Q3_K_L.gguf")
    _model_file(tmp_path, name="Qwen3-VL-4B-Instruct-Q4_K_M.gguf")
    (tmp_path / "mmproj-Qwen3-VL-4B-Instruct-f16.gguf").write_bytes(b"projector")

    record = cap.evidence_for(_vision_metadata(mine))["vision"]

    assert record.level == cap.UNKNOWN
    assert "does not match" in record.detail
    assert record.verified is False


def test_a_projector_that_matches_the_model_is_metadata_evidence(tmp_path):
    mine = _model_file(tmp_path, name="Qwen3-VL-4B-Instruct-Q4_K_M.gguf")
    (tmp_path / "mmproj-Qwen3-VL-4B-Instruct-f16.gguf").write_bytes(b"projector")

    record = cap.evidence_for(_vision_metadata(mine))["vision"]

    assert record.level == cap.METADATA
    assert record.source == "mmproj"
    # ...and it says nothing about coding.
    assert cap.evidence_for(_vision_metadata(mine)).get("coding") is None


def test_the_only_projector_in_a_single_model_folder_is_evidence(tmp_path):
    mine = _model_file(tmp_path, name="qwen.gguf")
    (tmp_path / "mmproj-vision-f16.gguf").write_bytes(b"projector")

    assert cap.evidence_for(_vision_metadata(mine))["vision"].level == cap.METADATA


def test_a_model_is_fingerprinted_whole(tmp_path):
    path = _model_file(tmp_path)
    identity = cap.content_identity_for(path)

    assert identity.complete is True
    assert identity.trusted is True
    assert identity.detail == "the whole file"
    before = identity.content

    _patch_and_touch(path, len(path.read_bytes()) - 8)
    assert cap.content_identity_for(path).content != before


# ---------------------------------------------------------------------------
# 4. a record may add a capability the file cannot reveal
# ---------------------------------------------------------------------------
def test_a_confirmed_capability_the_name_cannot_reveal_is_kept(tmp_path):
    """A person confirming coding on an unbranded file must not be dropped."""
    path = _model_file(tmp_path, name="Qwen3-30B-Instruct-Q4_K_M.gguf")
    metadata = _discovered(path, capabilities=("text", "chat"))
    store = CapabilityStore(tmp_path / "caps.json")
    assert store.record(
        cap.content_identity_for(path), "coding", cap.USER_CONFIRMED, source="user"
    ) is True

    records = cap.evidence_for(metadata, store.for_path(path))

    assert records["coding"].level == cap.USER_CONFIRMED
    assert records["coding"].confirmed is True
    assert records["coding"].verified is False


def test_a_weak_stored_level_cannot_add_a_capability(tmp_path):
    """Only a decision the store may hold can introduce one; a guess cannot."""
    path = _model_file(tmp_path, name="Qwen3-30B-Instruct-Q4_K_M.gguf")
    metadata = _discovered(path, capabilities=("text", "chat"))
    stored = {"coding": {"level": cap.INFERRED_NAME, "source": "hand edit"}}

    records = cap.evidence_for(metadata, stored)

    assert "coding" not in records
    assert records["text"].level == cap.METADATA
