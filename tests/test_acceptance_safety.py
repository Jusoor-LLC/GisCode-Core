"""Adversarial tests for the disposable-home boundary.

The acceptance scenarios can delete a browser profile, so the guard has to
reject real profiles, traversal, reparse-point escapes and unmarked homes
*before* anything is modified. These tests drive it with hostile inputs.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from giscode.acceptance_safety import (
    ENV_ALLOW_DESTRUCTIVE,
    ENV_DISPOSABLE,
    ENV_HOME,
    ENV_TOKEN,
    MARKER_NAME,
    DisposableHomeError,
    remove_tree,
    verify_destructive_target,
    verify_disposable_home,
)

TOKEN = "t" * 32


@pytest.fixture
def layout(tmp_path, monkeypatch):
    real = tmp_path / "Users" / "Real"
    real.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(real))
    temp = tmp_path / "Temp"
    temp.mkdir()
    home = temp / "giscode-home"
    home.mkdir()
    (home / MARKER_NAME).write_text(TOKEN, encoding="utf-8")
    return tmp_path, temp, home, real


def env(home, real, *, token=TOKEN, declared=None, extra=None):
    values = {
        ENV_HOME: str(home),
        ENV_DISPOSABLE: str(home if declared is None else declared),
        ENV_TOKEN: token,
        "USERPROFILE": str(real),
    }
    if extra:
        values.update(extra)
    return values


def test_valid_marked_home_is_accepted(layout):
    _tmp, temp, home, real = layout

    assert verify_disposable_home(environ=env(home, real), temp_root=temp) == home.resolve()


def test_missing_home_variable_is_refused(layout):
    _tmp, temp, home, real = layout
    values = env(home, real)
    values.pop(ENV_HOME)

    with pytest.raises(DisposableHomeError, match="GISCODE_HOME"):
        verify_disposable_home(environ=values, temp_root=temp)


def test_missing_disposable_declaration_is_refused(layout):
    _tmp, temp, home, real = layout
    values = env(home, real)
    values.pop(ENV_DISPOSABLE)

    with pytest.raises(DisposableHomeError, match=ENV_DISPOSABLE):
        verify_disposable_home(environ=values, temp_root=temp)


def test_declaration_mismatch_is_refused(layout):
    _tmp, temp, home, real = layout

    with pytest.raises(DisposableHomeError, match="does not match"):
        verify_disposable_home(
            environ=env(home, real, declared=temp / "other-home"), temp_root=temp
        )


def test_real_profile_path_is_refused_even_with_a_marker(layout):
    tmp_path, _temp, _home, real = layout
    profile = real / ".giscode"
    profile.mkdir()
    (profile / MARKER_NAME).write_text(TOKEN, encoding="utf-8")

    with pytest.raises(DisposableHomeError, match="real user profile"):
        verify_disposable_home(environ=env(profile, real), temp_root=tmp_path)


def test_home_directory_itself_is_refused(layout):
    _tmp, temp, _home, real = layout
    (real / MARKER_NAME).write_text(TOKEN, encoding="utf-8")

    with pytest.raises(DisposableHomeError, match="home directory"):
        verify_disposable_home(environ=env(real, real), temp_root=temp.parent)


def test_path_outside_the_temporary_root_is_refused(layout):
    tmp_path, temp, _home, real = layout
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / MARKER_NAME).write_text(TOKEN, encoding="utf-8")

    with pytest.raises(DisposableHomeError, match="temporary root"):
        verify_disposable_home(environ=env(elsewhere, real), temp_root=temp)


def test_temporary_root_itself_is_not_a_home(layout):
    _tmp, temp, _home, real = layout
    (temp / MARKER_NAME).write_text(TOKEN, encoding="utf-8")

    with pytest.raises(DisposableHomeError, match="temporary root"):
        verify_disposable_home(environ=env(temp, real), temp_root=temp)


def test_traversal_string_cannot_escape_into_the_real_profile(layout):
    tmp_path, temp, _home, real = layout
    profile = real / ".giscode"
    profile.mkdir()
    (profile / MARKER_NAME).write_text(TOKEN, encoding="utf-8")
    # The traversal must reach the profile check: the intermediate directory
    # has to exist for the path to resolve to the real profile on every OS.
    (temp / "nested").mkdir()
    sneaky = temp / "nested" / ".." / ".." / "Users" / "Real" / ".giscode"

    with pytest.raises(DisposableHomeError, match="real user profile"):
        verify_disposable_home(environ=env(sneaky, real), temp_root=tmp_path)


def test_short_token_is_refused(layout):
    _tmp, temp, home, real = layout
    (home / MARKER_NAME).write_text("short", encoding="utf-8")

    with pytest.raises(DisposableHomeError, match="shorter than"):
        verify_disposable_home(environ=env(home, real, token="short"), temp_root=temp)


def test_wrong_token_is_refused(layout):
    _tmp, temp, home, real = layout

    with pytest.raises(DisposableHomeError, match="does not match the token"):
        verify_disposable_home(environ=env(home, real, token="x" * 32), temp_root=temp)


def test_missing_marker_is_refused(layout):
    tmp_path, temp, _home, real = layout
    unmarked = temp / "unmarked"
    unmarked.mkdir()

    with pytest.raises(DisposableHomeError, match="no marker"):
        verify_disposable_home(environ=env(unmarked, real), temp_root=temp)


def test_project_checkout_is_refused(layout):
    tmp_path, temp, home, real = layout
    (home / ".git").mkdir()

    with pytest.raises(DisposableHomeError, match="project checkout"):
        verify_disposable_home(environ=env(home, real), temp_root=temp)


def test_reparse_point_home_is_refused(layout):
    tmp_path, temp, _home, real = layout
    target = tmp_path / "target-home"
    target.mkdir()
    (target / MARKER_NAME).write_text(TOKEN, encoding="utf-8")
    link = temp / "junction-home"
    if not sys.platform.startswith("win"):
        pytest.skip("junctions are a Windows filesystem feature")
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0 or not link.exists():
        pytest.skip("junction creation is unavailable in this session")

    with pytest.raises(DisposableHomeError, match="reparse point"):
        verify_disposable_home(environ=env(link, real), temp_root=temp)


def test_destructive_scenario_requires_the_separate_opt_in(layout):
    _tmp, temp, home, real = layout

    with pytest.raises(DisposableHomeError, match=ENV_ALLOW_DESTRUCTIVE):
        verify_disposable_home(
            environ=env(home, real), temp_root=temp, require_destructive=True
        )
    assert verify_disposable_home(
        environ=env(home, real, extra={ENV_ALLOW_DESTRUCTIVE: "1"}),
        temp_root=temp,
        require_destructive=True,
    ) == home.resolve()


def test_delete_target_must_stay_inside_the_home(layout):
    tmp_path, _temp, home, _real = layout
    inside = home / "chatgpt_browser"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    assert verify_destructive_target(inside, home) == inside
    with pytest.raises(DisposableHomeError, match="outside"):
        verify_destructive_target(outside, home)
    with pytest.raises(DisposableHomeError, match="home itself"):
        verify_destructive_target(home, home)


def test_remove_tree_never_follows_a_link_out(layout):
    tmp_path, _temp, home, _real = layout
    outside = tmp_path / "keep"
    outside.mkdir()
    (outside / "important.txt").write_text("keep", encoding="utf-8")
    link = home / "linked"
    if not sys.platform.startswith("win"):
        pytest.skip("junctions are a Windows filesystem feature")
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0 or not link.exists():
        pytest.skip("junction creation is unavailable in this session")

    # A junction is refused either because it is a reparse point or because its
    # canonical target is outside the home; both are safe and both must leave
    # the linked directory untouched.
    with pytest.raises(DisposableHomeError):
        remove_tree(link, home)
    assert (outside / "important.txt").read_text(encoding="utf-8") == "keep"
