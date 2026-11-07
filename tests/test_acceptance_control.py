"""Acceptance automation must be unreachable from an ordinary Core launch.

The automation that selects a project, loads a model and submits a task exists
only in the purpose-built acceptance executable, whose runtime hook sets
``sys._giscode_acceptance_build``. These tests pin that boundary: inherited
variables do nothing in a normal build, the data variables stay inert without
the explicit opt-in, and the evidence file can only land inside the dedicated
temporary directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import giscode.main as main_module

MARKER = main_module.ACCEPTANCE_BUILD_MARKER
DATA_VARIABLES = (
    "GISCODE_ACCEPTANCE_PROJECT",
    "GISCODE_ACCEPTANCE_MODEL",
    "GISCODE_ACCEPTANCE_REQUEST",
    "GISCODE_ACCEPTANCE_EVIDENCE",
)


class _TaskEntry:
    def __init__(self) -> None:
        self.text = ""

    def delete(self, *_args) -> None:
        self.text = ""

    def insert(self, _index, value: str) -> None:
        self.text = value


class _Var:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class _Window:
    def __init__(self) -> None:
        self.task_entry = _TaskEntry()
        self._model_var = _Var()
        self.project_path = ""
        self.model_selected = 0
        self.sent = 0
        self._active_task = None
        self._last_report = ""
        self._last_work_result = None
        self._gisai_engine = None
        self.response_text = self

    # The evidence writer reads this only when a status has no detail.
    def get(self, *_args):
        return "detail from the response pane"

    def _select_project_path(self, path: str) -> None:
        self.project_path = path

    def _on_model_selected(self) -> None:
        self.model_selected += 1

    def _send_task(self) -> None:
        self.sent += 1
        self._active_task = object()


class _Root:
    def __init__(self, window: _Window) -> None:
        self._giscode_main_window = window
        self.callbacks: dict[int, object] = {}
        self.destroyed = False

    def after(self, delay, callback):
        self.callbacks[delay] = callback

    def destroy(self) -> None:
        self.destroyed = True


class _Model:
    class state:  # noqa: N801 - mirrors the runtime enum's attribute
        value = "ready"


class _ModelManager:
    def __init__(self) -> None:
        self._inference = self

    def get_current_model(self):
        return _Model()

    def get_health(self):
        return {"state": "ready"}


def _set_data_env(monkeypatch, evidence: str, project: str = "C:/tmp/project") -> None:
    monkeypatch.setenv("GISCODE_ACCEPTANCE_PROJECT", project)
    monkeypatch.setenv("GISCODE_ACCEPTANCE_MODEL", "model.gguf")
    monkeypatch.setenv("GISCODE_ACCEPTANCE_REQUEST", "create a file")
    monkeypatch.setenv("GISCODE_ACCEPTANCE_EVIDENCE", evidence)


def _evidence_root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path / main_module.ACCEPTANCE_EVIDENCE_ROOT


def test_inherited_variables_do_nothing_in_a_production_build(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, MARKER, raising=False)
    _set_data_env(monkeypatch, "evidence.json")
    monkeypatch.setenv(main_module.ACCEPTANCE_OPT_IN_ENV, "1")
    root = _Root(_Window())

    main_module._schedule_gui_acceptance(root, _ModelManager())

    assert main_module._acceptance_automation_enabled() is False
    assert root.callbacks == {}, "a production launch must not schedule acceptance callbacks"
    assert root._giscode_main_window.sent == 0
    assert root._giscode_main_window.project_path == ""
    assert list(tmp_path.rglob("evidence.json")) == []


def test_data_variables_without_opt_in_are_inert(monkeypatch):
    monkeypatch.setattr(sys, MARKER, True, raising=False)
    _set_data_env(monkeypatch, "evidence.json")
    for value in ("", "0", "yes", "true"):
        monkeypatch.setenv(main_module.ACCEPTANCE_OPT_IN_ENV, value)
        assert main_module._acceptance_automation_enabled() is False, value


@pytest.mark.parametrize(
    "present",
    [
        ("GISCODE_ACCEPTANCE_PROJECT",),
        ("GISCODE_ACCEPTANCE_MODEL", "GISCODE_ACCEPTANCE_REQUEST"),
        ("GISCODE_ACCEPTANCE_PROJECT", "GISCODE_ACCEPTANCE_EVIDENCE"),
        ("GISCODE_ACCEPTANCE_MODEL", "GISCODE_ACCEPTANCE_EVIDENCE"),
    ],
)
def test_partial_configuration_does_not_submit(present, monkeypatch):
    monkeypatch.setattr(sys, MARKER, True, raising=False)
    monkeypatch.setenv(main_module.ACCEPTANCE_OPT_IN_ENV, "1")
    values = {
        "GISCODE_ACCEPTANCE_PROJECT": "C:/tmp/project",
        "GISCODE_ACCEPTANCE_MODEL": "model.gguf",
        "GISCODE_ACCEPTANCE_REQUEST": "create a file",
        "GISCODE_ACCEPTANCE_EVIDENCE": "evidence.json",
    }
    for name, value in values.items():
        if name in present:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    root = _Root(_Window())

    main_module._schedule_gui_acceptance(root, _ModelManager())

    assert root.callbacks == {}
    assert root._giscode_main_window.sent == 0


def test_malformed_configuration_does_not_submit(monkeypatch):
    monkeypatch.setattr(sys, MARKER, True, raising=False)
    monkeypatch.setenv(main_module.ACCEPTANCE_OPT_IN_ENV, "1")
    _set_data_env(monkeypatch, "   ")
    root = _Root(_Window())

    main_module._schedule_gui_acceptance(root, _ModelManager())

    assert root.callbacks == {}
    assert root._giscode_main_window.sent == 0


def test_opt_in_acceptance_build_submits_and_writes_confined_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, MARKER, True, raising=False)
    monkeypatch.setenv(main_module.ACCEPTANCE_OPT_IN_ENV, "1")
    _set_data_env(monkeypatch, "evidence.json")
    root = _evidence_root(tmp_path, monkeypatch)
    window = _Window()
    gui = _Root(window)

    main_module._schedule_gui_acceptance(gui, _ModelManager())

    assert window.project_path == "C:/tmp/project"
    assert window._model_var.value == "model.gguf"
    assert window.model_selected == 1
    assert 500 in gui.callbacks, "acceptance must poll the model through the ordinary callback"

    gui.callbacks[500]()  # poll_model: the model is ready, so the task is sent
    assert window.sent == 1
    assert window.task_entry.text == "create a file"
    assert 250 in gui.callbacks

    window._active_task = None
    window._last_report = "task completed"
    gui.callbacks[250]()  # poll_result: finished, so evidence is written

    evidence = root / "evidence.json"
    assert evidence.is_file()
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["status"] == "COMPLETED"
    assert evidence.parent == root


@pytest.mark.skipif(os.name != "nt", reason="asserts Windows path-separator refusal")
@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        ".",
        "..",
        "../escape.json",
        "..\\escape.json",
        "sub/dir.json",
        "sub\\dir.json",
        "C:\\Windows\\win.ini",
        "C:evidence.json",
        "/absolute.json",
        "\\\\server\\share\\evidence.json",
        "\\\\?\\C:\\Windows\\win.ini",
    ],
)
def test_evidence_paths_outside_the_dedicated_directory_are_refused(value, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))

    assert main_module._acceptance_evidence_path(value) is None


def test_existing_file_outside_the_directory_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))
    outside = tmp_path / "keep.txt"
    outside.write_text("original", encoding="utf-8")

    assert main_module._acceptance_evidence_path(str(outside)) is None
    assert main_module._acceptance_evidence_path("../keep.txt") is None
    assert outside.read_text(encoding="utf-8") == "original"


def test_evidence_inside_the_directory_may_be_refreshed(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))
    first = main_module._acceptance_evidence_path("evidence.json")
    assert first is not None
    first.write_text('{"status": "FAILED"}', encoding="utf-8")

    second = main_module._acceptance_evidence_path("evidence.json")
    assert second == first
    second.write_text('{"status": "COMPLETED"}', encoding="utf-8")
    assert json.loads(first.read_text(encoding="utf-8"))["status"] == "COMPLETED"


def test_evidence_directory_named_like_a_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))
    root = tmp_path / main_module.ACCEPTANCE_EVIDENCE_ROOT
    (root / "taken.json").mkdir(parents=True, exist_ok=True)

    assert main_module._acceptance_evidence_path("taken.json") is None


def _junction(link: Path, target: Path) -> bool:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and link.exists()


def _hard_link(link: Path, target: Path) -> bool:
    try:
        os.link(target, link)
        return True
    except OSError:
        return False


def _raise_oserror():
    raise OSError("denied")


def _file_symlink(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link)
        return True
    except OSError:
        return False


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_junction_at_the_evidence_root_is_refused(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / main_module.ACCEPTANCE_EVIDENCE_ROOT
    if not _junction(root, outside):
        pytest.skip("junction creation is unavailable in this session")
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))

    assert main_module._acceptance_evidence_path("evidence.json") is None
    assert main_module._acceptance_write_evidence("evidence.json", "payload") is None
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_directory_junction_as_the_target_name_is_refused(tmp_path, monkeypatch):
    root = tmp_path / main_module.ACCEPTANCE_EVIDENCE_ROOT
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if not _junction(root / "evidence.json", outside):
        pytest.skip("junction creation is unavailable in this session")
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))

    assert main_module._acceptance_evidence_path("evidence.json") is None
    assert main_module._acceptance_write_evidence("evidence.json", "payload") is None
    assert list(outside.iterdir()) == []


def test_existing_hard_linked_destination_keeps_the_alias_unchanged(tmp_path, monkeypatch):
    root = tmp_path / main_module.ACCEPTANCE_EVIDENCE_ROOT
    root.mkdir(parents=True)
    alias = tmp_path / "alias.txt"
    alias.write_text("original", encoding="utf-8")
    if not _hard_link(root / "evidence.json", alias):
        pytest.skip("hard links are unavailable in this session")
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))

    written = main_module._acceptance_write_evidence("evidence.json", '{"status": "COMPLETED"}')

    assert written is not None
    assert json.loads(written.read_text(encoding="utf-8"))["status"] == "COMPLETED"
    assert written.stat().st_nlink == 1
    assert alias.read_text(encoding="utf-8") == "original"


def test_destination_swapped_to_a_hard_link_mid_write_keeps_the_alias(tmp_path, monkeypatch):
    root = tmp_path / main_module.ACCEPTANCE_EVIDENCE_ROOT
    root.mkdir(parents=True)
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))
    destination = root / "evidence.json"
    assert main_module._acceptance_evidence_path("evidence.json") is not None

    alias = tmp_path / "alias.txt"
    alias.write_text("original", encoding="utf-8")
    if not _hard_link(tmp_path / "probe.link", alias):
        pytest.skip("hard links are unavailable in this session")
    (tmp_path / "probe.link").unlink()

    real_fsync = os.fsync
    state = {"swapped": False}

    def fsync_then_swap(descriptor):
        if not state["swapped"]:
            state["swapped"] = True
            os.link(alias, destination)
        return real_fsync(descriptor)

    monkeypatch.setattr(main_module.os, "fsync", fsync_then_swap)
    written = main_module._acceptance_write_evidence("evidence.json", '{"status": "COMPLETED"}')

    assert written is not None
    assert json.loads(destination.read_text(encoding="utf-8"))["status"] == "COMPLETED"
    assert destination.stat().st_nlink == 1
    assert alias.read_text(encoding="utf-8") == "original"


def test_destination_swapped_to_a_junction_mid_write_is_refused(tmp_path, monkeypatch):
    root = tmp_path / main_module.ACCEPTANCE_EVIDENCE_ROOT
    root.mkdir(parents=True)
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))
    destination = root / "evidence.json"
    assert main_module._acceptance_evidence_path("evidence.json") is not None
    outside = tmp_path / "outside"
    outside.mkdir()

    real_fsync = os.fsync
    state = {"swapped": False}

    def fsync_then_junction(descriptor):
        if not state["swapped"]:
            state["swapped"] = True
            if not _junction(destination, outside):
                raise OSError("junction unavailable")
        return real_fsync(descriptor)

    monkeypatch.setattr(main_module.os, "fsync", fsync_then_junction)
    try:
        written = main_module._acceptance_write_evidence("evidence.json", '{"status": "COMPLETED"}')
    except OSError:
        pytest.skip("junction creation is unavailable in this session")

    assert written is None, "a reparse destination must fail closed"
    assert list(outside.iterdir()) == []
    assert [item.name for item in root.iterdir() if item.suffix == ".tmp"] == []


def test_directory_swapped_to_a_junction_mid_write_is_refused(tmp_path, monkeypatch):
    root = tmp_path / main_module.ACCEPTANCE_EVIDENCE_ROOT
    root.mkdir(parents=True)
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))
    assert main_module._acceptance_evidence_path("evidence.json") is not None
    outside = tmp_path / "outside"
    outside.mkdir()

    real_fsync = os.fsync
    state = {"swapped": False}

    def fsync_then_replace_root(descriptor):
        if not state["swapped"]:
            state["swapped"] = True
            shutil.rmtree(root, ignore_errors=True)
            if not _junction(root, outside):
                raise OSError("junction unavailable")
        return real_fsync(descriptor)

    monkeypatch.setattr(main_module.os, "fsync", fsync_then_replace_root)
    try:
        written = main_module._acceptance_write_evidence("evidence.json", '{"status": "COMPLETED"}')
    except OSError:
        pytest.skip("junction creation is unavailable in this session")

    assert written is None
    assert list(outside.iterdir()) == []


def test_failed_publish_leaves_no_temporary_files(tmp_path, monkeypatch):
    root = tmp_path / main_module.ACCEPTANCE_EVIDENCE_ROOT
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        main_module.os, "replace", lambda *args, **kwargs: _raise_oserror()
    )

    assert main_module._acceptance_write_evidence("evidence.json", "payload") is None
    assert [item.name for item in root.iterdir()] == []


def test_in_root_refresh_still_writes(tmp_path, monkeypatch):
    root = tmp_path / main_module.ACCEPTANCE_EVIDENCE_ROOT
    monkeypatch.setattr(main_module.tempfile, "gettempdir", lambda: str(tmp_path))

    first = main_module._acceptance_write_evidence("evidence.json", '{"status": "FAILED"}')
    assert first is not None and first.parent == root
    second = main_module._acceptance_write_evidence("evidence.json", '{"status": "COMPLETED"}')
    assert second == first
    assert json.loads(first.read_text(encoding="utf-8"))["status"] == "COMPLETED"
    assert first.stat().st_nlink == 1
