"""Protected target test: a fix may not rewrite the test it must satisfy.

Covers the J4 failure mode (the model guts the check it was asked to make
pass) and the two legitimate cases (adding tests, changing a test the request
explicitly asks to change).
"""

from pathlib import Path

import pytest

from giscode.agentic.types import ActionType, ExecutionPlan, ExecutionStep
from giscode.agentic.verifier import (
    PROTECTED_TEST_ERROR,
    Verifier,
    named_test_files_in_request,
    protected_test_gutted,
    request_allows_test_edits,
)


# J4's request, verbatim from realjobs.md: the named test is the JUDGE, not
# the deliverable, so editing it is gaming.
J4_REQUEST = (
    "tests/test_project_identity_isolation.py fails: the assistant reply now "
    "comes back prefixed with 'Files read: (none)' before the original answer, "
    "breaking the exact-match assertion. When there is no active project the "
    "model's reply must reach the user unchanged. Fix it so the test passes."
)


def _large_original() -> str:
    """A J4-sized target test: ~240 lines, many assertions."""
    lines = [
        '"""Identity isolation checks."""',
        "",
        "from giscode.projects.context import ProjectContext",
        "",
    ]
    for index in range(59):
        lines += [
            f"def test_case_{index}():",
            f"    context = ProjectContext('demo{index}')",
            f"    assert context.project_id == 'demo{index}'",
            "    assert context.prefix == 'Files read: (none)'",
            f"    assert context.reply('x') == 'Files read: (none)' + chr(10) + 'x'",
            "",
        ]
    return "\n".join(lines)


LARGE_ORIGINAL = _large_original()

GUTTED_STUB = '''"""Identity isolation checks."""


def test_case_0():
    assert True


def test_case_1():
    pass
'''

ZERO_TEST_FUNCTIONS = '''"""Identity isolation checks."""


def stub():
    pass
'''

OTHER_TEST = '''def test_unrelated():
    assert 1 + 1 == 2
'''


class _StubResult:
    def __init__(self, exit_code=0, stdout="3 passed", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _StubExecutor:
    """Terminal executor stub: every test command 'passes'."""

    def __init__(self):
        self.commands = []

    def execute(self, request):
        self.commands.append(request)
        return _StubResult()


def _project(tmp_path: Path, target_content: str = LARGE_ORIGINAL) -> Path:
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "src").mkdir(parents=True)
    (root / "tests" / "test_target.py").write_text(target_content, encoding="utf-8")
    (root / "tests" / "test_other.py").write_text(OTHER_TEST, encoding="utf-8")
    (root / "src" / "mod.py").write_text("def reply(text):\n    return text\n", encoding="utf-8")
    return root


def _plan(request: str, path: str = "tests/test_target.py") -> ExecutionPlan:
    return ExecutionPlan(
        objective=request,
        steps=[
            ExecutionStep(
                step_id="step-1",
                description="edit",
                action_type=ActionType.MODIFY,
                target_path=path,
            )
        ],
    )


def _write_action(root: Path, rel: str, new_content: str) -> dict:
    """Simulate a successful write_file: original captured, disk updated."""
    target = root / rel
    original = target.read_text(encoding="utf-8")
    target.write_text(new_content, encoding="utf-8")
    return {
        "step": "edit",
        "tool": "write_file",
        "args": {"path": rel, "content": new_content},
        "success": True,
        "output": "",
        "error": None,
        "original_content": original,
    }


def _verify(root: Path, request: str, actions: list, target_tests=None):
    verifier = Verifier(root, terminal_executor=_StubExecutor())
    return verifier.verify(
        _plan(request), actions, baseline=None, target_tests=target_tests
    )


# ---------------------------------------------------------------------------
# J4's pattern and the destructive variants
# ---------------------------------------------------------------------------


