"""An isolated test run must not see the developer's own models.

``GISCODE_HOME`` redirects profile files but deliberately leaves
``~/.lmstudio`` visible. Stage 3C's packaged run showed the consequence: the
app discovered ``~/.lmstudio`` weights and tried to load one at startup (the
resource budget refused it, but nothing asked for it).

``GISCODE_TEST_MODE=1`` is the explicit switch the suite sets: discovery scans
only the configured models folder (which an isolated profile relocates) and
the startup autoload does not run. These tests pin both halves - the switch
works, and *unsetting* it restores the normal application behaviour exactly.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

# Imported under another name: a module-level ``test_mode`` would be collected
# as a test function and merely return a bool.
from giscode.config.paths import (
    GISCODE_TEST_MODE_ENV,
    giscode_home,
    test_mode as isolated_test_mode,
)
from giscode.inference.api import _DEFAULT_MODEL_DIRS, _default_discovery_dirs, InferenceAPI
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.models.manager import _candidate_model_dirs


class _Settings:
    """The only SettingsManager method model discovery actually uses."""

    def __init__(self, models_dir: Path) -> None:
        self._models_dir = str(models_dir)

    def get(self, key, default=None):
        if key == "paths.models_dir":
            return self._models_dir
        return default


def _folded(dirs) -> set[str]:
    import os

    return {os.path.normcase(str(Path(d).expanduser())) for d in dirs}


def test_the_suite_runs_in_test_mode():
    # conftest sets this at import time for every collected test.
    assert isolated_test_mode()


def test_test_mode_scans_only_the_configured_models_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(GISCODE_TEST_MODE_ENV, "1")
    configured = tmp_path / "profile" / "models"

    dirs = _candidate_model_dirs(_Settings(configured))

    assert _folded(dirs) == _folded([configured])
    joined = " ".join(str(d).lower() for d in dirs)
    assert "lmstudio" not in joined


def test_without_test_mode_the_installed_model_folders_are_scanned(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(GISCODE_TEST_MODE_ENV, raising=False)
    configured = tmp_path / "profile" / "models"

    dirs = _candidate_model_dirs(_Settings(configured))

    assert not isolated_test_mode()
    assert _folded([configured]) <= _folded(dirs)
    assert any("lmstudio" in str(d).lower() for d in dirs), dirs


def test_inference_api_default_discovery_is_profile_only_in_test_mode(
    monkeypatch,
):
    monkeypatch.setenv(GISCODE_TEST_MODE_ENV, "1")

    api = InferenceAPI(runtime=MockLlamaCppRuntime(available=False))

    assert _folded(api._discovery._search_dirs) == _folded(
        [giscode_home() / "models"]
    )
    assert _folded(_default_discovery_dirs()) == _folded([giscode_home() / "models"])


def test_inference_api_default_discovery_is_unchanged_without_test_mode(
    monkeypatch,
):
    monkeypatch.delenv(GISCODE_TEST_MODE_ENV, raising=False)

    api = InferenceAPI(runtime=MockLlamaCppRuntime(available=False))

    # The profile folder now follows ``GISCODE_HOME`` (``giscode_home()``) instead
    # of a hard-coded ``~/.giscode``, so a redirected profile is respected even
    # outside test mode. The LM Studio and public folders are unchanged.
    expected = _folded(
        [giscode_home() / "models"] + [Path(d).expanduser() for d in _DEFAULT_MODEL_DIRS[1:]]
    )
    assert _folded(api._discovery._search_dirs) == expected
    assert _default_discovery_dirs() == list(_DEFAULT_MODEL_DIRS)


class _RecordingModelManager:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def auto_register_discovered(self):
        self.calls.append("discover")
        return []

    def get_registry(self):
        self.calls.append("registry")
        raise AssertionError("an autoload that must not run read the registry")

    def load_model(self, name):  # pragma: no cover - asserted against
        self.calls.append(f"load:{name}")


def test_autoload_is_skipped_in_test_mode(monkeypatch):
    from giscode.main import _try_autoload_model

    monkeypatch.setenv(GISCODE_TEST_MODE_ENV, "1")
    settings = _Settings(Path("unused"))
    manager = _RecordingModelManager()

    _try_autoload_model(logging.getLogger("test-mode"), settings, manager)

    assert manager.calls == []


def test_autoload_still_discovers_when_test_mode_is_off(monkeypatch):
    from giscode.main import _try_autoload_model

    monkeypatch.delenv(GISCODE_TEST_MODE_ENV, raising=False)

    class _NoSelection(_Settings):
        def get(self, key, default=None):
            if key == "model.selected_model":
                return ""
            return super().get(key, default)

    manager = _RecordingModelManager()

    _try_autoload_model(
        logging.getLogger("normal-mode"), _NoSelection(Path("unused")), manager
    )

    # The normal path still discovers; it then stops because no model is
    # selected, so nothing is loaded.
    assert manager.calls == ["discover"]
