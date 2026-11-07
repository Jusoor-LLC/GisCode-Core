"""The capability store under concurrency, crashes and bad data.

A capability record is a claim about one specific set of weights. Three things
have to hold for it to be worth anything:

* two GisCode processes writing at once must not lose each other's records
  (a plain read-modify-write does exactly that);
* a write that fails must leave the previous store intact - a half-written
  JSON file would throw away every record;
* a record that cannot be re-validated against the model file must never be
  read as if it had been.

These tests use real child processes for the locking tests, because a thread
lock cannot demonstrate anything about two processes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from giscode.models import capability as cap
from giscode.models.capability_store import (
    LOCK_TIMEOUT_SECONDS,
    READ_LOCK_TIMEOUT_SECONDS,
    STORE_VERSION,
    CapabilityStore,
    _ProcessLock,
)

ROOT = Path(__file__).resolve().parents[1]

#: Writes one record, optionally with the atomic replace slowed down so several
#: children overlap on the read-modify-write, which is what loses updates when
#: there is no lock.
CHILD = """
import sys, time
from pathlib import Path
from giscode.models import capability as cap
from giscode.models.capability_store import CapabilityStore

store_path, model_path, capability, level, delay = sys.argv[1:6]
if float(delay):
    original = CapabilityStore._atomic_write
    def slow(self, data):
        time.sleep(float(delay))
        return original(self, data)
    CapabilityStore._atomic_write = slow

store = CapabilityStore(Path(store_path), lock_timeout=30)
identity = cap.content_identity_for(model_path)
ok = store.record(identity, capability, level, source="child")
print("saved" if ok else "refused")
"""

#: Takes the write lock and exits without releasing it - what a killed or
#: crashed writer looks like to the next process.
HOLDER = """
import sys
from pathlib import Path
from giscode.models.capability_store import _ProcessLock