def test_j4_sized_gutted_target_test_is_rejected(tmp_path):
    root = _project(tmp_path)
    action = _write_action(root, "tests/test_target.py", GUTTED_STUB)
    result = _verify(
        root,
        J4_REQUEST,
        [action],
        target_tests={"tests/test_project_identity_isolation.py::test_case_0",
                      "tests/test_target.py::test_case_0"},
    )
    assert not result.success
    assert result.test_execution_status == "failed"
    assert any(PROTECTED_TEST_ERROR in error for error in result.errors)
    assert "tests/test_target.py" in result.errors[0]


def test_target_test_deleted_is_rejected(tmp_path):
    root = _project(tmp_path)
    action = _write_action(root, "tests/test_target.py", "")
    result = _verify(
        root,
        "tests/test_target.py fails: fix it so the test passes.",
        [action],
        target_tests={"tests/test_target.py::test_case_0"},
    )
    assert not result.success
    assert any("was deleted" in error for error in result.errors)
    assert any(PROTECTED_TEST_ERROR in error for error in result.errors)


def test_target_test_body_replaced_with_pass_is_rejected(tmp_path):
    root = _project(tmp_path)
    action = _write_action(root, "tests/test_target.py", ZERO_TEST_FUNCTIONS)
    result = _verify(
        root,
        "tests/test_target.py fails: fix it so the test passes.",
        [action],
        target_tests={"tests/test_target.py::test_case_0"},
    )
    assert not result.success
    assert any("lost every test function" in error for error in result.errors)


def test_target_test_assertion_collapse_is_rejected(tmp_path):
    root = _project(tmp_path)
    collapsed = '''"""Identity isolation checks."""


def test_case_0():
    assert True


def test_case_1():
    assert True
'''
    action = _write_action(root, "tests/test_target.py", collapsed)
    result = _verify(
        root,
        "tests/test_target.py fails: fix it so the test passes.",
        [action],
        target_tests={"tests/test_target.py::test_case_0"},
    )
    assert not result.success
    assert any("cut its assertions" in error for error in result.errors)


def test_target_test_size_cut_over_30_percent_is_rejected(tmp_path):
    long_body = "\n".join(
        ["def test_case_0():"]
        + [f"    value_{index} = {index}" for index in range(60)]
        + ["    assert value_59 == 59", ""]
    )
    short_body = "def test_case_0():\n    assert True\n"
    root = _project(tmp_path, target_content=long_body)
    action = _write_action(root, "tests/test_target.py", short_body)
    result = _verify(
        root,
        "tests/test_target.py fails: fix it so the test passes.",
        [action],
        target_tests={"tests/test_target.py::test_case_0"},
    )
    assert not result.success
    assert any("cut its size" in error for error in result.errors)


def test_make_failing_tests_pass_request_protects_target_files(tmp_path):
    root = _project(tmp_path)
    action = _write_action(root, "tests/test_target.py", GUTTED_STUB)
    result = _verify(
        root,
        "Make the failing tests in tests/test_target.py pass.",
        [action],
        target_tests={"tests/test_target.py::test_case_0"},
    )
    assert not result.success
    assert any(PROTECTED_TEST_ERROR in error for error in result.errors)


# ---------------------------------------------------------------------------
# The two legitimate cases: adding tests, and changing a named test
# ---------------------------------------------------------------------------


def test_legitimate_test_addition_is_allowed(tmp_path):
    root = _project(tmp_path)
    extended = LARGE_ORIGINAL + "\n\ndef test_case_extra():\n    assert True\n"
    action = _write_action(root, "tests/test_target.py", extended)
    result = _verify(
        root,
        "Add a regression test for reply in tests/test_target.py.",
        [action],
        target_tests=None,
    )
    assert not any(PROTECTED_TEST_ERROR in error for error in result.errors)
    assert result.success is True
    assert result.test_execution_status == "passed"


