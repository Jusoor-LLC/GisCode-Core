"""The packaging metadata must be a real PEP 621 project table.

``[metadata]`` is not a key any build backend reads: setuptools ignores it, so
``pip install .`` produced a package with no declared dependencies and no
Python requirement. These tests keep the corrected table in place.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_project_table_declares_python_and_runtime_dependency():
    project = _pyproject()["project"]

    assert project["name"] == "giscode"
    assert project["requires-python"].startswith(">=3.13")
    assert any(dependency.startswith("psutil") for dependency in project["dependencies"])


def test_the_ignored_metadata_table_is_gone():
    # A leftover [metadata] block would silently do nothing and mislead readers.
    assert "metadata" not in _pyproject()


def test_optional_extras_name_the_lazy_dependencies():
    extras = _pyproject()["project"]["optional-dependencies"]

    assert set(extras) >= {"webview", "yaml", "images", "llama-cpp", "test"}
    assert any("pywebview" in entry for entry in extras["webview"])
    assert any("pytest" in entry for entry in extras["test"])


def test_every_locked_download_is_pinned_and_hash_verified():
    """A stale or unpinned lock entry would let an unreviewed artifact in."""
    lines = (ROOT / "requirements-lock.txt").read_text(encoding="utf-8").splitlines()
    entries: list[tuple[str, list[str]]] = []
    for line in lines:
        if not line.startswith((" ", "#")) and line.strip():
            name = line.split(" \\")[0].strip()
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", name), name
            entries.append((name, []))
        elif line.strip().startswith("--hash=sha256:") and entries:
            digest = line.split("sha256:", 1)[1].strip()
            assert re.fullmatch(r"[0-9a-f]{64}", digest), entries[-1][0]
            entries[-1][1].append(digest)
    assert entries, "the lock file declares no requirements"
    assert all(hashes for _, hashes in entries), "every requirement needs a hash"


def test_pytest_configuration_survives_the_rewrite():
    options = _pyproject()["tool"]["pytest"]["ini_options"]

    assert options["testpaths"] == ["tests"]
    assert options["python_files"] == ["test_*.py"]
