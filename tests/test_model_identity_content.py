"""Model identity is a content identity, not a sample.

Stage 3I anchored capability records to the header digest plus a *sampled*
fingerprint of the rest of the file. A sample cannot be a content identity: a
change confined to the gaps between windows leaves it untouched, and the code
claimed a guarantee ("replacing the weights produces a different identity") that
it could not keep.

These tests pin the stronger rule:

* the header digest is an index key and nothing more;
* a full-file SHA-256 is what a record is anchored to;
* renaming or moving an unchanged file keeps the identity, changing any byte
  does not;
* a partial, growing, unreadable or concurrently-modified file never earns a
  trusted identity;
* the file is hashed in bounded memory, once per process, and nothing is
  written beside the model.
"""
from __future__ import annotations

import os
import tracemalloc

from giscode.models import capability as cap


def _model_file(tmp_path, name="Qwen3-Coder-30B-Q3_K_L.gguf", size=4096):
    path = tmp_path / name
    path.write_bytes(b"GGUF" + b"\0" * (size - 4))
    return path


def _patch(path, offset, payload=b"\xff" * 64):
    with path.open("r+b") as handle:
        handle.seek(offset)
        handle.write(payload)


# ---------------------------------------------------------------------------
# 1. the header digest is an index key, never proof of content
# ---------------------------------------------------------------------------
def test_the_header_digest_is_not_a_trusted_identity(tmp_path):
    identity = cap.identity_for(_model_file(tmp_path))

    assert identity.known is True
    assert identity.trusted is False
    assert identity.content == ""
    assert identity.complete is False


def test_a_full_identity_says_what_it_read(tmp_path):
    identity = cap.content_identity_for(_model_file(tmp_path))

    assert identity.trusted is True
    assert identity.complete is True
    assert identity.detail == "the whole file"
    assert identity.content.startswith("sha256:")
    assert identity.size_bytes == 4096


# ---------------------------------------------------------------------------
# 2. unchanged content keeps its identity; any change loses it
# ---------------------------------------------------------------------------
def test_an_unchanged_file_keeps_its_content_identity(tmp_path):
    path = _model_file(tmp_path)

    first = cap.content_identity_for(path)
    second = cap.content_identity_for(path)

    assert first == second


def test_a_rename_keeps_the_content_identity(tmp_path):
    path = _model_file(tmp_path)
    before = cap.content_identity_for(path)

    moved = tmp_path / "moved" / "renamed.gguf"
    moved.parent.mkdir()
    path.replace(moved)
    after = cap.content_identity_for(moved)

    assert after.content == before.content
    assert after.digest == before.digest


def test_a_change_the_header_cannot_see_changes_the_content_identity(tmp_path):
    """Same size, same first 8 MiB, different tensor data."""
    path = _model_file(tmp_path, size=20 * 1024 * 1024)
    header_before = cap.identity_for(path)
    before = cap.content_identity_for(path)

    _patch(path, 19 * 1024 * 1024)

    assert cap.identity_for(path).digest == header_before.digest  # still blind
    after = cap.content_identity_for(path)
    assert after.content != before.content
    assert after.digest == header_before.digest  # the index key did not move


def test_a_replaced_model_changes_the_content_identity(tmp_path):
    path = _model_file(tmp_path)
    before = cap.content_identity_for(path)

    path.write_bytes(b"GGUF" + b"\1" * (8192 - 4))  # re-quantized, new file

    after = cap.content_identity_for(path)
    assert after.content != before.content
    assert after.digest != before.digest


def test_a_truncated_model_is_not_the_recorded_one(tmp_path):
    path = _model_file(tmp_path, size=8192)
    before = cap.content_identity_for(path)

    with path.open("r+b") as handle:
        handle.truncate(4096)  # interrupted download

    after = cap.content_identity_for(path)
    assert after.trusted is True  # a truncated file is still readable...
    assert after.content != before.content  # ...but it is a different file
    assert after.size_bytes == 4096


# ---------------------------------------------------------------------------
# 3. never a trusted identity for a file that is not settled
# ---------------------------------------------------------------------------
def test_a_file_modified_while_it_is_hashed_has_no_trusted_identity(tmp_path, monkeypatch):
    path = _model_file(tmp_path, size=16 * 1024 * 1024)
    real_open = open
    state = {"appended": False}

    class _Handle:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            data = self._handle.read(size)
            if data and not state["appended"]:
                state["appended"] = True
                with real_open(path, "ab") as extra:
                    extra.write(b"x" * 4096)
            return data

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *exception):
            return self._handle.__exit__(*exception)

        def __getattr__(self, name):
            return getattr(self._handle, name)

    def fake_open(target, mode="rb", *args, **kwargs):
        handle = real_open(target, mode, *args, **kwargs)
        if "r" in mode and "b" in mode and str(target) == str(path):
            return _Handle(handle)
        return handle

    monkeypatch.setattr(cap, "open", fake_open, raising=False)

    identity = cap.content_identity_for(path)

    assert state["appended"] is True
    assert identity.trusted is False
    assert identity.content == ""


def test_an_unreadable_model_has_no_identity(tmp_path):
    missing = cap.content_identity_for(tmp_path / "not-there.gguf")
    directory = tmp_path / "a-directory.gguf"
    directory.mkdir()

    assert missing.known is False and missing.trusted is False
    assert cap.content_identity_for(directory).known is False
    assert cap.content_identity_for("").known is False


# ---------------------------------------------------------------------------
# 4. performance shape: bounded memory, one hash per unchanged file
# ---------------------------------------------------------------------------
def test_a_large_file_is_hashed_without_loading_it(tmp_path):
    path = tmp_path / "large.gguf"
    with path.open("wb") as handle:
        for _ in range(16):
            handle.write(b"\0" * (4 * 1024 * 1024))  # 64 MiB

    tracemalloc.start()
    try:
        identity = cap.content_identity_for(path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert identity.trusted is True
    assert identity.size_bytes == 64 * 1024 * 1024
    # One 4 MiB chunk at a time; nowhere near the file's size.
    assert peak < 24 * 1024 * 1024


def test_an_unchanged_file_is_hashed_once_per_process(tmp_path, monkeypatch):
    path = _model_file(tmp_path)
    first = cap.content_identity_for(path)
    real_open = open

    def refuse(target, mode="rb", *args, **kwargs):
        if "r" in mode and "b" in mode and str(target) == str(path):
            raise AssertionError("the file was read again although it did not change")
        return real_open(target, mode, *args, **kwargs)

    monkeypatch.setattr(cap, "open", refuse, raising=False)

    assert cap.content_identity_for(path) == first


def test_the_hash_cache_is_invalidated_even_when_the_mtime_is_restored(tmp_path):
    """A writer can put the mtime back; the change time still moves."""
    path = _model_file(tmp_path, size=8192)
    before = cap.content_identity_for(path)
    stat = path.stat()

    _patch(path, 4096)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # mtime restored

    after = cap.content_identity_for(path)
    assert after.content != before.content


def test_nothing_is_written_beside_the_model(tmp_path):
    path = _model_file(tmp_path)
    cap.content_identity_for(path)

    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]
