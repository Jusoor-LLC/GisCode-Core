"""The verifier must expose the baseline comparison as data, not only prose.

Graded learning needs "did an existing failing test turn green" without parsing
the human-readable detail lines. These tests pin the structured fields and the
backwards-compatible defaults.
"""

from dataclasses import asdict

from giscode.agentic.test_runner import classify_python_result
from giscode.agentic.types import VerificationResult
from giscode.agentic.verifier import Verifier

FAILING_RUN = (
    "tests/test_calc.py::test_add PASSED   [ 50%]\n"
    "tests/test_calc.py::test_sub FAILED   [100%]\n"
    "FAILED tests/test_calc.py::test_sub - assert 1 == 2\n"
    "1 failed, 1 passed in 0.10s\n"
)

PASSING_RUN = (
    "tests/test_calc.py::test_add PASSED   [ 50%]\n"
    "tests/test_calc.py::test_sub PASSED   [100%]\n"
    "2 passed in 0.10s\n"
)


def _verifier(tmp_path) -> Verifier:
    return Verifier(workspace_root=tmp_path, terminal_executor=None)


def test_newly_passing_is_structured(tmp_path):
    baseline = classify_python_result("pytest", 1, FAILING_RUN, "")
    post = classify_python_result("pytest", 0, PASSING_RUN, "")

    result = _verifier(tmp_path)._build_classification_result(post, [], baseline=baseline)

    assert result.success is True
    assert result.test_execution_status == "passed"
    assert result.baseline_available is True
    assert result.newly_passing == ["tests/test_calc.py::test_sub"]
    assert result.tests_before == "failed"
    assert result.tests_after == "passed"
    # The prose report is unchanged.
    assert any("Newly passing: tests/test_calc.py::test_sub" in line for line in result.details)


def test_without_a_baseline_the_fields_stay_empty(tmp_path):
    post = classify_python_result("pytest", 0, PASSING_RUN, "")

    result = _verifier(tmp_path)._build_classification_result(post, [], baseline=None)

    assert result.baseline_available is False
    assert result.newly_passing == []
    assert result.tests_before == "unknown"
    assert result.tests_after == "passed"


def test_plain_verification_result_defaults_are_unchanged():
    result = VerificationResult(success=True)

    assert result.baseline_available is False
    assert result.newly_passing == []
    assert result.tests_before == "unknown"
    assert result.tests_after == "unknown"


def test_fields_survive_the_dict_conversion_used_for_persistence(tmp_path):
    baseline = classify_python_result("pytest", 1, FAILING_RUN, "")
    post = classify_python_result("pytest", 0, PASSING_RUN, "")

    payload = asdict(_verifier(tmp_path)._build_classification_result(post, [], baseline=baseline))

    assert payload["baseline_available"] is True
    assert payload["newly_passing"] == ["tests/test_calc.py::test_sub"]
    assert payload["tests_before"] == "failed"
    assert payload["tests_after"] == "passed"
