"""Regression tests for the consolidated project control area.

Two reported GUI problems
-------------------------
1. The project controls looked duplicated / overlapped.
2. Clicking **Open** reported ``Select a project from the list first``
   even though the user had just picked an existing project.

Root causes
-----------
The window header (``MainWindow._build_ui``) built a project control area
(combobox + Open / Settings) and ``ProjectWorkspaceView._build_ui``
independently built a *second* one inside the Projects tab (its own combobox
+ Start New Project / Open / Close / Settings). Each copy owned a private
``StringVar``, so a project picked in one was invisible to the other. The
Projects-tab combobox had no ``<<ComboboxSelected>>`` binding at all, so
selecting there recorded nothing, and both ``Settings`` handlers read the
*active* project instead of the user's *selection*.

Fix
---
The header is the single authoritative project control area. Selection is
recorded once in ``MainWindow._selected_project_id`` and every project action
reads it. The Projects tab mirrors that state read-only.

These tests pin the widget counts, the selection state, and the exact
end-to-end user flow that used to fail.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk

import pytest

from giscode import edition
from giscode.gui.main_window import NAV_TABS, MainWindow
from giscode.projects import Project, ProjectStore
from giscode.projects.context import ProjectContextStore
from giscode.storage import ChatStore
from tests.conftest import _create_tk_root, _safe_destroy

# Skip all GUI tests if Tk cannot initialise: the shared probe retries the
# transient init.tcl race, so a single unlucky attempt cannot disable the module.
from tests.conftest import tk_available, tk_skip_reason

_TK_OK = tk_available()
pytestmark_gui = pytest.mark.skipif(not _TK_OK, reason=tk_skip_reason())

#: Header controls: selection, creation and project settings.
PROJECT_CONTROLS = (
    "+ New Project", "Open Existing…", "Settings"
)

#: Activation and closing moved to the Projects tab with the workspace.
PROJECT_TAB_ACTIONS = ("Open project", "Close project")
NO_SELECTION_MESSAGE = "Select a project from the list first"


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _tab_of(widget, root):
    """Notebook tab title containing ``widget``, else 'HEADER'."""
    chain = []
    parent = getattr(widget, "master", None)
    while parent is not None:
        chain.append(parent)
        parent = getattr(parent, "master", None)
    for ancestor in chain:
        if isinstance(ancestor, ttk.Notebook):
            for tab in ancestor.tabs():
                if ancestor.nametowidget(tab) in chain:
                    return ancestor.tab(tab, "text")
    return "HEADER"


@pytest.fixture
def window(monkeypatch, tmp_path):
    """A real MainWindow with all dialogs captured instead of displayed.

    Uses a throwaway on-disk project/chat store seeded with one project, so
    these tests never read or write the developer's real
    ``~/.giscode/projects.db`` and never depend on pre-existing user data.
    """
    dialogs: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        messagebox, "showinfo",
        lambda title, msg, **kw: dialogs.append(("info", title, msg)),
    )
    monkeypatch.setattr(
        messagebox, "showerror",
        lambda title, msg, **kw: dialogs.append(("error", title, msg)),
    )

    project_store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    chat_store = ChatStore(db_path=str(tmp_path / "chats.db"))
    project_store.create_project(
        Project.create("SelProj", str(tmp_path / "SelProj"))
    )

    root = _create_tk_root()
    root.withdraw()
    win = MainWindow(
        root,
        terminal_executor=None,
        project_store=project_store,
        chat_store=chat_store,
    )
    root.update_idletasks()
    win._test_dialogs = dialogs
    try:
        yield win, root
    finally:
        # Same lifecycle the shared tk_root fixture uses: wait for worker
        # threads before destroying so tkinter Variables are finalized on the
        # main thread.
        _safe_destroy(root)
        for store in (project_store, chat_store):
            try:
                store.close()
            except Exception:
                pass


def _buttons(root):
    return [
        w.cget("text")
        for w in _walk(root)
        if isinstance(w, ttk.Button)
    ]


def _button(root, text):
    for w in _walk(root):
        if isinstance(w, ttk.Button):
            try:
                if w.cget("text") == text:
                    return w
            except Exception:
                continue
    raise AssertionError("no %r button found" % text)


# ---------------------------------------------------------------- duplication


def test_exactly_one_of_each_project_control_button(window):
    """Each project action exists once, in the place it belongs.

    The header keeps selection/creation/settings and no longer carries Open or
    Close; those two live in the Projects tab with the workspace, so there is
    still exactly one of each and the header stays uncluttered.
    """
    win, root = window
    for label in PROJECT_CONTROLS:
        matches = [
            w for w in _walk(root)
            if isinstance(w, ttk.Button)
            and _safe_text(w) == label
        ]
        assert len(matches) == 1, "%r exists %d times" % (label, len(matches))
        assert _tab_of(matches[0], root) == "HEADER", (
            "%r is not in the authoritative header area" % label
        )
    for label in PROJECT_TAB_ACTIONS:
        matches = [
            w for w in _walk(root)
            if isinstance(w, ttk.Button)
            and _safe_text(w) == label
        ]
        assert len(matches) == 1, "%r exists %d times" % (label, len(matches))
        assert _tab_of(matches[0], root) == "Projects", (
            "%r is not with the project workspace" % label
        )
    # The removed header controls are gone from the header for good.
    header_labels = [_safe_text(w) for w in _walk(root) if _tab_of(w, root) == "HEADER"]
    assert "Open" not in header_labels
    assert "Close" not in header_labels


def _safe_text(widget):
    try:
        return widget.cget("text")
    except Exception:
        return None


def test_duplicate_projects_tab_picker_is_removed(window):
    """The Projects tab no longer builds its own combobox or button row."""
    win, root = window
    view = win._project_workspace_view
    assert not hasattr(view, "_project_combo"), (
        "ProjectWorkspaceView must not build a second project combobox"
    )
    assert "Start New Project" not in _buttons(root)
    assert "Open" not in [
        _safe_text(w) for w in _walk(root)
        if isinstance(w, ttk.Button) and _tab_of(w, root) == "Projects"
    ]


def test_only_one_project_combobox_remains(window):
    """The authoritative header has only its project/mode/model selectors."""
    win, root = window
    combos = [
        w for w in _walk(root)
        if isinstance(w, ttk.Combobox) and _tab_of(w, root) == "HEADER"
    ]
    # project selector + assistant mode + model selector
    assert len(combos) == 3, "expected 3 comboboxes, found %d" % len(combos)
    widths = sorted(int(c.cget("width")) for c in combos)
    assert widths == [18, 22, 35], widths


def test_projects_tab_no_longer_hosts_a_second_chat_prompt(window):
    """One chat pane: the assistant prompt lives only in the Chat tab.

    The Projects tab used to build a full "Planer Chat" panel (transcript +
    prompt + Send + attach row). Stage 2B removed it outright: what remains is
    the plan-request line the approval workflow needs, not a second chat box.
    """
    win, root = window
    view = win._project_workspace_view

    for gone in ("_planer_chat_panel",
                 "_input_text",
                 "_chat_display",
                 "_standalone_transcript"):
        assert not hasattr(view, gone), (
            "the removed Planer chat panel came back: %s" % gone
        )
    planer_tab = next(
        view._workspace_tabs.nametowidget(tab)
        for tab in view._workspace_tabs.tabs()
        if view._workspace_tabs.tab(tab, "text") == "Planer"
    )
    assert [
        w for w in _walk(planer_tab) if isinstance(w, tk.Text)
    ] == [], "the Planer tab hosts a chat transcript again"
    # The plan request line and its answer are the tab's only text surface.
    assert view._plan_entry.winfo_manager() == "pack"
    assert view._plan_reply_var.get() == ""

    # The Chat tab's prompt is still the visible one.
    assert win.task_entry.winfo_manager() == "pack"
    assert str(win.task_entry.winfo_toplevel()) == str(root)

    # The Projects tab says where chatting happens now.
    hints = [
        w.cget("text")
        for w in _walk(win._tab_frames["Projects"])
        if isinstance(w, ttk.Label) and "Chat runs tasks" in str(w.cget("text"))
    ]
    assert hints, "the Projects tab must point at Chat for running tasks"


# ------------------------------------------------------------------- tabs


def test_navigation_tabs_are_not_duplicated(window):
    """The tabs were never duplicated; this pins that so a future refactor
    does not introduce a second notebook.

    The count is deliberately expressed as "one notebook, no duplicate
    titles, every NAV_TABS entry present" rather than a hard-coded total, so
    that legitimately adding a new tab stays green.
    """
    win, root = window
    # The Project Workspace intentionally owns a nested Planer/Visual Design/
    # Integrity notebook; only the navigation notebook belongs at the header
    # level and must match NAV_TABS.
    notebooks = [
        w for w in _walk(root)
        if isinstance(w, ttk.Notebook) and _tab_of(w, root) == "HEADER"
    ]
    assert len(notebooks) == 1
    titles = [notebooks[0].tab(t, "text") for t in notebooks[0].tabs()]
    assert len(set(titles)) == len(titles), "duplicate tab titles: %s" % titles
    assert set(titles) == set(win._tab_frames.keys())
    # Exactly the running edition's tabs, in registry order: nothing missing and
    # nothing invented.
    assert titles == list(edition.tabs())
    assert set(titles) <= set(edition.ALL_TABS)
    assert set(titles) <= set(NAV_TABS) | {"AdSpace"}

    # The panes that only point at the Chat tab stay registered but hidden, so
    # the visible strip has one entry per real capability (Pro keeps them).
    if edition.is_pro():
        for name in ("Code Task", "Analyze"):
            assert notebooks[0].tab(win._tab_frames[name], "state") == "hidden"


# --------------------------------------------------- selection + open flow


def test_selecting_project_records_the_selection(window):
    win, root = window
    projects = win._project_store.list_projects()
    assert projects, "expected at least one stored project"
    project = projects[0]
    label = "%s [Traditional] (%s)" % (project.name, project.root_path)

    assert win._selected_project_id is None
    win._project_var.set(label)
    win._on_project_selection()

    assert win._selected_project_id == project.id


def test_open_after_selection_does_not_claim_nothing_is_selected(window):
    """The exact reported bug.

    Previously the selection lived in a different control's StringVar, so
    Open read the untouched placeholder and reported that nothing was chosen.
    """
    win, root = window
    project = win._project_store.list_projects()[0]
    label = "%s [Traditional] (%s)" % (project.name, project.root_path)

    win._project_var.set(label)
    win._on_project_selection()
    win._test_dialogs.clear()

    win.open_project_btn.invoke()
    root.update_idletasks()

    assert not any(
        NO_SELECTION_MESSAGE in message
        for _, _, message in win._test_dialogs
    ), win._test_dialogs
    assert win._project_workspace_controller.current_project_id == project.id
    assert win._active_project_context is not None
    assert os.path.normcase(win._active_project_context.root_path) == os.path.normcase(
        project.root_path
    )
    assert os.path.normcase(win.current_path_var.get()) == os.path.normcase(
        project.root_path
    )
    assert "Project opened" in win._status_var.get()


def test_projects_tab_display_matches_the_header_selection(window):
    win, root = window
    project = win._project_store.list_projects()[0]
    label = "%s [Traditional] (%s)" % (project.name, project.root_path)

    win._project_var.set(label)
    win._on_project_selection()
    win.open_project_btn.invoke()
    root.update_idletasks()

    assert project.name in win._project_workspace_view._project_var.get()


def test_open_without_a_selection_still_informs_the_user(window):
    """The genuine no-selection case must keep its helpful message."""
    win, root = window
    win._selected_project_id = None
    win._project_var.set(win.NEW_PROJECT_LABEL)
    win._test_dialogs.clear()

    win.open_project_btn.invoke()

    assert any(
        NO_SELECTION_MESSAGE in message for _, _, message in win._test_dialogs
    ), win._test_dialogs


def test_settings_uses_the_selection_not_only_the_active_project(window, monkeypatch):
    """Settings used to read ``current_project_id`` and so rejected a valid
    selection whenever no project was active."""
    win, root = window
    project = win._project_store.list_projects()[0]
    label = "%s [Traditional] (%s)" % (project.name, project.root_path)

    win._project_var.set(label)
    win._on_project_selection()
    # nothing active, but a project *is* selected
    win._project_workspace_controller.close()
    assert win._project_workspace_controller.current_project_id is None

    seen = {}

    class _Dialog:
        def __init__(self, parent, store, project_id):
            seen["project_id"] = project_id

        def show(self):
            return {"changed": False}

    import giscode.gui.project_workspace as pw

    monkeypatch.setattr(pw, "_ProjectSettingsDialog", _Dialog)
    win._test_dialogs.clear()

    _button(root, "Settings").invoke()

    assert seen.get("project_id") == project.id
    assert not any(
        NO_SELECTION_MESSAGE in m for _, _, m in win._test_dialogs
    )


def test_the_single_project_button_activates_the_selected_project(window):
    """Open is now the only activation control; Switch was the same handler."""
    win, root = window
    project = win._project_store.list_projects()[0]
    label = "%s [Traditional] (%s)" % (project.name, project.root_path)

    win._project_var.set(label)
    win._on_project_selection()
    win._test_dialogs.clear()

    win.open_project_btn.invoke()
    root.update_idletasks()

    assert not any(
        NO_SELECTION_MESSAGE in m for _, _, m in win._test_dialogs
    )
    assert win._project_workspace_controller.current_project_id == project.id


def test_close_clears_selection_and_active_context(window):
    win, root = window
    project = win._project_store.list_projects()[0]
    label = "%s [Traditional] (%s)" % (project.name, project.root_path)

    win._project_var.set(label)
    win._on_project_selection()
    win.open_project_btn.invoke()
    root.update_idletasks()
    assert win._active_project_context is not None

    win.close_project_btn.invoke()
    root.update_idletasks()

    assert win._active_project_context is None
    assert win._selected_project_id is None
    # Closing the project returns the header to the real "no project" state.
    assert win._project_var.get() == win.NO_PROJECT_LABEL


def test_refresh_does_not_discard_a_valid_selection(window):
    """``_refresh_project_list`` used to reset the combobox to the placeholder
    whenever its text was not in the value list, silently dropping the choice."""
    win, root = window
    project = win._project_store.list_projects()[0]
    label = "%s [Traditional] (%s)" % (project.name, project.root_path)

    win._project_var.set(label)
    win._on_project_selection()
    win._refresh_project_list()

    assert win._project_var.get() == label
    assert win._selected_project_id == project.id


def test_new_project_button_invokes_the_create_flow(window, monkeypatch):
    win, root = window
    called = []
    monkeypatch.setattr(
        win._project_workspace_view, "_on_start_new", lambda: called.append(1)
    )

    _button(root, "+ New Project").invoke()

    assert called == [1]


class _ProjectContextRecorder:
    def __init__(self):
        self.contexts = []

    def set_project_context(self, context):
        self.contexts.append(context)


def _project_tree_snapshot(root):
    return {
        path.relative_to(root).as_posix(): (
            "directory" if path.is_dir() else "file",
            path.read_bytes() if path.is_file() else None,
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
    }


def test_open_existing_transferred_project_is_read_only_and_activates_context(
    window, monkeypatch, tmp_path
):
    win, root = window
    transferred = tmp_path / "Transferred Legacy App"
    (transferred / "src").mkdir(parents=True)
    (transferred / "README.md").write_text("Legacy transferred project\n", encoding="utf-8")
    (transferred / "src" / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    # This project has no ProjectStore row or current GisCode project metadata.
    before_tree = _project_tree_snapshot(transferred)
    before_projects = [project.to_dict() for project in win._project_store.list_projects()]
    context_store = ProjectContextStore(tmp_path / "contexts")
    win._ctx_store = context_store
    engine = _ProjectContextRecorder()
    win._gisai_engine = engine
    monkeypatch.setattr(
        "giscode.gui.main_window.filedialog.askdirectory",
        lambda **kwargs: str(transferred),
    )

    _button(root, "Open Existing…").invoke()
    root.update_idletasks()

    assert win._active_project_context is not None
    assert os.path.normcase(win._active_project_context.root_path) == os.path.normcase(
        str(transferred)
    )
    assert win._active_project_context.project_kind == "existing"
    assert win._project_workspace_controller.current_project_root == str(transferred)
    assert win._project_workspace_controller.current_project_id is None
    assert engine.contexts[-1] is win._active_project_context
    assert [project.to_dict() for project in win._project_store.list_projects()] == before_projects
    assert _project_tree_snapshot(transferred) == before_tree
    # The existing integrity operation is available without a locally-created row.
    report = win._project_workspace_controller.inspect_project_integrity()
    assert os.path.normcase(report.project_root) == os.path.normcase(str(transferred))
    assert _project_tree_snapshot(transferred) == before_tree


def test_open_existing_ambiguous_folder_requires_explicit_open_action_and_does_not_create(
    window, monkeypatch, tmp_path
):
    win, root = window
    ambiguous = tmp_path / "Unclassified Folder"
    ambiguous.mkdir()
    (ambiguous / "notes.txt").write_text("not a GisCode marker", encoding="utf-8")
    before_tree = _project_tree_snapshot(ambiguous)
    before_ids = {project.id for project in win._project_store.list_projects()}
    win._ctx_store = ProjectContextStore(tmp_path / "contexts")
    monkeypatch.setattr(
        "giscode.gui.main_window.filedialog.askdirectory",
        lambda **kwargs: str(ambiguous),
    )

    win._on_open_existing_project()

    assert win._active_project_context.project_kind == "existing"
    assert win._active_project_context.root_path == str(ambiguous)
    assert win._project_workspace_controller.current_project_root == str(ambiguous)
    assert {project.id for project in win._project_store.list_projects()} == before_ids
    assert _project_tree_snapshot(ambiguous) == before_tree


def test_switching_existing_roots_keeps_project_context_isolated(
    window, monkeypatch, tmp_path
):
    win, _root = window
    first = tmp_path / "Project A"
    second = tmp_path / "Project B"
    first.mkdir()
    second.mkdir()
    (first / "a.txt").write_text("A", encoding="utf-8")
    (second / "b.txt").write_text("B", encoding="utf-8")
    first_before = _project_tree_snapshot(first)
    second_before = _project_tree_snapshot(second)
    win._ctx_store = ProjectContextStore(tmp_path / "contexts")
    engine = _ProjectContextRecorder()
    win._gisai_engine = engine
    selected = [str(first)]
    monkeypatch.setattr(
        "giscode.gui.main_window.filedialog.askdirectory",
        lambda **kwargs: selected[0],
    )

    win._on_open_existing_project()
    assert win._active_project_context.root_path == str(first)
    selected[0] = str(second)
    win._on_open_existing_project()

    assert win._active_project_context.root_path == str(second)
    assert engine.contexts[-1] is win._active_project_context
    assert engine.contexts[-1].root_path == str(second)
    assert engine.contexts[-1].project_id != engine.contexts[-2].project_id
    assert _project_tree_snapshot(first) == first_before
    assert _project_tree_snapshot(second) == second_before


def test_cancel_open_existing_keeps_current_project_and_files_unchanged(
    window, monkeypatch, tmp_path
):
    win, _root = window
    active = tmp_path / "Active"
    active.mkdir()
    (active / "data.txt").write_text("keep", encoding="utf-8")
    win._ctx_store = ProjectContextStore(tmp_path / "contexts")
    win._select_project_path(str(active))
    active_context = win._active_project_context
    before_tree = _project_tree_snapshot(active)
    monkeypatch.setattr(
        "giscode.gui.main_window.filedialog.askdirectory", lambda **kwargs: ""
    )

    win._on_open_existing_project()

    assert win._active_project_context is active_context
    assert win._project_workspace_controller.current_project_root == str(active)
    assert _project_tree_snapshot(active) == before_tree


def test_new_project_flow_still_creates_a_new_registered_project(
    window, monkeypatch, tmp_path
):
    win, _root = window
    created_root = tmp_path / "New Project Root"
    win._ctx_store = ProjectContextStore(tmp_path / "contexts")
    engine = _ProjectContextRecorder()
    win._gisai_engine = engine

    monkeypatch.setattr(
        win._project_workspace_view,
        "_on_start_new",
        lambda: win._project_workspace_controller.create_project(
            "Newly Created", str(created_root)
        ),
    )

    win._on_new_project()

    created = win._project_store.find_by_name("Newly Created")
    assert created is not None
    assert win._selected_project_id == created.id
    assert win._active_project_context.project_kind == "new"
    assert win._active_project_context.root_path == str(created_root)
    assert engine.contexts[-1] is win._active_project_context
    assert not created_root.exists()


def test_cancel_new_project_flow_does_not_change_selection_or_create_a_record(
    window, monkeypatch
):
    win, _root = window
    before_ids = {project.id for project in win._project_store.list_projects()}
    monkeypatch.setattr(win._project_workspace_view, "_on_start_new", lambda: None)

    win._on_new_project()

    assert {project.id for project in win._project_store.list_projects()} == before_ids
    assert win._active_project_context is None


def test_new_project_dialog_prompts_before_using_ambiguous_nonempty_folder(
    monkeypatch, tmp_path
):
    from giscode.gui import project_workspace

    ambiguous = tmp_path / "Unclassified"
    ambiguous.mkdir()
    (ambiguous / "notes.txt").write_text("existing content", encoding="utf-8")
    monkeypatch.setattr(
        project_workspace, "_default_projects_root", lambda: str(tmp_path / "default")
    )
    choices = []
    monkeypatch.setattr(
        project_workspace.messagebox,
        "askyesno",
        lambda title, message, **kwargs: choices.append((title, message)) or False,
    )

    class _Value:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    class _Top:
        destroyed = False

        def destroy(self):
            self.destroyed = True

    dialog = project_workspace._StartProjectDialog.__new__(
        project_workspace._StartProjectDialog
    )
    dialog._name_var = _Value("New project")
    dialog._path_var = _Value(str(ambiguous))
    dialog._mode_var = _Value("traditional")
    dialog._top = _Top()
    dialog._result = None
    dialog._is_inside_existing_project = lambda _path: False

    dialog._ok()

    assert choices and "cannot identify it" in choices[0][1]
    assert dialog._result is None
    assert not dialog._top.destroyed
    assert (ambiguous / "notes.txt").read_text(encoding="utf-8") == "existing content"


def test_only_one_selection_state_exists(window):
    """There must be a single authoritative selection holder."""
    win, root = window
    project = win._project_store.list_projects()[0]
    label = "%s [Traditional] (%s)" % (project.name, project.root_path)

    win._project_var.set(label)
    win._on_project_selection()

    assert win._selected_project_id == project.id
    assert not hasattr(win._project_workspace_view, "_project_combo"), (
        "the view must not keep a second, independent selection widget"
    )
