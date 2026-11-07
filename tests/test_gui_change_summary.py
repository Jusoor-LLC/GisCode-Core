"""Focused tests for the user-facing "What Changed" summary.

These tests assert only what the recorded RealWork evidence can actually
support:

* successful ``write_file`` actions with a captured pre-edit snapshot become
  the changed-file list, with +lines/-lines from the same unified diff that
  ``ExecutionHistoryStore.record_work_result`` persists;
* an empty or diff-free result renders an honest empty summary;
* ``test_execution_status`` drives the wording, and a verifier that only
  succeeded because tests were unavailable is never reported as PASS or
  "Tests passed";
* failed validation, repair attempts and real errors surface only when the
  result actually records them;
* CREATE vs MODIFY is never inferred from ``original_content``;
* a fallback/non-RealWork result with no ``work_result`` produces no summary.

No files outside ``tmp_path`` are touched and no project-wide tracking exists.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from giscode.gui.main_window import MainWindow
from giscode.gui.task_report import (
    VALIDATION_FAIL,
    VALIDATION_NOT_RUN,
    VALIDATION_PASS,
    VALIDATION_UNAVAILABLE,
    build_change_summary,
    collect_errors,
    format_change_summary,
    ran_shell_commands,
    validation_state,
)
# Aliased so pytest does not collect the imported helper as a test function.
from giscode.gui.task_report import test_status_word as _test_status_word


def _write_action(path, before, after, success=True, **extra):
    action = {
        "tool": "write_file",
        "success": success,
        "args": {"path": path, "content": after},
        "original_content": before,
    }
    action.update(extra)
    return action


def _rewrite(tmp_path, name, before, after):
    target = tmp_path / name
    target.write_text(before, encoding="utf-8")
    target.write_text(after, encoding="utf-8")
    return name


# ─── changed files and line counts ─────────────────────────────────────────────


class TestChangedFilesAndLineCounts:
    def test_changed_files_are_listed_with_line_counts(self, tmp_path):
        first = _rewrite(tmp_path, "first.py", "old-one\n", "new-one\n")
        second = _rewrite(tmp_path, "second.py", "a\nb\n", "a\nb\nc\n")
        summary = build_change_summary(
            {
                "status": "completed",
                "verification": {"success": True, "test_execution_status": "passed"},
                "actions": [
                    _write_action(first, "old-one\n", "new-one\n"),
                    _write_action(second, "a\nb\n", "a\nb\nc\n"),
                ],
            },
            str(tmp_path),
        )
        rendered = format_change_summary(summary)

        assert summary["file_count"] == 2
        assert [item["path"] for item in summary["files"]] == ["first.py", "second.py"]
        assert summary["additions"] == 2
        assert summary["deletions"] == 1
        assert "Files changed: 2" in rendered
        assert "first.py +1 -1" in rendered
        assert "second.py +1 -0" in rendered
        assert "Lines: +2 -1" in rendered

    def test_failed_write_action_is_not_reported_as_a_change(self, tmp_path):
        target = tmp_path / "kept.py"
        target.write_text("original\n", encoding="utf-8")
        summary = build_change_summary(
            {"actions": [_write_action("kept.py", "original\n", "broken\n", success=False)]},
            str(tmp_path),
        )
        assert summary["file_count"] == 0

    def test_write_with_no_actual_difference_is_not_a_change(self, tmp_path):
        same = _rewrite(tmp_path, "same.py", "same\n", "same\n")
        summary = build_change_summary(
            {"actions": [_write_action(same, "same\n", "same\n")]},
            str(tmp_path),
        )
        assert summary["file_count"] == 0
        assert summary["diff"] == ""

    def test_file_outside_project_root_is_excluded(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("a\n", encoding="utf-8")
        outside.write_text("b\n", encoding="utf-8")
        summary = build_change_summary(
            {"actions": [_write_action("../outside.py", "a\n", "b\n")]},
            str(root),
        )
        assert summary["file_count"] == 0


# ─── empty / no-change results ─────────────────────────────────────────────────


class TestEmptyResult:
    def test_empty_actions_render_no_change_summary(self, tmp_path):
        summary = build_change_summary({"actions": []}, str(tmp_path))
        rendered = format_change_summary(summary)
        assert summary["file_count"] == 0
        assert "Files changed: 0" in rendered
        assert "Changed:" not in rendered
        assert "Lines:" not in rendered
        assert f"Validation: {VALIDATION_NOT_RUN}" in rendered

    def test_read_only_result_reports_no_file_changes(self, tmp_path):
        summary = build_change_summary(
            {
                "status": "completed",
                "actions": [{
                    "tool": "read_file",
                    "success": True,
                    "args": {"path": "app.py"},
                    "output": "file body",
                }],
                "verification": {"success": True, "test_execution_status": "unavailable"},
            },
            str(tmp_path),
        )
        rendered = format_change_summary(summary)
        assert "Files changed: 0" in rendered
        assert f"Validation: {VALIDATION_UNAVAILABLE}" in rendered
        assert "Tests: unavailable" in rendered

    def test_missing_actions_key_is_handled(self, tmp_path):
        summary = build_change_summary({}, str(tmp_path))
        assert summary["file_count"] == 0
        assert summary["files"] == []
        assert summary["errors"] == []
        assert summary["verification_state"] == VALIDATION_NOT_RUN


# ─── validation and test wording ───────────────────────────────────────────────


class TestValidationWording:
    def test_pass_requires_a_real_passed_test_execution(self):
        assert validation_state({"success": True, "test_execution_status": "passed"}) == VALIDATION_PASS

    @pytest.mark.parametrize("status", ["unavailable", "unknown", "", None])
    def test_success_without_a_real_test_run_is_not_pass(self, status):
        verification = {"success": True, "test_execution_status": status}
        assert validation_state(verification) == VALIDATION_UNAVAILABLE

    def test_no_tests_found_success_is_never_reported_as_passed(self):
        """Verifier returns success=True + unavailable when no tests exist."""
        verification = {
            "success": True,
            "errors": [],
            "details": ["No test files found; skipping test execution"],
            "test_execution_status": "unavailable",
        }
        rendered = format_change_summary(
            {"files": [], "file_count": 0, "verification_state": validation_state(verification),
             "test_status": _test_status_word(verification)}
        )
        assert "Tests: unavailable" in rendered
        assert "Tests passed" not in rendered
        assert "Tests: passed" not in rendered
        assert f"Validation: {VALIDATION_UNAVAILABLE}" in rendered

    def test_failed_validation_is_reported_as_fail(self, tmp_path):
        summary = build_change_summary(
            {
                "status": "failed",
                "failure_reason": "verification_failed",
                "actions": [],
                "verification": {
                    "success": False,
                    "errors": ["Python test suite failed (exit_code=1)"],
                    "details": ["Test stdout: FAILED (failures=1)"],
                    "test_execution_status": "failed",
                },
            },
            str(tmp_path),
        )
        rendered = format_change_summary(summary)
        assert summary["verification_state"] == VALIDATION_FAIL
        assert f"Validation: {VALIDATION_FAIL}" in rendered
        assert "Tests: failed" in rendered

    def test_absent_verification_is_not_run(self):
        assert validation_state(None) == VALIDATION_NOT_RUN
        assert validation_state({}) == VALIDATION_NOT_RUN

    def test_unknown_test_status_prints_no_tests_line(self, tmp_path):
        summary = build_change_summary(
            {
                "actions": [],
                "verification": {"success": True, "test_execution_status": "unknown"},
            },
            str(tmp_path),
        )
        rendered = format_change_summary(summary)
        assert "Tests:" not in rendered
        assert f"Validation: {VALIDATION_UNAVAILABLE}" in rendered

    def test_no_pass_fail_skip_counts_are_invented(self, tmp_path):
        summary = build_change_summary(
            {
                "actions": [],
                "verification": {
                    "success": True,
                    "details": ["13 tests passed"],
                    "test_execution_status": "passed",
                },
            },
            str(tmp_path),
        )
        rendered = format_change_summary(summary)
        assert "13" not in rendered
        for word in ("passed", "failed", "skipped", "skip"):
            assert f"{word}=" not in rendered.lower()


# ─── repairs and errors ───────────────────────────────────────────────────────


class TestRepairsAndErrors:
    def test_repair_attempts_shown_when_present(self, tmp_path):
        summary = build_change_summary(
            {"actions": [], "repair_attempts": 2, "verification": {"success": True, "test_execution_status": "passed"}},
            str(tmp_path),
        )
        assert "Repairs: 2 attempted" in format_change_summary(summary)

    def test_zero_repairs_prints_no_repairs_line(self, tmp_path):
        summary = build_change_summary(
            {"actions": [], "repair_attempts": 0, "verification": {"success": True, "test_execution_status": "passed"}},
            str(tmp_path),
        )
        assert "Repairs:" not in format_change_summary(summary)

    def test_errors_only_render_when_real_errors_exist(self, tmp_path):
        clean = build_change_summary(
            {"actions": [], "verification": {"success": True, "test_execution_status": "passed"}},
            str(tmp_path),
        )
        assert "Errors:" not in format_change_summary(clean)

    def test_failed_task_errors_are_listed(self, tmp_path):
        summary = build_change_summary(
            {
                "status": "failed",
                "error": "Action(s) failed: write_file: File already exists",
                "failure_reason": "action_failed",
                "actions": [{
                    "tool": "write_file",
                    "success": False,
                    "args": {"path": "a.py"},
                    "error": "File already exists: a.py",
                }],
                "verification": {
                    "success": False,
                    "errors": ["Python test suite failed (exit_code=1)"],
                    "test_execution_status": "failed",
                },
            },
            str(tmp_path),
        )
        rendered = format_change_summary(summary)
        assert "Errors:" in rendered
        assert "Action(s) failed: write_file: File already exists" in rendered
        assert "action_failed" in rendered
        assert "Python test suite failed (exit_code=1)" in rendered

    def test_error_collection_deduplicates(self):
        errors = collect_errors({
            "error": "boom",
            "failure_reason": "boom",
            "actions": [{"tool": "write_file", "success": False, "error": "boom"}],
        })
        assert errors == ["boom", "write_file: boom"]

    def test_successful_actions_contribute_no_errors(self):
        assert collect_errors({
            "status": "completed",
            "actions": [{"tool": "write_file", "success": True, "error": None}],
        }) == []


# ─── CREATE / MODIFY is never inferred ────────────────────────────────────────


class TestCreateModifyIsNotInferred:
    def test_new_file_is_not_labelled_created(self, tmp_path):
        new_file = tmp_path / "fresh.py"
        new_file.write_text("print('hi')\n", encoding="utf-8")
        summary = build_change_summary(
            {"actions": [_write_action("fresh.py", None, "print('hi')\n")]},
            str(tmp_path),
        )
        rendered = format_change_summary(summary)
        assert summary["file_count"] == 1
        assert "Changed:" in rendered
        assert "fresh.py" in rendered
        assert "Created:" not in rendered
        assert "Modified:" not in rendered

    def test_missing_snapshot_does_not_prove_creation(self, tmp_path):
        """original_content None may mean 'read failed', not 'file was new'."""
        existing = tmp_path / "existing.py"
        existing.write_text("before\n", encoding="utf-8")
        existing.write_text("after\n", encoding="utf-8")
        summary = build_change_summary(
            {"actions": [_write_action("existing.py", None, "after\n")]},
            str(tmp_path),
        )
        rendered = format_change_summary(summary)
        assert "existing.py" in rendered
        assert "Created:" not in rendered
        assert "Modified:" not in rendered

    def test_rendered_summary_never_uses_create_or_modify_labels(self, tmp_path):
        first = _rewrite(tmp_path, "a.py", "1\n", "2\n")
        second = tmp_path / "b.py"
        second.write_text("x\n", encoding="utf-8")
        summary = build_change_summary(
            {"actions": [_write_action(first, "1\n", "2\n"), _write_action("b.py", None, "x\n")]},
            str(tmp_path),
        )
        rendered = format_change_summary(summary)
        assert "Created:" not in rendered
        assert "Modified:" not in rendered
        assert rendered.count("Changed:") == 1


# ─── command evidence boundary ────────────────────────────────────────────────


class TestCommandEvidenceBoundary:
    def test_successful_shell_command_adds_the_evidence_note(self, tmp_path):
        summary = build_change_summary(
            {
                "actions": [
                    {"tool": "run_command", "success": True, "args": {"command": "pytest"},
                     "output": "ok", "metadata": {"exit_code": 0}},
                ],
                "verification": {"success": True, "test_execution_status": "passed"},
            },
            str(tmp_path),
        )
        rendered = format_change_summary(summary)
        assert summary["ran_shell_commands"] is True
        assert "may not be captured" in rendered
        assert "exit_code" not in rendered

    def test_no_note_without_shell_commands(self, tmp_path):
        summary = build_change_summary(
            {"actions": [], "verification": {"success": True, "test_execution_status": "passed"}},
            str(tmp_path),
        )
        assert ran_shell_commands(summary) is False
        assert "may not be captured" not in format_change_summary(summary)

    def test_raw_command_output_is_not_in_the_summary(self, tmp_path):
        summary = build_change_summary(
            {
                "actions": [{
                    "tool": "run_command",
                    "success": True,
                    "args": {"command": "python", "arguments": ["-m", "unittest"]},
                    "output": "Ran 42 tests in 0.1s\nOK",
                    "metadata": {"exit_code": 0, "stdout": "Ran 42 tests in 0.1s\nOK"},
                }],
                "verification": {"success": True, "test_execution_status": "passed"},
            },
            str(tmp_path),
        )
        rendered = format_change_summary(summary)
        assert "Ran 42 tests" not in rendered
        assert "exit_code" not in rendered
        assert "-m unittest" not in rendered


# ─── GUI integration and fallback handling ────────────────────────────────────


def _rendered(window: MainWindow) -> str:
    return window.response_text.get("1.0", "end")


def _finish(window: MainWindow, result, error=None, tmp_path=None):
    window._active_gisai_task = SimpleNamespace(
        project_path=str(tmp_path) if tmp_path else "", status="completed"
    )
    window._finish_task(result, error, 0)


class TestGuiIntegration:
    def test_completed_task_renders_summary_in_result_area(self, tk_root, tmp_path):
        name = _rewrite(tmp_path, "app.py", "one\n", "one\ntwo\n")
        window = MainWindow(tk_root)
        try:
            _finish(window, {
                "coding_result": "Implemented the change.",
                "work_result": {
                    "status": "completed",
                    "actions": [_write_action(name, "one\n", "one\ntwo\n")],
                    "verification": {"success": True, "test_execution_status": "passed"},
                    "repair_attempts": 0,
                },
            }, tmp_path=tmp_path)
            rendered = _rendered(window)
            assert "Implemented the change." in rendered
            assert "CHANGE SUMMARY" in rendered
            assert "Files changed: 1" in rendered
            assert "Lines: +1 -0" in rendered
            assert f"Validation: {VALIDATION_PASS}" in rendered
            assert "Tests: passed" in rendered
            assert window.view_diff_btn.instate(["!disabled"])
        finally:
            window.root.destroy()

    def test_raw_work_result_payload_is_not_dumped_into_result_area(self, tk_root, tmp_path):
        window = MainWindow(tk_root)
        try:
            _finish(window, {
                "coding_result": "Implemented the change.",
                "work_result": {
                    "status": "completed",
                    "actions": [_write_action("app.py", "SECRET-BEFORE\n", "SECRET-AFTER\n")],
                    "verification": {"success": True, "test_execution_status": "passed"},
                },
            }, tmp_path=tmp_path)
            rendered = _rendered(window)
            assert "SECRET-BEFORE" not in rendered
            assert "work_result" not in rendered
        finally:
            window.root.destroy()

    def test_fallback_result_without_work_result_shows_no_summary(self, tk_root, tmp_path):
        window = MainWindow(tk_root)
        try:
            _finish(window, {"response": "coding-agent fallback answer"}, tmp_path=tmp_path)
            rendered = _rendered(window)
            assert "coding-agent fallback answer" in rendered
            assert "CHANGE SUMMARY" not in rendered
            assert window._last_change_summary is None
            assert window.view_diff_btn.instate(["disabled"])
        finally:
            window.root.destroy()

    def test_error_result_without_work_result_shows_no_summary(self, tk_root, tmp_path):
        window = MainWindow(tk_root)
        try:
            _finish(window, {"error": "boom: server exploded"}, tmp_path=tmp_path)
            rendered = _rendered(window)
            assert "boom: server exploded" in rendered
            assert "CHANGE SUMMARY" not in rendered
        finally:
            window.root.destroy()

    def test_failed_task_with_real_writes_still_reports_changes(self, tk_root, tmp_path):
        name = _rewrite(tmp_path, "half.py", "before\n", "after\n")
        window = MainWindow(tk_root)
        try:
            window._active_gisai_task = SimpleNamespace(project_path=str(tmp_path), status="failed")
            window._finish_task({
                "coding_result": "Real work failed.",
                "error": "Verification failed",
                "work_result": {
                    "status": "failed",
                    "error": "Verification failed",
                    "failure_reason": "verification_failed",
                    "actions": [_write_action(name, "before\n", "after\n")],
                    "verification": {"success": False, "errors": ["tests failed"],
                                     "test_execution_status": "failed"},
                    "repair_attempts": 1,
                },
            }, None, 0)
            rendered = _rendered(window)
            assert "CHANGE SUMMARY" in rendered
            assert "Files changed: 1" in rendered
            assert f"Validation: {VALIDATION_FAIL}" in rendered
            assert "Repairs: 1 attempted" in rendered
            assert "Errors:" in rendered
        finally:
            window.root.destroy()

    def test_cancelled_task_with_real_writes_still_reports_changes(self, tk_root, tmp_path):
        name = _rewrite(tmp_path, "partial.py", "before\n", "after\n")
        window = MainWindow(tk_root)
        try:
            window._active_gisai_task = SimpleNamespace(project_path=str(tmp_path), status="cancelled")
            window._finish_task({
                "error": "Task was cancelled",
                "work_result": {
                    "status": "cancelled",
                    "error": "Task was cancelled",
                    "actions": [_write_action(name, "before\n", "after\n")],
                    "verification": None,
                },
            }, None, 0)
            rendered = _rendered(window)
            assert "Operation cancelled" in rendered
            assert "CHANGE SUMMARY" in rendered
            assert "Files changed: 1" in rendered
        finally:
            window.root.destroy()

    def test_summary_is_persisted_to_chat_history(self, tk_root, tmp_path):
        name = _rewrite(tmp_path, "logged.py", "one\n", "one\ntwo\n")
        window = MainWindow(tk_root)
        try:
            _finish(window, {
                "coding_result": "Done.",
                "work_result": {
                    "status": "completed",
                    "actions": [_write_action(name, "one\n", "one\ntwo\n")],
                    "verification": {"success": True, "test_execution_status": "unavailable"},
                },
            }, tmp_path=tmp_path)
            stored = "".join(
                message.get("content", "")
                for message in window._current_chat_session()["history"]
            )
            assert "CHANGE SUMMARY" in stored
            assert "Tests: unavailable" in stored
        finally:
            window.root.destroy()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
