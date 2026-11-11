"""GUI transaction recovery: discovery, confirmation, results, and retry."""

from __future__ import annotations

import json
import sys

import pytest

from giscode.agentic.write_safety import SnapshotManager, atomic_write_text
from giscode.gui import recovery_panel as rp
from giscode.gui.main_window import MainWindow


def _project(tmp_path, name="project"):
    root = tmp_path / name
    root.mkdir()
    return root


def _profile(home, key="proj"):
    return home / "projects" / key


def _interrupt(
    profile,
    project,
    *,
    task="task-1",
    relative="calc.py",
    before="VALUE = 1\n",
    after="VALUE = 2\n",
):
    target = project / relative
    target.write_text(before, encoding="utf-8")
    snapshot = SnapshotManager(task, profile_root=profile)
    snapshot.snapshot(target, project)
    atomic_write_text(target, after)
    snapshot.record_written(target, project)
    return snapshot, target


def _panel(tk_root, project, home):
    panel = rp.RecoveryPanel(
        tk_root, project_root_provider=lambda: str(project), home_root=home
    )
    panel.set_project(str(project))
    return panel


def _labels(panel):
    return {
        panel._tree.item(iid, "values")[0] for iid in panel._tree.get_children()
    }


def _outcome_text(panel):
    return panel._outcome.get("1.0", "end").strip()


def test_no_pending_transactions_keeps_the_panel_compact(tk_root, tmp_path):
    project = _project(tmp_path)
    panel = _panel(tk_root, project, tmp_path / "home")

    assert panel.records == []
    assert "No unfinished transactions" in panel.status_var.get()
    assert panel._tree.get_children() == ()
    assert panel._body.winfo_manager() == ""
    assert str(panel._recover_btn.cget("state")) == "disabled"
    assert str(panel._recover_all_btn.cget("state")) == "disabled"


def test_lists_one_and_multiple_pending_transactions(tk_root, tmp_path):
    project = _project(tmp_path)
    home = tmp_path / "home"
    profile = _profile(home)
    _interrupt(profile, project)
    _interrupt(
        profile,
        project,
        task="task-2",
        relative="other.py",
        before="OTHER = 1\n",
        after="OTHER = 2\n",
    )

    panel = _panel(tk_root, project, home)

    assert len(panel.records) == 2
    assert len(panel._tree.get_children()) == 2
    assert _labels(panel) == {"Interrupted"}
    assert "2 transaction(s) need attention" in panel.status_var.get()
    assert str(panel._recover_btn.cget("state")) == "normal"
    assert panel._body.winfo_manager() == "pack"


def test_successful_recovery_restores_files_and_reports_it(tk_root, tmp_path, monkeypatch):
    project = _project(tmp_path)
    home = tmp_path / "home"
    _snapshot, target = _interrupt(_profile(home), project)
    panel = _panel(tk_root, project, home)
    monkeypatch.setattr(rp.messagebox, "askyesno", lambda *a, **k: True)

    results = panel.recover_all(wait=True)

    assert [item["status"] for item in results] == ["recovered"]
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _labels(panel) == {"Recovered"}
    assert "recovered" in _outcome_text(panel).lower()
    assert "Recovery finished" in panel.status_var.get()
    assert str(panel._recover_btn.cget("state")) == "disabled"


def test_user_cancellation_changes_nothing(tk_root, tmp_path, monkeypatch):
    project = _project(tmp_path)
    home = tmp_path / "home"
    _snapshot, target = _interrupt(_profile(home), project)
    panel = _panel(tk_root, project, home)
    monkeypatch.setattr(rp.messagebox, "askyesno", lambda *a, **k: False)

    results = panel.recover_all(wait=True)

    assert results == []
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert "cancelled" in _outcome_text(panel).lower()
    assert "cancelled" in panel.status_var.get().lower()


def test_external_modification_is_reported_and_not_overwritten(
    tk_root, tmp_path, monkeypatch
):
    project = _project(tmp_path)
    home = tmp_path / "home"
    _snapshot, target = _interrupt(_profile(home), project)
    target.write_text("USER = 9\n", encoding="utf-8")
    panel = _panel(tk_root, project, home)
    monkeypatch.setattr(rp.messagebox, "askyesno", lambda *a, **k: True)

    results = panel.recover_all(wait=True)

    assert results[0]["status"] == "recovery_required"
    assert target.read_text(encoding="utf-8") == "USER = 9\n"
    assert "changed after" in _outcome_text(panel).lower()
    assert "needs attention" in panel.status_var.get()


