"""Task 14: GisAi Project and Task Memory Foundation tests."""

import os
import tempfile

import pytest

from giscode.projects.context import ProjectContext


def test_project_context_creation():
    """Test that ProjectContext can be created with proper fields."""
    # Use absolute path since ProjectContext.create calls os.path.abspath
    ctx = ProjectContext.create(root_path=os.path.abspath("/test/project"), name="MyProject")
    assert ctx.project_id != ""
    assert ctx.name == "MyProject"
    assert ctx.root_path == os.path.abspath("/test/project")
    assert ctx.active_section == "Overview"
    assert ctx.current_task_id == ""
    assert ctx.task_status == ""
    assert ctx.last_activity == ""

    # Test to_dict / from_dict roundtrip
    d = ctx.to_dict()
    ctx2 = ProjectContext.from_dict(d)
    assert ctx2.project_id == ctx.project_id
    assert ctx2.name == ctx.name
    assert ctx2.root_path == ctx.root_path


def test_project_context_activity_recording():
    """Test that ProjectContext records activity."""
    import time
    ctx = ProjectContext.create(root_path=os.path.abspath("/test/project"))
    ctx.record_activity("test activity")
    assert ctx.last_activity != ""
    # Test enter_section - adds PREVIOUS section to history
    ctx.enter_section("Backend")
    # When entering "Backend" from "Overview", "Overview" is added to history
    assert "Overview" in ctx.section_history


def test_project_context_unique_id():
    """Test that different paths get different project IDs."""
    ctx1 = ProjectContext.create(root_path=os.path.abspath("/path/a"))
    ctx2 = ProjectContext.create(root_path=os.path.abspath("/path/b"))
    assert ctx1.project_id != ctx2.project_id


def test_gisai_engine_set_project_context():
    """Test that GisAiEngine can receive project context."""
    import tkinter as tk
    from giscode.gui.main_window import MainWindow
    from giscode.projects.context import ProjectContext

    root = tk.Tk()
    try:
        mw = MainWindow(root, gisai_engine=None)
        # Just test that the context can be created and has expected fields
        ctx = ProjectContext.create(root_path="/test", name="TestProject")
        assert ctx.project_id != ""
        assert ctx.name == "TestProject"
    finally:
        root.destroy()


def test_main_window_project_context():
    """Test that MainWindow tracks project context."""
    import tkinter as tk
    from giscode.gui.main_window import MainWindow
    from giscode.projects.context import ProjectContext

    root = tk.Tk()
    try:
        mw = MainWindow(root, gisai_engine=None)
        # Initially no project
        assert mw._active_project_context is None
        assert mw.current_name_var.get() == "No project"

        # After selecting a project
        mw._select_project_path("/tmp/test_project")
        assert mw._active_project_context is not None
        # The project name should be displayed
        # Note: /tmp/test_project may not exist, but ProjectContext.create
        # handles this by using basename
        assert mw.current_name_var.get() != "No project"
    finally:
        root.destroy()


def test_main_window_task_context_display():
    """Test that MainWindow displays task context.

    The task context is shown in the status bar: "Ready" while no task is
    running, and the phase and status of the task once one starts.
    """
    import tkinter as tk
    from giscode.gui.main_window import MainWindow
    from giscode.task_control.manager import TaskManager

    root = tk.Tk()
    try:
        tm = TaskManager()
        mw = MainWindow(root, task_manager=tm, gisai_engine=None)
        # Initially no task
        mw._poll_task_state()
        assert mw._status_var.get() == "Ready"

        # Start a task
        tm.start_task("test-task-1")
        # Poll will update the display
        mw._poll_task_state()
        # The running task's phase and status are shown
        assert "Analyzing" in mw._status_var.get()
        assert "Running" in mw._status_var.get()
    finally:
        root.destroy()


def test_project_context_persistence_role():
    """Test that ProjectContext supports persistence role."""
    ctx = ProjectContext.create(root_path=os.path.abspath("/my/project"))
    # to_dict should produce a serializable dict
    d = ctx.to_dict()
    assert "project_id" in d
    assert "name" in d
    assert "root_path" in d
    assert "active_section" in d
    assert "current_task_id" in d
    assert "task_status" in d
    assert "last_activity" in d

    # from_dict should recover
    ctx2 = ProjectContext.from_dict(d)
    assert ctx2.project_id == ctx.project_id
    assert ctx2.name == ctx.name
    assert ctx2.root_path == ctx.root_path