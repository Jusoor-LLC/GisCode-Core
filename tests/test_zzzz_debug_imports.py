"""Temporary CI diagnostic, not part of the product test suite."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def _load(path: Path):
    name = "debug_import_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_reauthorization_after_importing_suite(tmp_path):
    limit = int(os.environ.get("DEBUG_IMPORT_STOP", "999"))
    here = Path(__file__).resolve().parent
    files = sorted(here.glob("test_*.py"))
    for path in files[:limit]:
        if path.name == Path(__file__).name:
            continue
        try:
            _load(path)
        except pytest.skip.Exception:
            pass
    auth = _load(here / "test_chatgpt_siwc.py")
    auth.test_reauthorization_identity_change_is_explicit(tmp_path)
