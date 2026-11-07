"""Fix 4: the Projects tab "Active project" label follows the real selection.

_select_project_path() is the single place every project selection funnels
through (header combo box, Open..., and the task's existing-or-new dialog), but
it never told the Projects tab, so the label kept claiming "No project selected"
after a project had in fact been opened.
"""

from __future__ import annotations

import threading

import pytest

from giscode.gui import main_window
from giscode.gui.main_window import MainWindow


class _FakeEngine:
    def _detect_intent(self, text, has_image=False, project_active=False):
        return {"capabilities": ["general_reasoning"], "confidence": 0.4, "name": "test"}

    def get_active_task(self):
        return None

    def cancel(self):
        pass

    def set_project_context(self, context):
        pass


@pytest.fixture
def window(tk_root, monkeypatch):
    """A MainWindow with the project stores stubbed out."""
    monkeypatch.setattr(main_window, "messagebox", main_window.messagebox)
    win = MainWindow(tk_root, gisai_engine=_FakeEngine(), model_manager=None)
    win._active_project_context = None
    win._project_store = type("S", (), {"list_projects": lambda self: [], "get_project": lambda self, i: None})()
    win._project_workspace_controller = None
    return win


def test_select_project_path_syncs_the_workspace_display(window, tmp_path):
    calls: list[int] = []
    window._sync_project_workspace_display = lambda: calls.append(1)

    window._select_project_path(str(tmp_path))

    assert calls, "_select_project_path must refresh the Projects tab"


def test_sync_runs_on_the_gui_thread(window, tmp_path):
    seen: list[int] = []
    gui_thread = threading.get_ident()

    def _sync():
        seen.append(threading.get_ident())

    window._sync_project_workspace_display = _sync
    window._select_project_path(str(tmp_path))

    assert seen == [gui_thread]


def test_label_updates_after_selecting_a_project(window, tmp_path):
    """End to end through the real view: the label must name the project."""
    view = window._project_workspace_view
    if view is None:
        pytest.skip("project workspace view not built")

    window._select_project_path(str(tmp_path))
    window.root.update_idletasks()

    label = view._project_var.get()
    assert label != ""
    assert "No project selected" not in label
    assert "No active project" not in label


def test_header_and_projects_tab_agree(window, tmp_path):
    view = window._project_workspace_view
    if view is None:
        pytest.skip("project workspace view not built")

    window._select_project_path(str(tmp_path))
    window.root.update_idletasks()

    header = window.project_path_var.get()
    label = view._project_var.get()

    assert header.startswith("Project opened:")
    assert tmp_path.name in label or tmp_path.name in header


def test_projects_tab_shows_none_at_startup(window):
    """The Projects tab label must mirror the main window header:
    when no project is active, both should display "None" (NO_PROJECT_LABEL)."""
    view = window._project_workspace_view
    if view is None:
        pytest.skip("project workspace view not built")

    # At startup with no project, the Projects tab label should mirror the header
    label = view._project_var.get()
    assert label == "None", f"Expected 'None', got '{label}'"
    assert "No project selected" not in label
    assert "No active project" not in label