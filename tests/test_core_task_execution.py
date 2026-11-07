"""Core task execution must work with GisAI absent.

These tests cover the deterministic dispatcher directly and the real GUI send
path with ``gisai_engine=None``. They also prove the Core module does not pull
the assistant package into a fresh interpreter.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

import pytest
import tkinter as tk

from giscode.agentic.core_tasks import (
    CoreOperation,
    UnsupportedOperation,
    execute_request,
    parse_operation,
    request_needs_workspace,
)
from giscode.gui.main_window import MainWindow
from giscode.task_control.manager import TaskManager

REQUEST = (
    "Create a new file named proof.txt in the project root. "
    "Its contents must be exactly this single line: GisCode Core deterministic proof"
)
CONTENT = "GisCode Core deterministic proof"


def test_core_module_does_not_import_gisai_in_a_fresh_process():
    code = (
        "import sys;"
        "import giscode.agentic.core_tasks as m;"
        "assert not any(name == 'giscode.gisai' or name.startswith('giscode.gisai.')"
        " for name in sys.modules), sorted(sys.modules);"
        "print('CORE_IMPORT_OK')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    assert "CORE_IMPORT_OK" in completed.stdout


def test_parse_operation_extracts_name_and_exact_content():
    operation = parse_operation(REQUEST)
    assert operation == CoreOperation(path="proof.txt", content=CONTENT)


def test_parse_operation_rejects_unrelated_requests():
    with pytest.raises(UnsupportedOperation):
        parse_operation("Please refactor the parser")
    with pytest.raises(UnsupportedOperation):
        parse_operation("Create a new file named proof.txt in the project root.")


def test_request_needs_workspace_detects_file_work():
    assert request_needs_workspace(REQUEST) is True
    assert request_needs_workspace("hello there") is False


def test_create_file_is_verified_and_reported(tmp_path):
    result = execute_request(REQUEST, str(tmp_path), task_id="core-test")

    target = tmp_path / "proof.txt"
    assert result["success"] is True
    assert result["error"] is None
    assert target.read_text(encoding="utf-8") == CONTENT
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    assert digest in result["response"]
    assert str(target) in result["response"]
    assert "Completed" in result["response"]
    assert result["work_result"]["verification"]["success"] is True
    assert result["work_result"]["actions"][0]["tool"] == "write_file"


def test_existing_file_is_not_overwritten(tmp_path):
    target = tmp_path / "proof.txt"
    target.write_text("pre-existing content", encoding="utf-8")

    result = execute_request(REQUEST, str(tmp_path), task_id="core-existing")

    assert result["success"] is False
    assert "already exists" in (result["error"] or "").lower()
    assert target.read_text(encoding="utf-8") == "pre-existing content"


def test_unsupported_request_is_explained_and_changes_nothing(tmp_path):
    result = execute_request("Please refactor the parser", str(tmp_path), task_id="core-unsup")

    assert result["success"] is False
    assert "cannot carry out that request without GisAI" in result["response"]
    assert "Nothing was changed" in result["response"]
    assert list(tmp_path.iterdir()) == []


def test_failed_verification_rolls_back_and_never_reports_success(tmp_path, monkeypatch):
    import giscode.agentic.core_tasks as core_tasks

    monkeypatch.setattr(core_tasks, "verify_written_file", lambda path, expected: (False, "", 0))

    result = execute_request(REQUEST, str(tmp_path), task_id="core-verify")

    assert result["success"] is False
    assert "verification failed" in (result["error"] or "").lower()
    assert not (tmp_path / "proof.txt").exists()
    assert any("rollback" in note for note in result["work_result"]["observations"])


def test_cancellation_before_write_creates_nothing(tmp_path):
    result = execute_request(
        REQUEST, str(tmp_path), task_id="core-cancel", should_cancel=lambda: True
    )

    assert result["success"] is False
    assert "cancel" in (result["error"] or "").lower()
    assert not (tmp_path / "proof.txt").exists()


def test_timeout_is_reported_without_a_file(tmp_path):
    result = execute_request(
        REQUEST, str(tmp_path), task_id="core-timeout", deadline_s=0.0
    )

    assert result["success"] is False
    assert "time limit" in (result["error"] or "").lower()
    assert not (tmp_path / "proof.txt").exists()


class _NoopModelManager:
    """Core send must not need a model, but the window still stores a manager."""

    def __init__(self):
        self._settings = {}

    def get_current_model(self):
        return None

    def get_health(self):
        return {"model_loaded": False}


def _make_core_window(root):
    return MainWindow(root, task_manager=TaskManager(), model_manager=_NoopModelManager(), gisai_engine=None)


def _wait_idle(window, timeout=10.0):
    deadline = time.time() + timeout
    while window._active_task and time.time() < deadline:
        window.root.update()
        time.sleep(0.01)
    assert not window._active_task


def test_gui_submits_core_task_with_no_gisai_engine(tk_root, tmp_path):
    window = _make_core_window(tk_root)
    window._select_project_path(str(tmp_path))
    window.task_entry.delete("1.0", tk.END)
    window.task_entry.insert("1.0", REQUEST)

    window._send_task()
    _wait_idle(window)

    target = tmp_path / "proof.txt"
    assert target.is_file()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    report = window._last_report
    assert "Completed" in report
    assert str(target) in report
    assert digest in report
    assert window._last_work_result is not None


def test_gui_explains_unsupported_task_without_gisai_engine(tk_root, tmp_path):
    window = _make_core_window(tk_root)
    window._select_project_path(str(tmp_path))
    window.task_entry.delete("1.0", tk.END)
    window.task_entry.insert("1.0", "Please refactor the parser")

    window._send_task()
    _wait_idle(window)

    assert "without GisAI" in window._last_report
    assert not (tmp_path / "proof.txt").exists()
