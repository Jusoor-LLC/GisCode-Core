"""Regression: a refused acceptance run must not touch the profile at all.

The earlier behaviour validated the scenario only after ``bootstrap()`` had
created the profile, opened the knowledge database and written a log line, so a
refused run left ``knowledge.db-shm``/``-wal`` behind and appended to
``logs\\giscode.log``. These tests pin the refusal to the entry point, where no
persistent state exists yet.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import pytest

from giscode import main
from giscode.acceptance_runtime import preflight
from giscode.acceptance_safety import DisposableHomeError

TOKEN = "t" * 32


def _inventory(root: Path) -> dict[str, tuple[int, str]]:
    """Every file under *root* with its size and digest."""
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            found[str(path.relative_to(root))] = (
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return found


def _seed_real_profile(root: Path) -> Path:
    profile = root / "real-user" / ".giscode"
    (profile / "logs").mkdir(parents=True)
    (profile / "logs" / "giscode.log").write_text("existing log\n", encoding="utf-8")
    (profile / "config.json").write_text('{"app": {"version": "0.1.1"}}', encoding="utf-8")
    (profile / "chats.db").write_bytes(b"SQLite format 3\x00existing")
    return profile


def _armed(monkeypatch, *, home: Path, scenario: str = "interactive",
           destructive: bool = True, evidence: str = "refusal.json") -> None:
    monkeypatch.setenv("GISCODE_ACCEPTANCE_OPT_IN", "1")
    monkeypatch.setenv("GISCODE_ACCEPTANCE_SCENARIO", scenario)
    monkeypatch.setenv("GISCODE_ACCEPTANCE_EVIDENCE", evidence)
    monkeypatch.setenv("GISCODE_HOME", str(home))
    monkeypatch.setenv("GISCODE_ACCEPTANCE_DISPOSABLE_HOME", str(home))
    monkeypatch.setenv("GISCODE_ACCEPTANCE_DISPOSABLE_TOKEN", TOKEN)
    if destructive:
        monkeypatch.setenv("GISCODE_ACCEPTANCE_ALLOW_DESTRUCTIVE", "1")
    else:
        monkeypatch.delenv("GISCODE_ACCEPTANCE_ALLOW_DESTRUCTIVE", raising=False)
    monkeypatch.setattr(sys, main.ACCEPTANCE_BUILD_MARKER, True, raising=False)


def test_refusal_for_real_profile_leaves_it_byte_for_byte_unchanged(tmp_path, monkeypatch):
    profile = _seed_real_profile(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real-user"))
    _armed(monkeypatch, home=profile)
    before = _inventory(tmp_path / "real-user")

    reason = main._acceptance_refusal_reason()

    assert reason is not None
    assert "real user profile" in reason
    assert _inventory(tmp_path / "real-user") == before


def test_refusal_for_outside_temp_home_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "temp-root"))
    (tmp_path / "temp-root").mkdir()
    profile = _seed_real_profile(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real-user"))
    _armed(monkeypatch, home=profile)
    before = _inventory(tmp_path)

    reason = main._acceptance_refusal_reason()

    assert reason == "the disposable home is outside the temporary root"
    assert _inventory(tmp_path) == before


def test_refusal_without_destructive_opt_in_writes_nothing(tmp_path, monkeypatch):
    home = tmp_path / "disposable"
    home.mkdir()
    (home / ".giscode-acceptance-disposable").write_text(TOKEN, encoding="utf-8")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real-user"))
    (tmp_path / "real-user").mkdir()
    _armed(monkeypatch, home=home, destructive=False)
    before = _inventory(tmp_path)

    reason = main._acceptance_refusal_reason()

    assert reason is not None and "ALLOW_DESTRUCTIVE" in reason
    assert _inventory(tmp_path) == before


def test_refusal_for_invalid_evidence_name_writes_nothing(tmp_path, monkeypatch):
    home = tmp_path / "disposable"
    home.mkdir()
    (home / ".giscode-acceptance-disposable").write_text(TOKEN, encoding="utf-8")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real-user"))
    (tmp_path / "real-user").mkdir()
    _armed(monkeypatch, home=home, evidence="../escape.json")

    assert main._acceptance_refusal_reason() == "the evidence path is not valid"
    assert not (tmp_path / "giscode_acceptance").exists()


def test_refusal_for_unknown_scenario(tmp_path, monkeypatch):
    home = tmp_path / "disposable"
    home.mkdir()
    (home / ".giscode-acceptance-disposable").write_text(TOKEN, encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real-user"))
    (tmp_path / "real-user").mkdir()
    _armed(monkeypatch, home=home, scenario="not-a-scenario")

    assert main._acceptance_refusal_reason() == "unknown scenario"


def test_valid_disposable_home_is_not_refused(tmp_path, monkeypatch):
    home = tmp_path / "disposable"
    home.mkdir()
    (home / ".giscode-acceptance-disposable").write_text(TOKEN, encoding="utf-8")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real-user"))
    (tmp_path / "real-user").mkdir()
    _armed(monkeypatch, home=home)

    assert main._acceptance_refusal_reason() is None
    # Preflight must not create the evidence root either.
    assert not (tmp_path / "giscode_acceptance").exists()


def test_production_build_never_refuses(tmp_path, monkeypatch):
    home = _seed_real_profile(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real-user"))
    _armed(monkeypatch, home=home)
    monkeypatch.setattr(sys, main.ACCEPTANCE_BUILD_MARKER, False, raising=False)

    assert main._acceptance_refusal_reason() is None


def test_preflight_raises_instead_of_exiting(tmp_path, monkeypatch):
    """The entry-point gate reports a reason, so callers stay testable."""
    home = _seed_real_profile(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real-user"))
    _armed(monkeypatch, home=home)

    with pytest.raises(DisposableHomeError):
        preflight("interactive", main._acceptance_evidence_name)