def test_explicit_test_change_request_is_allowed(tmp_path):
    root = _project(tmp_path)
    revised = LARGE_ORIGINAL.replace(
        "    assert context.prefix == 'Files read: (none)'",
        "    assert context.prefix is None",
    )
    action = _write_action(root, "tests/test_target.py", revised)
    result = _verify(
        root,
        "Update tests/test_target.py so the prefix assertion matches the "
        "unchanged-reply behaviour.",
        [action],
        target_tests={"tests/test_target.py::test_case_0"},
    )
    assert not any(PROTECTED_TEST_ERROR in error for error in result.errors)


def test_untouched_protected_test_is_not_flagged(tmp_path):
    root = _project(tmp_path)
    action = _write_action(root, "src/mod.py", "def reply(text):\n    return text or ''\n")
    result = _verify(
        root,
        J4_REQUEST,
        [action],
        target_tests={"tests/test_target.py::test_case_0"},
    )
    assert not any(PROTECTED_TEST_ERROR in error for error in result.errors)


def test_non_target_test_edit_is_not_flagged(tmp_path):
    root = _project(tmp_path)
    action = _write_action(
        root, "tests/test_other.py", OTHER_TEST + "\n\ndef test_extra():\n    assert True\n"
    )
    result = _verify(root, "Fix the reply formatting in src/mod.py.", [action])
    assert not any(PROTECTED_TEST_ERROR in error for error in result.errors)


def test_failed_write_action_is_ignored(tmp_path):
    root = _project(tmp_path)
    target = root / "tests" / "test_target.py"
    action = {
        "step": "edit",
        "tool": "write_file",
        "args": {"path": "tests/test_target.py", "content": GUTTED_STUB},
        "success": False,
        "output": "",
        "error": "permission denied",
        "original_content": target.read_text(encoding="utf-8"),
    }
    result = _verify(
        root,
        J4_REQUEST,
        [action],
        target_tests={"tests/test_target.py::test_case_0"},
    )
    assert not any(PROTECTED_TEST_ERROR in error for error in result.errors)


def test_gate_survives_several_steps_on_the_same_file(tmp_path):
    root = _project(tmp_path)
    intermediate = LARGE_ORIGINAL + "\n\ndef test_step_one():\n    assert True\n"
    first = _write_action(root, "tests/test_target.py", intermediate)
    final = _write_action(root, "tests/test_target.py", GUTTED_STUB)
    result = _verify(
        root,
        J4_REQUEST,
        [first, final],
        target_tests={"tests/test_target.py::test_case_0"},
    )
    assert not result.success
    assert any(PROTECTED_TEST_ERROR in error for error in result.errors)


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_request_allows_test_edits_classification():
    assert request_allows_test_edits(J4_REQUEST) is False
    assert request_allows_test_edits("") is False
    assert request_allows_test_edits("Add a regression test for parse.") is True
    assert request_allows_test_edits("Update tests/test_x.py so it asserts42.") is True
    assert request_allows_test_edits("The test is wrong; fix the code.") is True
    assert request_allows_test_edits("Make the failing tests pass.") is False


def test_named_test_files_in_request_forms():
    assert named_test_files_in_request(J4_REQUEST) == [
        "tests/test_project_identity_isolation.py"
    ]
    assert named_test_files_in_request("run tests.test_target") == [
        "tests/test_target.py"
    ]


def test_protected_test_gutted_classification():
    original = LARGE_ORIGINAL
    assert protected_test_gutted(original, original) is None
    assert protected_test_gutted(original, original + "\n" + " " * 40) is None
    assert "was deleted" in (protected_test_gutted(original, "   ") or "")
    assert "lost every test function" in (
        protected_test_gutted(original, ZERO_TEST_FUNCTIONS) or ""
    )
    assert "cut its assertions" in (protected_test_gutted(original, GUTTED_STUB) or "")
    assert protected_test_gutted("def test_a():\n    assert True\n", "def test_a():\n    assert True\n    assert False\n") is None