lock = _ProcessLock(Path(sys.argv[1]), timeout=10)
print("held" if lock.acquire() else "busy", flush=True)
"""


def _model(path: Path, size: int = 4096) -> Path:
    # Distinct content per file name: two models with identical bytes are, by
    # design, one identity and one record.
    tag = path.name.encode("utf-8")[:8].ljust(8, b"#")
    path.write_bytes(b"GGUF" + tag + b"\0" * (size - 12))
    return path


def _env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    env["GISCODE_HOME"] = str(tmp_path / "home")
    env["GISCODE_TEST_MODE"] = "1"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _spawn(script: str, *arguments: str, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *arguments],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _run(script: str, *arguments: str, env: dict, timeout: float = 120) -> str:
    result = subprocess.run(
        [sys.executable, "-c", script, *arguments],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# 1. concurrency: no lost updates, readers never see a partial file
# ---------------------------------------------------------------------------
def test_two_processes_writing_different_records_keep_both(tmp_path):
    store_path = tmp_path / "caps.json"
    first_model = _model(tmp_path / "one.gguf")
    second_model = _model(tmp_path / "two.gguf")
    env = _env(tmp_path)

    children = [
        _spawn(CHILD, str(store_path), str(first_model), "coding", "verified", "0.25", env=env),
        _spawn(CHILD, str(store_path), str(second_model), "coding", "verified", "0.25", env=env),
    ]
    outputs = [child.communicate(timeout=120)[0].strip() for child in children]

    assert outputs == ["saved", "saved"]
    stored = CapabilityStore(store_path).entries()
    assert len(stored) == 2
    assert all(entry["capabilities"]["coding"]["level"] == "verified" for entry in stored.values())


def test_concurrent_updates_to_one_record_keep_every_capability(tmp_path):
    store_path = tmp_path / "caps.json"
    model = _model(tmp_path / "model.gguf")
    env = _env(tmp_path)
    capabilities = ["coding", "vision", "reasoning", "tool_use"]

    children = [
        _spawn(CHILD, str(store_path), str(model), name, "user_confirmed", "0.2", env=env)
        for name in capabilities
    ]
    outputs = [child.communicate(timeout=120)[0].strip() for child in children]

    assert outputs == ["saved"] * len(capabilities)
    stored = CapabilityStore(store_path).entries()
    assert len(stored) == 1
    entry = next(iter(stored.values()))
    assert sorted(entry["capabilities"]) == sorted(capabilities)


def test_a_reader_never_sees_a_half_written_store(tmp_path):
    store_path = tmp_path / "caps.json"
    model = _model(tmp_path / "model.gguf")
    env = _env(tmp_path)
    writer = _spawn(
        CHILD + "\nfor index in range(8): store.record(identity, 'cap%d' % index, 'verified')",
        str(store_path),
        str(model),
        "coding",
        "verified",
        "0.05",
        env=env,
    )

    seen = 0
    unopenable = 0
    while writer.poll() is None:
        if store_path.exists():
            try:
                payload = store_path.read_text(encoding="utf-8")
            except OSError:
                # The name was being replaced at that instant. Not a torn read:
                # the next attempt sees a whole store.
                unopenable += 1
                continue
            data = json.loads(payload)  # a partial write would raise here
            assert data["version"] == STORE_VERSION
            assert isinstance(data["records"], dict)
            seen += 1
        time.sleep(0.01)
    output = writer.communicate(timeout=60)[0].strip()

    assert output == "saved"
    assert seen > 0
    # Every write landed: the reader never made the writer lose a record.
    stored = CapabilityStore(store_path).entries()
    assert len(stored) == 1
    assert len(next(iter(stored.values()))["capabilities"]) == 9


def test_a_lock_held_by_a_dead_writer_does_not_wedge_the_store(tmp_path):
    store_path = tmp_path / "caps.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    env = _env(tmp_path)

    output = _run(HOLDER, str(store_path) + ".lock", env=env)
    assert output == "held"  # the process has exited here

    store = CapabilityStore(store_path, lock_timeout=2)
    identity = cap.content_identity_for(_model(tmp_path / "model.gguf"))

    started = time.monotonic()
    assert store.record(identity, "coding", "verified") is True
    assert time.monotonic() - started < 2  # the OS released it


def test_a_write_times_out_instead_of_clobbering_a_held_lock(tmp_path):
    store_path = tmp_path / "caps.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    identity = cap.content_identity_for(_model(tmp_path / "model.gguf"))
    locked = _ProcessLock(store_path.with_name(store_path.name + ".lock"), timeout=1)
    assert locked.acquire() is True
    try:
        store = CapabilityStore(store_path, lock_timeout=0.2)
        assert store.record(identity, "coding", "verified") is False
    finally:
        locked.release()

    assert not store_path.exists()


# ---------------------------------------------------------------------------
# 2. persistence: atomic, and the previous store survives a failure
# ---------------------------------------------------------------------------
def test_a_failed_replace_leaves_the_previous_store_intact(tmp_path, monkeypatch):
    store_path = tmp_path / "caps.json"
    model = _model(tmp_path / "model.gguf")
    store = CapabilityStore(store_path)
    assert store.record(cap.content_identity_for(model), "coding", "verified") is True
    before = store_path.read_bytes()

    def refuse(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("giscode.models.capability_store.os.replace", refuse)

    other = _model(tmp_path / "other.gguf")
    assert CapabilityStore(store_path).record(
        cap.content_identity_for(other), "coding", "verified"
    ) is False

    assert store_path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir() if ".tmp" in item.name) == []


def test_a_write_leaves_no_temporary_files_behind(tmp_path):
    store_path = tmp_path / "caps.json"
    store = CapabilityStore(store_path)

    for index in range(3):
        model = _model(tmp_path / f"model{index}.gguf")
        assert store.record(cap.content_identity_for(model), "coding", "verified") is True

    assert [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")] == []
    assert json.loads(store_path.read_text(encoding="utf-8"))["version"] == STORE_VERSION


# ---------------------------------------------------------------------------
# 3. corruption: never trusted, never the only copy lost
# ---------------------------------------------------------------------------
def test_a_corrupt_store_is_quarantined_and_never_trusted(tmp_path):
    store_path = tmp_path / "caps.json"
    store_path.write_text("{not json", encoding="utf-8")
    store = CapabilityStore(store_path)

    assert store.for_identity("sha256:anything") == {}
    quarantined = [item for item in tmp_path.iterdir() if ".corrupt-" in item.name]
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not json"


def test_one_bad_record_does_not_hide_the_good_ones(tmp_path):
    model = _model(tmp_path / "model.gguf")
    identity = cap.content_identity_for(model)
    store_path = tmp_path / "caps.json"
    store_path.write_text(
        json.dumps(
            {
                "version": STORE_VERSION,
                "records": {
                    identity.digest: {
                        "content": identity.content,
                        "complete": True,
                        "size": 4096,
                        "capabilities": {"coding": {"level": "verified", "source": "test"}},
                    },
                    "sha256:broken": {
                        "content": "not-a-digest",
                        "complete": True,
                        "capabilities": {"coding": {"level": "verified"}},
                    },
                    "sha256:guessing": {
                        "content": "sha256:" + "0" * 64,
                        "complete": True,
                        "capabilities": {"coding": {"level": "inferred_name"}},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    store = CapabilityStore(store_path)

    assert sorted(store.entries()) == [identity.digest]
    assert store.for_path(model)["coding"]["level"] == "verified"


def test_duplicate_content_identities_are_collapsed(tmp_path):
    store_path = tmp_path / "caps.json"
    shared = "sha256:" + "a" * 64
    record = {
        "content": shared,
        "complete": True,
        "size": 10,
        "capabilities": {"coding": {"level": "verified", "source": "test"}},
    }
    store_path.write_text(
        json.dumps(
            {
                "version": STORE_VERSION,
                "records": {"sha256:one": dict(record), "sha256:two": dict(record)},
            }
        ),
        encoding="utf-8",
    )

    assert len(CapabilityStore(store_path).entries()) == 1


# ---------------------------------------------------------------------------
# 4. schema migration: old records are never promoted
# ---------------------------------------------------------------------------
def _legacy_store(tmp_path, version=2) -> tuple[Path, dict]:
    store_path = tmp_path / "caps.json"
    store_path.write_text(
        json.dumps(
            {
                "version": version,
                "records": {
                    "sha256:legacy": {
                        "body": "sha256:" + "b" * 64,
                        "complete": False,
                        "capabilities": {"coding": {"level": "verified", "source": "old"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return store_path, json.loads(store_path.read_text(encoding="utf-8"))


def test_an_older_store_is_backed_up_and_not_trusted(tmp_path):
    store_path, original = _legacy_store(tmp_path)
    model = _model(tmp_path / "model.gguf")
    store = CapabilityStore(store_path)

    assert store.for_path(model) == {}  # a sampled legacy record proves nothing
    assert store.for_identity("sha256:legacy") == {}  # nor is it readable as a claim
    assert store_path.read_text(encoding="utf-8") == json.dumps(original)

    assert store.record(cap.content_identity_for(model), "coding", "user_confirmed") is True

    backup = tmp_path / "caps.json.v2.bak"
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    written = json.loads(store_path.read_text(encoding="utf-8"))
    assert written["version"] == STORE_VERSION
    assert "sha256:legacy" not in written["records"]
    assert store.for_path(model)["coding"]["level"] == "user_confirmed"


def test_migration_is_repeatable_and_only_backs_up_once(tmp_path):
    store_path, _original = _legacy_store(tmp_path)
    store = CapabilityStore(store_path)
    first = _model(tmp_path / "first.gguf")
    second = _model(tmp_path / "second.gguf")

    assert store.record(cap.content_identity_for(first), "coding", "verified") is True
    backup = tmp_path / "caps.json.v2.bak"
    stamp = backup.stat().st_mtime_ns

    assert store.record(cap.content_identity_for(second), "vision", "verified") is True

    assert backup.stat().st_mtime_ns == stamp  # not overwritten
    assert len([item for item in tmp_path.iterdir() if ".v2.bak" in item.name]) == 1
    assert len(CapabilityStore(store_path).entries()) == 2


def test_an_interrupted_migration_leaves_the_old_store_and_can_be_retried(tmp_path, monkeypatch):
    store_path, original = _legacy_store(tmp_path)
    model = _model(tmp_path / "model.gguf")
    store = CapabilityStore(store_path)
    identity = cap.content_identity_for(model)

    def refuse(*args, **kwargs):
        raise OSError(5, "I/O error")

    monkeypatch.setattr("giscode.models.capability_store.os.replace", refuse)
    assert store.record(identity, "coding", "verified") is False
    assert json.loads(store_path.read_text(encoding="utf-8")) == original
    assert (tmp_path / "caps.json.v2.bak").exists()

    monkeypatch.undo()
    assert store.record(identity, "coding", "verified") is True
    assert json.loads(store_path.read_text(encoding="utf-8"))["version"] == STORE_VERSION


def test_a_newer_store_is_not_overwritten(tmp_path):
    store_path = tmp_path / "caps.json"
    newer = {"version": 99, "records": {"sha256:future": {"content": "sha256:x"}}}
    store_path.write_text(json.dumps(newer), encoding="utf-8")
    model = _model(tmp_path / "model.gguf")

    store = CapabilityStore(store_path)

    assert store.record(cap.content_identity_for(model), "coding", "verified") is False
    assert json.loads(store_path.read_text(encoding="utf-8")) == newer


# ---------------------------------------------------------------------------
# 5. evidence is not promoted on the way through the store
# ---------------------------------------------------------------------------
def test_a_user_confirmation_is_stored_and_read_back_as_one(tmp_path):
    model = _model(tmp_path / "model.gguf")
    identity = cap.content_identity_for(model)
    store = CapabilityStore(tmp_path / "caps.json")

    assert store.record(identity, "coding", "user_confirmed", source="user") is True

    stored = store.for_path(model)["coding"]
    assert stored["level"] == "user_confirmed"
    assert stored["level"] != "verified"


def test_weak_levels_are_never_written_even_by_hand(tmp_path):
    model = _model(tmp_path / "model.gguf")
    identity = cap.content_identity_for(model)
    store = CapabilityStore(tmp_path / "caps.json")

    for level in ("inferred_name", "metadata", "declared", "unknown"):
        assert store.record(identity, "coding", level) is False


def test_the_store_never_writes_beside_the_models(tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    model = _model(model_dir / "tiny.gguf")

    store = CapabilityStore(tmp_path / "home" / "caps.json")
    assert store.record(cap.content_identity_for(model), "coding", "verified") is True

    assert sorted(item.name for item in model_dir.iterdir()) == [model.name]


def test_a_record_without_whole_file_proof_is_ignored(tmp_path):
    """A hand-edited record cannot claim a sampled or partial identity."""
    model = _model(tmp_path / "model.gguf")
    identity = cap.identity_for(model)
    store_path = tmp_path / "caps.json"
    store_path.write_text(
        json.dumps(
            {
                "version": STORE_VERSION,
                "records": {
                    identity.digest: {
                        "content": identity.digest,  # header digest, not content
                        "complete": False,
                        "capabilities": {"coding": {"level": "verified"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = CapabilityStore(store_path)

    assert store.status_for_path(model)["state"] == "unrecorded"
    assert store.for_path(model) == {}


# ---------------------------------------------------------------------------
# 6. a failed read is not the same as an empty store
# ---------------------------------------------------------------------------
def _refuse_reads(monkeypatch, store_path: Path) -> None:
    """Make reading one file fail the way a sharing violation or a bad sector does."""
    original = Path.read_text

    def unreadable(self, *args, **kwargs):
        if Path(self) == store_path:
            raise OSError(5, "simulated I/O error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)


def test_a_failed_read_is_not_remembered_as_an_empty_store(tmp_path, monkeypatch):
    """A transient read error must not hide every record until the file changes."""
    store_path = tmp_path / "caps.json"
    model = _model(tmp_path / "model.gguf")
    identity = cap.content_identity_for(model)
    assert CapabilityStore(store_path).record(identity, "coding", "verified") is True
    before = store_path.read_bytes()

    store = CapabilityStore(store_path)  # nothing read yet, so nothing cached
    _refuse_reads(monkeypatch, store_path)

    assert store.entries() == {}  # "no information", not "no records"
    monkeypatch.undo()

    # The empty view was not cached: the record is visible again immediately.
    assert "coding" in store.for_identity(identity.digest)
    assert store_path.read_bytes() == before


def test_a_write_is_refused_rather_than_derived_from_a_failed_read(tmp_path, monkeypatch):
    """Rewriting from an unreadable view would replace every record with one."""
    store_path = tmp_path / "caps.json"
    first = _model(tmp_path / "first.gguf")
    second = _model(tmp_path / "second.gguf")
    assert CapabilityStore(store_path).record(
        cap.content_identity_for(first), "coding", "verified"
    ) is True
    before = store_path.read_bytes()

    store = CapabilityStore(store_path)
    _refuse_reads(monkeypatch, store_path)

    assert store.record(cap.content_identity_for(second), "vision", "verified") is False

    monkeypatch.undo()
    assert store_path.read_bytes() == before  # untouched, not a one-record store
    assert len(CapabilityStore(store_path).entries()) == 1


def test_forget_refuses_when_the_store_cannot_be_read(tmp_path, monkeypatch):
    store_path = tmp_path / "caps.json"
    model = _model(tmp_path / "model.gguf")
    identity = cap.content_identity_for(model)
    assert CapabilityStore(store_path).record(identity, "coding", "verified") is True
    before = store_path.read_bytes()

    store = CapabilityStore(store_path)
    _refuse_reads(monkeypatch, store_path)

    assert store.forget(identity.digest) is False

    monkeypatch.undo()
    assert store_path.read_bytes() == before


def test_a_store_that_is_not_utf8_is_quarantined_instead_of_crashing(tmp_path):
    """Invalid bytes used to raise out of the read into model discovery."""
    store_path = tmp_path / "caps.json"
    payload = b"\xff\xfe\x00{\x00not a store"
    store_path.write_bytes(payload)

    store = CapabilityStore(store_path)

    assert store.entries() == {}
    assert store.for_path(tmp_path / "missing.gguf") == {}
    quarantined = [item for item in tmp_path.iterdir() if ".corrupt-" in item.name]
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == payload


def test_a_reader_does_not_wait_the_writer_timeout(tmp_path):
    """Discovery reads the store once per model, so a read must not wait 10 s."""
    store_path = tmp_path / "caps.json"
    model = _model(tmp_path / "model.gguf")
    assert CapabilityStore(store_path).record(
        cap.content_identity_for(model), "coding", "verified"
    ) is True

    assert READ_LOCK_TIMEOUT_SECONDS < LOCK_TIMEOUT_SECONDS
    locked = _ProcessLock(store_path.with_name(store_path.name + ".lock"), timeout=5)
    assert locked.acquire() is True
    try:
        reader = CapabilityStore(store_path, read_lock_timeout=0.05)
        started = time.monotonic()
        records = reader.entries()
        elapsed = time.monotonic() - started
    finally:
        locked.release()

    assert records != {}  # an atomic update can be read without the lock
    assert elapsed < 3.0
