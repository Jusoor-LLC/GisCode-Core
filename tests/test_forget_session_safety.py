"""H1 regression: acceptance Forget Session must not delete a real profile."""
from __future__ import annotations

from pathlib import Path

import pytest

from giscode.acceptance_safety import ENV_DISPOSABLE, ENV_HOME, MARKER_NAME
from giscode.browser.host import BrowserHostError, ChatGptBrowserHost


def _host(profile: Path):
    return ChatGptBrowserHost(profile_root=profile)


def test_acceptance_forget_without_disposable_home_refuses_real_profile(
    tmp_path, monkeypatch
):
    real_profile = tmp_path / "real-user" / ".giscode" / "chatgpt_browser"
    real_profile.mkdir(parents=True)
    sentinel = real_profile / "cookie-store"
    sentinel.write_text("real sign-in data", encoding="utf-8")
    monkeypatch.setenv("GISCODE_ACCEPTANCE_SCENARIO", "interactive")
    monkeypatch.delenv(ENV_DISPOSABLE, raising=False)

    with pytest.raises(BrowserHostError, match="disposable home"):
        _host(real_profile).clear_session()

    assert sentinel.read_text(encoding="utf-8") == "real sign-in data"


def test_acceptance_forget_rejects_a_declared_real_profile(tmp_path, monkeypatch):
    real_profile = tmp_path / "real-user" / ".giscode" / "chatgpt_browser"
    real_profile.mkdir(parents=True)
    (real_profile / MARKER_NAME).write_text("t" * 32, encoding="utf-8")
    sentinel = real_profile / "cookie-store"
    sentinel.write_text("real sign-in data", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real-user"))
    monkeypatch.setenv(ENV_HOME, str(real_profile))
    monkeypatch.setenv(ENV_DISPOSABLE, str(real_profile))
    monkeypatch.setenv("GISCODE_ACCEPTANCE_SCENARIO", "interactive")
    monkeypatch.setenv("GISCODE_ACCEPTANCE_DISPOSABLE_TOKEN", "t" * 32)

    with pytest.raises(BrowserHostError):
        _host(real_profile).clear_session()

    assert sentinel.read_text(encoding="utf-8") == "real sign-in data"


def test_ordinary_app_forget_still_removes_its_owned_profile(tmp_path, monkeypatch):
    profile = tmp_path / "owned-profile"
    profile.mkdir()
    (profile / "cookie-store").write_text("owned", encoding="utf-8")
    monkeypatch.delenv("GISCODE_ACCEPTANCE_SCENARIO", raising=False)
    monkeypatch.delenv(ENV_DISPOSABLE, raising=False)

    _host(profile).clear_session()

    assert not profile.exists()
