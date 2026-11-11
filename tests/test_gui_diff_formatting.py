"""The View Diff text must be a valid multi-file unified diff.

Each of these guards a real defect: per-file diffs used to be concatenated with
``"".join``, so a file without a trailing newline ran its last hunk line into the
next file's header, and the standard ``\\ No newline at end of file`` marker was
never emitted.
"""
from __future__ import annotations

from pathlib import Path

from giscode.gui.task_report import (
    NO_NEWLINE_MARKER,
    build_change_summary,
    format_change_summary,
    render_unified_diff,
)


def _summary(tmp_path: Path, writes: list[dict]) -> dict:
    return build_change_summary({"status": "completed", "actions": writes}, str(tmp_path))


def _headers(diff: str) -> list[str]:
    return [line[4:] for line in diff.splitlines() if line.startswith("--- ")]


def test_multiple_files_are_separated_when_first_lacks_trailing_newline(tmp_path):
    first = tmp_path / "calc.py"
    second = tmp_path / "app.py"
    first.write_text("def add(a, b):\n    return a + b", encoding="utf-8")  # no trailing newline
    second.write_text("print(add(2, 3))", encoding="utf-8")  # no trailing newline
    first.write_text(
        "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b", encoding="utf-8"
    )
    second.write_text("print(add(2, 3))\nprint(sub(5, 2))", encoding="utf-8")

    diff = _summary(tmp_path, [
        {"tool": "write_file", "success": True, "args": {"path": "calc.py"},
         "original_content": "def add(a, b):\n    return a + b"},
        {"tool": "write_file", "success": True, "args": {"path": "app.py"},
         "original_content": "print(add(2, 3))"},
    ])["diff"]

    assert _headers(diff) == ["calc.py", "app.py"]
    assert "--- app.py" in diff
    # The defect: a hunk line glued onto the next file's header.
    assert "b--- app.py" not in diff
    for line in diff.splitlines():
        if line.startswith("--- "):
            assert line.strip() == "--- " + line[4:].strip()


def test_files_with_and_without_trailing_newline_get_correct_markers(tmp_path):
    with_newline = tmp_path / "with_newline.py"
    without_newline = tmp_path / "without_newline.py"
    with_newline.write_text("value = 1\n", encoding="utf-8")
    without_newline.write_text("value = 2", encoding="utf-8")
    with_newline.write_text("value = 10\n", encoding="utf-8")
    without_newline.write_text("value = 20", encoding="utf-8")

    diff = _summary(tmp_path, [
        {"tool": "write_file", "success": True, "args": {"path": "with_newline.py"},
         "original_content": "value = 1\n"},
        {"tool": "write_file", "success": True, "args": {"path": "without_newline.py"},
         "original_content": "value = 2"},
    ])["diff"]

    blocks = diff.split("--- ")
    with_block = next(block for block in blocks if block.startswith("with_newline.py"))
    without_block = next(block for block in blocks if block.startswith("without_newline.py"))
    assert NO_NEWLINE_MARKER not in with_block
    assert NO_NEWLINE_MARKER in without_block
    lines = without_block.splitlines()
    # Both sides lack a trailing newline, so each side's last line carries the
    # marker: the removed one first, then the added one.
    assert lines[lines.index(NO_NEWLINE_MARKER) - 1].endswith("value = 2")
    assert lines[lines.index("+value = 20") + 1] == NO_NEWLINE_MARKER


def test_added_deleted_and_modified_files_all_render(tmp_path):
    added = tmp_path / "added.py"
    deleted = tmp_path / "deleted.py"
    modified = tmp_path / "modified.py"
    added.write_text("new = True\n", encoding="utf-8")
    deleted.write_text("", encoding="utf-8")  # the file is gone afterwards
    modified.write_text("value = 2\n", encoding="utf-8")

    summary = _summary(tmp_path, [
        {"tool": "write_file", "success": True, "args": {"path": "added.py"},
         "original_content": ""},
        {"tool": "write_file", "success": True, "args": {"path": "deleted.py"},
         "original_content": "gone = True\n"},
        {"tool": "write_file", "success": True, "args": {"path": "modified.py"},
         "original_content": "value = 1\n"},
    ])

    assert summary["file_count"] == 3
    assert _headers(summary["diff"]) == ["added.py", "deleted.py", "modified.py"]
    assert "+new = True" in summary["diff"]
    assert "-gone = True" in summary["diff"]
    assert "-value = 1" in summary["diff"] and "+value = 2" in summary["diff"]
    assert format_change_summary(summary).startswith("CHANGE SUMMARY")


def test_each_file_block_is_independent_and_ordered(tmp_path):
    paths = ["b.py", "a.py", "c.py"]
    writes = []
    for name in paths:
        target = tmp_path / name
        target.write_text("old\n", encoding="utf-8")
        target.write_text("new\n", encoding="utf-8")
        writes.append({"tool": "write_file", "success": True, "args": {"path": name},
                       "original_content": "old\n"})

    diff = _summary(tmp_path, writes)["diff"]
    assert _headers(diff) == paths
    assert diff.count("+++ ") == 3
    assert diff.count("@@ ") == 3


def test_empty_and_unavailable_diffs(tmp_path):
    empty = build_change_summary({"actions": []}, str(tmp_path))
    assert empty["diff"] == ""
    assert empty["file_count"] == 0

    same = tmp_path / "same.py"
    same.write_text("same\n", encoding="utf-8")
    unchanged = _summary(tmp_path, [
        {"tool": "write_file", "success": True, "args": {"path": "same.py"},
         "original_content": "same\n"},
    ])
    assert unchanged["diff"] == ""
    assert unchanged["file_count"] == 0

    outside = _summary(tmp_path, [
        {"tool": "write_file", "success": True,
         "args": {"path": str(tmp_path.parent / "elsewhere.py")},
         "original_content": "old\n"},
    ])
    assert outside["diff"] == ""
    assert outside["file_count"] == 0


def test_render_unified_diff_terminates_every_line():
    text = render_unified_diff(["--- f.py\n", "+++ f.py\n", "@@ -1 +1 @@\n", "-a", "+b"])
    assert text.endswith("\n")
    assert text.count(NO_NEWLINE_MARKER) == 2
    assert text.splitlines() == [
        "--- f.py", "+++ f.py", "@@ -1 +1 @@", "-a", NO_NEWLINE_MARKER,
        "+b", NO_NEWLINE_MARKER,
    ]
