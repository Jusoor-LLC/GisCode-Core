"""The interactive scenario must stay unreachable outside the acceptance EXE.

``GisCodeAcceptance.exe`` bakes ``sys._giscode_acceptance_build`` into the bundle
through a PyInstaller runtime hook. These tests pin the boundary for the scenario
entry point added on top of the existing automation: a product build ignores the
scenario variable, and even a marked build needs the explicit opt-in.
"""
from __future__ import annotations

import sys

import pytest

import giscode.acceptance_runtime as runtime
import giscode.main as main_module

MARKER = main_module.ACCEPTANCE_BUILD_MARKER


class _Root:
    def __init__(self) -> None:
        self.callbacks: dict[int, object] = {}
        self._giscode_main_window = object()

    def after(self, delay, callback):
        self.callbacks[delay] = callback


def _scenario_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(runtime.SCENARIO_ENV, "interactive")
    monkeypatch.setenv("GISCODE_ACCEPTANCE_PROJECT", str(tmp_path))
    monkeypatch.setenv("GISCODE_ACCEPTANCE_EVIDENCE", "interactive.json")


def test_scenario_is_inert_in_a_production_build(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, MARKER, raising=False)
    monkeypatch.setenv(main_module.ACCEPTANCE_OPT_IN_ENV, "1")
    _scenario_env(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(runtime, "schedule_scenario", lambda *a, **k: calls.append(a))
    root = _Root()

    main_module._schedule_gui_acceptance(root, object())

    assert calls == []
    assert root.callbacks == {}


def test_scenario_requires_the_explicit_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, MARKER, True, raising=False)
    monkeypatch.setenv(main_module.ACCEPTANCE_OPT_IN_ENV, "0")
    _scenario_env(monkeypatch, tmp_path)
    calls: list = []
    monkeypatch.setattr(runtime, "schedule_scenario", lambda *a, **k: calls.append(a))

    main_module._schedule_gui_acceptance(_Root(), object())

    assert calls == []


def test_scenario_is_scheduled_in_an_opted_in_acceptance_build(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, MARKER, True, raising=False)
    monkeypatch.setenv(main_module.ACCEPTANCE_OPT_IN_ENV, "1")
    _scenario_env(monkeypatch, tmp_path)
    calls: list = []

    def _record(root, scenario, **kwargs):
        calls.append((root, scenario, kwargs))
        return True

    monkeypatch.setattr(runtime, "schedule_scenario", _record)
    root = _Root()

    main_module._schedule_gui_acceptance(root, object())

    assert len(calls) == 1
    assert calls[0][1] == "interactive"
    # The confining helpers must come from the running entry point, not a
    # re-import, so the evidence stays inside the dedicated directory.
    assert calls[0][2]["evidence_path"] is main_module._acceptance_evidence_path
    assert calls[0][2]["write_evidence"] is main_module._acceptance_write_evidence
    assert root.callbacks == {}


def test_unknown_scenario_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("GISCODE_ACCEPTANCE_EVIDENCE", "interactive.json")
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))
    # The refusal leaves through os._exit; record it instead of killing pytest.
    refused: list[str] = []

    def _refuse(reason: str):
        refused.append(reason)
        raise SystemExit(2)

    monkeypatch.setattr(runtime, "_refuse", _refuse)

    with pytest.raises(SystemExit):
        runtime.schedule_scenario(
            _Root(),
            "nonsense",
            evidence_path=main_module._acceptance_evidence_path,
            write_evidence=main_module._acceptance_write_evidence,
        )
    assert refused == ["unknown scenario"]
    written = tmp_path / main_module.ACCEPTANCE_EVIDENCE_ROOT / "interactive.json"
    assert written.is_file()
    assert '"unknown scenario"' in written.read_text(encoding="utf-8")
