"""The first coding task: it must write one file, inside its own workspace, and
it must refuse to apply a reply it cannot verify.
"""
from __future__ import annotations

import pytest

from giscode.onboarding import first_task as task_module

GOOD_REPLY = (
    "Here is the fix:\n\n```python\ndef greet(name):\n    return f\"Hello, {name}!\"\n```\n"
)


class _Provider:
    def __init__(self, reply):
        self.reply = reply

    def chat(self, model, messages, **kwargs):
        return self.reply


@pytest.fixture(autouse=True)
def _isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("GISCODE_HOME", str(tmp_path / "profile"))
    return tmp_path


def test_a_correct_fix_is_applied_verified_and_diffed(tmp_path):
    result = task_module.run_first_task(
        _Provider(GOOD_REPLY), "qwen2.5-coder:7b", root=str(tmp_path / "workspaces")
    )
    assert result.ok is True
    assert "Hello, Ada!" in result.detail
    assert "+    return f\"Hello, {name}!\"" in result.diff
    checks = {item["name"]: item["ok"] for item in result.checks}
    assert checks["code_block"] and checks["verification"] and checks["bounded_output"]


def test_a_reply_without_a_code_block_changes_nothing(tmp_path):
    result = task_module.run_first_task(
        _Provider("I would change the return statement."),
        "qwen2.5-coder:7b",
        root=str(tmp_path / "workspaces"),
    )
    assert result.ok is False
    assert "no code block" in result.detail
    written = (tmp_path / "workspaces").glob("*/greeting.py")
    for path in written:
        assert path.read_text(encoding="utf-8") == task_module.INITIAL_SOURCE


def test_a_wrong_fix_fails_verification_and_shows_the_diff(tmp_path):
    wrong = "```python\ndef greet(name):\n    return 'Hello ' + name\n```"
    result = task_module.run_first_task(
        _Provider(wrong), "qwen2.5-coder:7b", root=str(tmp_path / "workspaces")
    )
    assert result.ok is False
    assert "check failed" in result.detail
    assert result.diff


def test_an_oversized_reply_is_not_applied(tmp_path):
    huge = "```python\n" + ("# padding\n" * 4000) + "```"
    result = task_module.run_first_task(
        _Provider(huge), "qwen2.5-coder:7b", root=str(tmp_path / "workspaces")
    )
    assert result.ok is False
    assert "larger than the task allows" in result.detail


def test_a_model_error_is_reported_without_touching_the_file(tmp_path):
    class _Broken:
        def chat(self, *args, **kwargs):
            raise RuntimeError("connection reset")

    result = task_module.run_first_task(
        _Broken(), "qwen2.5-coder:7b", root=str(tmp_path / "workspaces")
    )
    assert result.ok is False
    assert "connection reset" in result.detail


def test_the_workspace_is_disposable_and_separate_per_run(tmp_path):
    first = task_module.run_first_task(
        _Provider(GOOD_REPLY), "m", root=str(tmp_path / "workspaces")
    )
    second = task_module.run_first_task(
        _Provider(GOOD_REPLY), "m", root=str(tmp_path / "workspaces")
    )
    assert first.workspace != second.workspace
    assert first.workspace.startswith(str(tmp_path / "workspaces"))


def test_no_model_means_no_task():
    result = task_module.run_first_task(_Provider(GOOD_REPLY), "  ")
    assert result.ok is False
    assert "No model" in result.detail
