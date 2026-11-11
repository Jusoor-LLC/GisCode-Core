"""The production build must stay free of acceptance-only controls.

The marker is a PyInstaller runtime hook. It exists only in the hardened
acceptance recipe; these tests fail if it appears in the Core recipe or if the
scenario stops requiring a verified disposable home.
"""
from __future__ import annotations

import sys
from pathlib import Path

import giscode.main as main_module

ROOT = Path(__file__).resolve().parents[1]


def test_core_spec_cannot_enable_acceptance():
    text = (ROOT / "GisCodeCore.spec").read_text(encoding="utf-8")

    assert "_giscode_acceptance_build" not in text
    assert "GISCODE_ACCEPTANCE_ENTRY" not in text
    assert "pyi_rth_giscode_acceptance" not in text
    assert "runtime_hooks=[]" in text


def test_hardened_spec_bakes_the_marker():
    text = (ROOT / "GisCodeAcceptanceHardened.spec").read_text(encoding="utf-8")

    assert "_giscode_acceptance_build = True" in text
    assert "runtime_hooks=[str(hook_path)]" in text
    assert 'name="GisCodeAcceptanceHardened"' in text


def test_runtime_requires_the_disposable_home_before_anything_else():
    text = (ROOT / "giscode" / "acceptance_runtime.py").read_text(encoding="utf-8")

    assert "verify_disposable_home(require_destructive=name == INTERACTIVE)" in text
    assert "SCENARIO_REFUSED" in text


def test_production_gate_needs_marker_and_opt_in(monkeypatch):
    monkeypatch.delattr(sys, main_module.ACCEPTANCE_BUILD_MARKER, raising=False)
    monkeypatch.setenv(main_module.ACCEPTANCE_OPT_IN_ENV, "1")
    assert main_module._acceptance_automation_enabled() is False

    monkeypatch.setattr(sys, main_module.ACCEPTANCE_BUILD_MARKER, True, raising=False)
    assert main_module._acceptance_automation_enabled() is True

    monkeypatch.setenv(main_module.ACCEPTANCE_OPT_IN_ENV, "0")
    assert main_module._acceptance_automation_enabled() is False