def test_corrupt_metadata_is_reported_not_hidden(tk_root, tmp_path):
    project = _project(tmp_path)
    home = tmp_path / "home"
    _interrupt(_profile(home), project)
    (_profile(home) / "transactions" / "task-1.json").write_text(
        "{not json", encoding="utf-8"
    )

    panel = _panel(tk_root, project, home)

    assert len(panel.records) == 1
    assert "could not be read" in panel.records[0]["problem"]
    assert "could not be read" in panel.status_var.get()
    assert str(panel._recover_btn.cget("state")) == "disabled"


def test_failed_recovery_then_retry_succeeds(tk_root, tmp_path, monkeypatch):
    project = _project(tmp_path)
    home = tmp_path / "home"
    snapshot, target = _interrupt(_profile(home), project)
    saved = (snapshot.root / "calc.py").read_bytes()
    (snapshot.root / "calc.py").unlink()
    panel = _panel(tk_root, project, home)
    monkeypatch.setattr(rp.messagebox, "askyesno", lambda *a, **k: True)

    first = panel.recover_all(wait=True)
    assert first[0]["status"] == "recovery_required"
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert "needs attention" in panel.status_var.get()

    (snapshot.root / "calc.py").write_bytes(saved)
    panel.refresh()
    second = panel.recover_all(wait=True)
    assert second[0]["status"] == "recovered"
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_restart_shows_unresolved_transaction_again(tk_root, tmp_path, monkeypatch):
    project = _project(tmp_path)
    home = tmp_path / "home"
    snapshot, target = _interrupt(_profile(home), project)
    (snapshot.root / "calc.py").unlink()
    monkeypatch.setattr(rp.messagebox, "askyesno", lambda *a, **k: True)
    _panel(tk_root, project, home).recover_all(wait=True)

    reopened = _panel(tk_root, project, home)

    assert [record["status"] for record in reopened.records] == ["recovery_required"]
    assert _labels(reopened) == {"Recovery required"}
    assert "1 transaction(s) need attention" in reopened.status_var.get()
    assert str(reopened._recover_btn.cget("state")) == "normal"


def test_recovery_is_scoped_to_the_selected_project(tk_root, tmp_path):
    home = tmp_path / "home"
    first = _project(tmp_path, "one")
    second = _project(tmp_path, "two")
    _interrupt(_profile(home, "profile-one"), first, task="task-one")
    _interrupt(_profile(home, "profile-two"), second, task="task-two")

    panel = _panel(tk_root, first, home)

    assert [record["task"] for record in panel.records] == ["task-one"]


def test_panel_does_not_import_gisai(tk_root, tmp_path, monkeypatch):
    project = _project(tmp_path)
    home = tmp_path / "home"
    _snapshot, target = _interrupt(_profile(home), project)
    panel = _panel(tk_root, project, home)
    monkeypatch.setattr(rp.messagebox, "askyesno", lambda *a, **k: True)

    panel.recover_all(wait=True)

    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert [
        name for name in sys.modules if name == "giscode.gisai" or name.startswith("giscode.gisai.")
    ] == []


def test_rolled_back_record_is_labelled_and_not_pending(tk_root, tmp_path):
    project = _project(tmp_path)
    home = tmp_path / "home"
    _interrupt(_profile(home), project)
    record = _profile(home) / "transactions" / "task-1.json"
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["status"] = "rolled_back"
    record.write_text(json.dumps(payload), encoding="utf-8")

    panel = _panel(tk_root, project, home)

    # A finished automatic rollback must not masquerade as unresolved work.
    assert _labels(panel) == {"Rolled back"}
    assert "No unfinished transactions" in panel.status_var.get()
    assert str(panel._recover_btn.cget("state")) == "disabled"


def test_projects_tab_builds_a_visible_recovery_panel(tk_root):
    window = MainWindow(tk_root, gisai_engine=None, model_manager=None)
    window._notebook.select(window._tab_frames["Projects"])
    tk_root.update_idletasks()

    assert hasattr(window, "_recovery_panel")
    # The panel is packed at the bottom before the expanding workspace, so it
    # reserves its strip instead of being squeezed out of the page.
    assert window._recovery_panel.winfo_manager() == "pack"
    assert str(window._recovery_panel.pack_info().get("side")) == "bottom"
    assert window._project_root_for_recovery() is None
    assert "No project selected" in window._recovery_panel.status_var.get()
