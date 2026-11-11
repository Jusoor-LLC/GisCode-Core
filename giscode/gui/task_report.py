"""Truthful presentation helpers for completed real-work results.

Every value produced here is derived from evidence that RealWork already
recorded: ``work_result["actions"]`` (including the pre-write
``original_content`` snapshot captured by
``GisAiRealWorkExecutor._resolve_and_execute``), ``work_result["verification"]``,
``work_result["status"]``, ``work_result["error"]`` /
``work_result["failure_reason"]`` and ``work_result["repair_attempts"]``.

Two rules are deliberate and must not be relaxed:

    1. CREATE vs MODIFY is never inferred. ``WriteFileTool`` computes an internal
   ``ActionType`` but the serialized action record does not carry it, and
   ``original_content is None`` cannot distinguish "file did not exist" from
   "the pre-edit read failed". Files are therefore reported as ``Changed:``.

2. A validator reporting success only because no tests could be executed is
   reported as ``UNAVAILABLE``, never ``PASS``. "Tests passed" is only ever
   printed when the verifier reported ``test_execution_status == "passed"``.

This module adds no second diff engine and no filesystem tracking: the diff is
the same ``difflib.unified_diff`` over the same before/after pair that
``ExecutionHistoryStore.record_work_result`` already persists.
"""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Optional


VALIDATION_PASS = "PASS"
VALIDATION_FAIL = "FAIL"
VALIDATION_NOT_RUN = "NOT RUN"
VALIDATION_UNAVAILABLE = "UNAVAILABLE"

# Verifier test_execution_status values that are concrete enough to show. The
# "unknown" default is deliberately absent: no line is printed for it rather
# than guessing a word for it.
_TEST_STATUS_WORDS = {
    "passed": "passed",
    "failed": "failed",
    "unavailable": "unavailable",
}

_SHELL_COMMAND_NOTE = (
    "Structured file changes shown above. Changes made indirectly by shell "
    "commands may not be captured."
)


def _actions(work_result: dict) -> list:
    actions = work_result.get("actions") if isinstance(work_result, dict) else None
    return [item for item in (actions or []) if isinstance(item, dict)]


def _verification(work_result: dict) -> dict:
    """The recorded verification payload, or an empty dict when absent."""
    verification = work_result.get("verification") if isinstance(work_result, dict) else None
    return verification if isinstance(verification, dict) else {}


def validation_state(verification: Any) -> str:
    """The strongest validation claim the recorded evidence actually supports.

    ``PASS`` requires both a non-failing verification verdict and a test
    execution that actually ran and passed. A verifier that returned success
    only because no tests were available, no terminal executor was bound, or
    the frozen runtime could not execute them is ``UNAVAILABLE``.
    """
    if not isinstance(verification, dict) or not verification:
        return VALIDATION_NOT_RUN
    if verification.get("success") is False:
        return VALIDATION_FAIL
    if test_status_word(verification) == "passed":
        return VALIDATION_PASS
    return VALIDATION_UNAVAILABLE


def test_status_word(verification: Any) -> Optional[str]:
    """Concrete test-execution wording, or ``None`` when there is no evidence."""
    if not isinstance(verification, dict):
        return None
    status = str(verification.get("test_execution_status") or "").strip().lower()
    return _TEST_STATUS_WORDS.get(status)


def collect_errors(work_result: dict) -> list[str]:
    """Real errors recorded by RealWork, de-duplicated and order-preserving.

    Sources are the work result's own error/failure reason, the verifier error
    list, and the error text of each failed action. Returns an empty list when
    nothing actually failed.
    """
    candidates: list[str] = []
    error = work_result.get("error")
    if error:
        candidates.append(str(error))
    reason = work_result.get("failure_reason")
    if reason and str(reason) != str(error or ""):
        candidates.append(str(reason))
    for item in _verification(work_result).get("errors") or []:
        if item:
            candidates.append(str(item))
    for action in _actions(work_result):
        if action.get("success"):
            continue
        message = action.get("error")
        if message:
            candidates.append(f"{action.get('tool') or 'action'}: {message}")

    errors: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = candidate.strip()
        if text and text not in seen:
            seen.add(text)
            errors.append(text)
    return errors


def ran_shell_commands(work_result: dict) -> bool:
    """True when a ``run_command`` action actually succeeded.

    Shell commands can touch files without any structured write action, so
    this is what justifies telling the user where the evidence boundary is.
    """
    return any(
        action.get("tool") == "run_command" and action.get("success")
        for action in _actions(work_result)
    )


NO_NEWLINE_MARKER = "\\ No newline at end of file"


def render_unified_diff(lines: list[str]) -> str:
    """Join one file's unified-diff lines into text git can be compared with.

    ``difflib`` yields the last line of a file that has no trailing newline
    without one, so joining the lines of two files ran the first file's last
    hunk line into the next file's ``---`` header (``+    return a - b---
    app.py``). Each line is therefore terminated, and a line that lacked its own
    newline gets the standard ``\\ No newline at end of file`` marker that git
    prints. The diff itself is unchanged: same headers, same hunks, same order.
    """
    parts: list[str] = []
    for line in lines:
        terminated = line if line.endswith("\n") else f"{line}\n"
        parts.append(terminated)
        if not line.endswith("\n"):
            parts.append(f"{NO_NEWLINE_MARKER}\n")
    return "".join(parts)


