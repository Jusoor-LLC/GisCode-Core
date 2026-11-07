"""Verifying a finished build reports only what it actually established.

Every outcome here is distinct: a passing suite, a failing suite, no tests, no
changes, no interpreter, an exhausted budget, a timeout, a cancellation and a
crashing verifier. Only the first may be called verified.
"""
from __future__ import annotations

import pytest

from giscode.agentic.test_runner import PythonResolution
from giscode.agentic.types import VerificationResult
from giscode.agentic.verification_budget import VerificationBudget
from giscode.agentic.verifier import (
    TEST_STATUS_COULD_NOT_RUN,
    TEST_STATUS_PASSED,
)
from giscode.projects.build_verification import (
    BuildVerificationOutcome,
    verify_completed_build,
)


class _StubVerifier:
    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def verify_project(self, budget_s=None):
        self.calls.append(budget_s)
        if self.raises is not None:
            raise self.raises
        return self.result


def _vr(**fields):
    base = dict(
        success=True,
        errors=[],
        details=[],
        test_execution_status=TEST_STATUS_PASSED,
    )
    base.update(fields)
    return VerificationResult(**base)


def test_passing_project_tests_are_verified(tmp_path):
    (tmp_path / "test_ok.py").write_bytes(b"def test_a():\n    assert True\n")

    report = verify_completed_build(tmp_path, changed_files=["test_ok.py"])

    assert report.outcome is BuildVerificationOutcome.VERIFIED
    assert report.verified is True
    assert report.tests_run is True
    assert report.verification.get("test_execution_status") == TEST_STATUS_PASSED


def test_failing_project_tests_are_reported_as_failures(tmp_path):
    (tmp_path / "test_bad.py").write_bytes(b"def test_a():\n    assert False\n")

    report = verify_completed_build(tmp_path, changed_files=["test_bad.py"])

    assert report.outcome is BuildVerificationOutcome.TESTS_FAILED
    assert report.verified is False
    assert "failed" in report.message.lower()


def test_no_project_tests_is_not_reported_as_success(tmp_path):
    report = verify_completed_build(tmp_path, changed_files=["app.py"])

    assert report.outcome is BuildVerificationOutcome.NO_TESTS
    assert report.tests_run is False
    assert "no project tests" in report.message.lower()


def test_no_changes_does_not_spend_a_test_run(tmp_path):
    report = verify_completed_build(tmp_path, changed_files=[])

    assert report.outcome is BuildVerificationOutcome.NO_CHANGES
    assert report.tests_run is False
    assert "no tests" in report.message.lower()


def test_unknown_changes_still_run_the_suite(tmp_path):
    (tmp_path / "test_ok.py").write_bytes(b"def test_a():\n    assert True\n")

    report = verify_completed_build(tmp_path, changed_files=None)

    assert report.outcome is BuildVerificationOutcome.VERIFIED


def test_missing_interpreter_is_unverified_and_runs_nothing(tmp_path, monkeypatch):
    import giscode.projects.build_verification as mod

    stub = _StubVerifier(_vr())
    monkeypatch.setattr(
        mod,
        "resolve_python",
        lambda root: PythonResolution(
            executable=None,
            source=None,
            reason="no interpreter was found on this machine",
            version="",
        ),
    )

    report = verify_completed_build(tmp_path, changed_files=["a.py"], verifier=stub)

    assert report.outcome is BuildVerificationOutcome.UNAVAILABLE
    assert "no interpreter" in report.message
    assert stub.calls == []


def test_exhausted_budget_is_refused_before_running(tmp_path):
    stub = _StubVerifier(_vr())
    budget = VerificationBudget(lambda: 0.0, max_repair_attempts=0)

    report = verify_completed_build(
        tmp_path, changed_files=["a.py"], budget=budget, verifier=stub
    )

    assert report.outcome is BuildVerificationOutcome.UNAVAILABLE
    assert "budget" in report.message.lower()
    assert stub.calls == []
    assert budget.passes == 0


def test_a_timeout_is_reported_as_a_timeout(tmp_path):
    stub = _StubVerifier(
        _vr(
            success=False,
            errors=["tests timed out after 30 s (the change was never judged)"],
            test_execution_status=TEST_STATUS_COULD_NOT_RUN,
        )
    )

    report = verify_completed_build(tmp_path, changed_files=["a.py"], verifier=stub)

    assert report.outcome is BuildVerificationOutcome.TIMEOUT
    assert report.verified is False


def test_a_runner_that_cannot_start_is_unavailable(tmp_path):
    stub = _StubVerifier(
        _vr(
            success=False,
            errors=["pytest is not importable in this interpreter"],
            test_execution_status=TEST_STATUS_COULD_NOT_RUN,
        )
    )

    report = verify_completed_build(tmp_path, changed_files=["a.py"], verifier=stub)

    assert report.outcome is BuildVerificationOutcome.UNAVAILABLE


def test_cancellation_before_the_run_spends_no_pass(tmp_path):
    budget = VerificationBudget(max_repair_attempts=0)
    stub = _StubVerifier(_vr())

    report = verify_completed_build(
        tmp_path,
        changed_files=["a.py"],
        budget=budget,
        should_cancel=lambda: True,
        verifier=stub,
    )

    assert report.outcome is BuildVerificationOutcome.CANCELLED
    assert budget.passes == 0
    assert stub.calls == []


def test_cancellation_during_the_run_discards_the_verdict(tmp_path):
    state = {"cancelled": False}

    class _CancelDuringRun(_StubVerifier):
        def verify_project(self, budget_s=None):
            state["cancelled"] = True
            return _vr()

    report = verify_completed_build(
        tmp_path,
        changed_files=["a.py"],
        should_cancel=lambda: state["cancelled"],
        verifier=_CancelDuringRun(),
    )

    assert report.outcome is BuildVerificationOutcome.CANCELLED
    assert report.tests_run is True
    assert report.verified is False


def test_a_crashing_verifier_is_an_error_not_a_pass(tmp_path):
    report = verify_completed_build(
        tmp_path, changed_files=["a.py"], verifier=_StubVerifier(raises=RuntimeError("boom"))
    )

    assert report.outcome is BuildVerificationOutcome.ERROR
    assert "boom" in report.message
    assert report.verified is False


def test_the_report_serializes_without_claiming_more_than_it_did(tmp_path):
    report = verify_completed_build(tmp_path, changed_files=[])

    data = report.to_dict()

    assert data["verified"] is False
    assert data["outcome"] == "completed_no_changes"
    assert data["tests_run"] is False
