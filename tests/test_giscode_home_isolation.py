"""The suite must never write the developer's real ``~/.giscode`` profile.

``SettingsManager()`` resolves its file through ``giscode_home()``, which is
``~/.giscode`` unless ``GISCODE_HOME`` says otherwise. Until 2026-10-27 nothing
redirected it, so ``test_resource_budget.py``'s window-title test wrote
``"selected_model": "Broken"`` into the real ``~/.giscode/config.json``; the
``_isolate_giscode_home`` fixture in ``tests/conftest.py`` now prevents that.

These tests fail loudly if that fixture is ever removed or bypassed.
"""
from pathlib import Path

from giscode.config.paths import giscode_home, giscode_home_configured, profile_root
from giscode.config.settings import SettingsManager

REAL_HOME = Path.home() / ".giscode"


def test_pytest_profile_is_not_the_real_one():
    assert giscode_home_configured() is True, "GISCODE_HOME must be set during pytest"
    assert giscode_home() != REAL_HOME, f"tests are pointed at the real profile: {giscode_home()}"


def test_isolated_leaf_folder_is_named_giscode():
    assert giscode_home().name == ".giscode"


def test_a_default_settings_manager_resolves_inside_the_isolated_profile():
    settings = SettingsManager()
    config_path = Path(settings.manager.config_path)
    assert config_path.parent == giscode_home()
    assert config_path.parent.name == ".giscode"
    assert config_path != REAL_HOME / "config.json"


def test_a_settings_write_lands_in_the_isolated_profile():
    settings = SettingsManager()
    before = settings.get("model.selected_model", "")
    settings.set("model.selected_model", "Isolation-Probe")
    try:
        written = giscode_home() / "config.json"
        assert written.is_file(), "the write did not land in the isolated profile"
        assert "Isolation-Probe" in written.read_text(encoding="utf-8")

        real_config = REAL_HOME / "config.json"
        real_text = real_config.read_text(encoding="utf-8") if real_config.is_file() else ""
        assert "Isolation-Probe" not in real_text
    finally:
        settings.set("model.selected_model", before)


def test_profile_root_is_the_isolated_folder_parent():
    assert profile_root() == giscode_home().parent
    assert profile_root() != Path.home()