def build_change_summary(work_result: dict, project_root: str) -> dict:
    # Core-owned classification: GisAI is optional, so this report must not
    # import the GisAI planner at module scope, and a copied set drifts.
    from giscode.agentic.tool_kinds import WRITE_ACTION_TOOLS

    files: list[dict] = []
    diffs: list[str] = []
    seen: set[str] = set()
    root = Path(project_root).resolve() if project_root else None
    for action in _actions(work_result):
        # Count a mutation when it is a known write tool OR when RealWork
        # recorded a pre-write snapshot for it. The snapshot is the stronger
        # evidence: it means the executor treated the action as a write, so the
        # report cannot miss a real change even if the tool sets drift apart
        # again. A locally duplicated list previously dropped ``set_docstring``
        # edits, reporting "No files were modified" for a file that had changed.
        if action.get("tool") not in WRITE_ACTION_TOOLS and action.get("original_content") is None:
            continue
        if not action.get("success"):
            continue
        raw_path = action.get("args", {}).get("path")
        if not isinstance(raw_path, str) or not raw_path.strip() or root is None:
            continue
        target = Path(raw_path)
        if not target.is_absolute():
            target = root / target
        target = target.resolve(strict=False)
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError:
            continue
        if relative in seen:
            continue
        seen.add(relative)
        before = action.get("original_content")
        if not isinstance(before, str):
            before = ""
        try:
            after = target.read_text(encoding="utf-8") if target.is_file() else ""
        except OSError:
            after = ""
        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        diff = list(difflib.unified_diff(before_lines, after_lines, fromfile=relative, tofile=relative))
        if not diff:
            continue
        additions = sum(1 for line in diff[2:] if line.startswith("+") and not line.startswith("+++") )
        deletions = sum(1 for line in diff[2:] if line.startswith("-") and not line.startswith("---") )
        files.append({"path": relative, "additions": additions, "deletions": deletions})
        diffs.append(render_unified_diff(diff))
    verification = _verification(work_result)
    try:
        repair_attempts = int(work_result.get("repair_attempts") or 0)
    except (TypeError, ValueError):
        repair_attempts = 0
    return {
        "files": files,
        "file_count": len(files),
        "additions": sum(item["additions"] for item in files),
        "deletions": sum(item["deletions"] for item in files),
        "diff": "".join(diffs),
        "status": work_result.get("status"),
        "verification": work_result.get("verification"),
        "verification_state": validation_state(verification),
        "test_status": test_status_word(verification),
        "repair_attempts": repair_attempts,
        "errors": collect_errors(work_result),
        "ran_shell_commands": ran_shell_commands(work_result),
    }


def format_change_summary(summary: dict) -> str:
    """Render the completed-task change summary shown in the result area."""
    files = summary.get("files") or []
    lines: list[str] = [
        "CHANGE SUMMARY",
        "",
        f"Files changed: {summary.get('file_count', 0)}",
    ]
    if files:
        lines.append("")
        lines.append("Changed:")
        for item in files:
            lines.append(f"{item['path']} +{item['additions']} -{item['deletions']}")
        lines.append("")
        lines.append(f"Lines: +{summary.get('additions', 0)} -{summary.get('deletions', 0)}")

    lines.append("")
    lines.append(f"Validation: {summary.get('verification_state') or VALIDATION_NOT_RUN}")
    test_status = summary.get("test_status")
    if test_status:
        lines.append(f"Tests: {test_status}")

    repairs = summary.get("repair_attempts") or 0
    if repairs:
        lines.append(f"Repairs: {repairs} attempted")

    errors = summary.get("errors") or []
    if errors:
        lines.append("")
        lines.append("Errors:")
        lines.extend(errors)

    if summary.get("ran_shell_commands"):
        lines.append("")
        lines.append(_SHELL_COMMAND_NOTE)
    return "\n".join(lines)


def build_honest_report(work_result: dict, project_root: str) -> str:
    """Render a report using only recorded actions, diffs, and verification.

    A passing claim requires both a non-empty structured diff and an actual
    passing test classification. Test output is copied from the verifier's
    recorded detail tail; this function never runs a command or infers a
    result from the task status.
    """
    summary = build_change_summary(work_result, project_root)
    files = summary.get("files") or []
    verification = _verification(work_result)
    status = str(verification.get("test_execution_status") or "unknown")
    passed = bool(files) and status == "passed" and verification.get("success") is True
    lines = []
    diff_text = str(summary.get("diff") or "")
    if files:
        lines.append(f"Files changed: {len(files)}")
        for item in files:
            lines.append(f"- {item['path']}: +{item['additions']} -{item['deletions']}")
    elif diff_text.strip():
        # Invariant: a non-empty recorded diff means something changed on disk.
        # Claiming "no files were modified" here would contradict the evidence,
        # so report the change even when it could not be attributed to a path.
        lines.append("Files changed: recorded diff is non-empty but no path could be attributed.")
    else:
        lines.append("No files were modified.")
    lines.append(f"Tests: {status}")
    if status == "could_not_run":
        lines.append("Tests could not run: " + "; ".join(collect_errors(work_result)))
    elif status == "no_tests_collected":
        lines.append("No tests were collected; this is not a pass or fail.")
    elif status == "failed":
        lines.append("Tests failed.")
    elif status == "passed":
        lines.append("Tests passed." if passed else "Tests passed, but no non-empty file diff was recorded.")
    details = verification.get("details") or []
    tails = [str(item) for item in details if "stdout" in str(item).lower() or "stderr" in str(item).lower()]
    if tails:
        lines.append("Output tail:")
        lines.extend(tails)
    rollback = any("rollback" in str(item).lower() for item in (work_result.get("observations") or []))
    lines.append(f"Rollback: {'performed' if rollback else 'not recorded'}")
    if work_result.get("plan_source") == "FAILED":
        lines.append("Fallback: deterministic fallback plan.")
    lines.append("Result: success" if passed else "Result: not successful")
    return "\n".join(lines)
