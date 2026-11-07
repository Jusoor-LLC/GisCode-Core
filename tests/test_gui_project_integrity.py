"""GUI integration for explicit read-only project integrity inspection."""
from __future__ import annotations

import json

import pytest

from giscode.gui.project_workspace import ProjectWorkspaceController, ProjectWorkspaceView
from giscode.projects import ProjectStore
from giscode.storage import ChatStore
from tests.conftest import GISAI_ABSENT_REASON, gisai_available


def test_integrity_tab_reports_only_the_active_project_without_writing(
    tk_root, tmp_path
):
    project_store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    chat_store = ChatStore(db_path=str(tmp_path / "chats.db"))
    controller = ProjectWorkspaceController(project_store, chat_store)
    try:
        root_a = tmp_path / "project-a"
        source_a = root_a / "src"
        source_a.mkdir(parents=True)
        manifest_a = root_a / "package.json"
        source_file_a = source_a / "main.py"
        manifest_a.write_text(
            json.dumps({"name": "a", "main": "src/main.py"}), encoding="utf-8"
        )
        source_file_a.write_text("def main():\n    return 0\n", encoding="utf-8")
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (manifest_a, source_file_a)
        }

        project_a = controller.create_project(
            "Transferred A", str(root_a), mode="visual_app_design"
        )
        view = ProjectWorkspaceView(tk_root, controller)
        view.pack(fill="both", expand=True)
        tk_root.update_idletasks()
        tabs = [
            view._workspace_tabs.tab(tab_id, "text")
            for tab_id in view._workspace_tabs.tabs()
        ]
        assert tabs == ["Planer", "Visual Design", "Integrity"]

        integrity_view = view._integrity_view
        integrity_tab_id = view._workspace_tabs.tabs()[2]
        assert integrity_view.winfo_manager() == "pack"
        assert len(integrity_view.winfo_children()) == 2
        view._workspace_tabs.select(integrity_tab_id)
        tk_root.update()
        assert integrity_view._inspect_button.instate(["!disabled"])
        integrity_view._inspect_button.invoke()
        report_a = integrity_view._report_text.get("1.0", "end-1c")
        assert "NO ISSUES DETECTED" in report_a
        assert str(root_a.resolve()) in report_a
        assert "Files examined: 2" in report_a
        assert "Git metadata: NOT_A_REPOSITORY" in report_a
        assert "No integrity problems were detected in the checked files." in report_a
        assert {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (manifest_a, source_file_a)
        } == before

        view._workspace_tabs.select(view._workspace_tabs.tabs()[0])
        tk_root.update()
        view._workspace_tabs.select(integrity_tab_id)
        tk_root.update()
        assert integrity_view._report_text.get("1.0", "end-1c") == report_a

        root_b = tmp_path / "project-b"
        root_b.mkdir()
        manifest_b = root_b / "package.json"
        manifest_b.write_text(
            json.dumps({"name": "b", "main": "src/missing.py"}), encoding="utf-8"
        )
        project_b = controller.create_project(
            "Transferred B", str(root_b), mode="visual_app_design"
        )
        view.show_active_project("Transferred B", controller.current_project_id)
        assert integrity_view._report_text.get("1.0", "end-1c") == ""
        assert integrity_view._status_var.get() == "Ready to inspect active project"
        integrity_view._inspect_button.invoke()
        report_b = integrity_view._report_text.get("1.0", "end-1c")
        assert "BROKEN_REFERENCE" in report_b
        assert "src/missing.py" in report_b
        assert "src/main.py" not in report_b

        controller.open_project(project_a.project_id)
        view.show_active_project("Transferred A", project_a.project_id)
        assert integrity_view._report_text.get("1.0", "end-1c") == ""
        integrity_view._inspect_button.invoke()
        assert "NO ISSUES DETECTED" in integrity_view._report_text.get(
            "1.0", "end-1c"
        )
        assert project_b.project_id != project_a.project_id
    finally:
        controller.close()
        project_store.close()
        chat_store.close()


@pytest.mark.parametrize("failure_site", ["inspection", "rendering"])
def test_integrity_failure_is_rendered_as_a_visible_error(
    tk_root, tmp_path, monkeypatch, failure_site
):
    project_store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    chat_store = ChatStore(db_path=str(tmp_path / "chats.db"))
    controller = ProjectWorkspaceController(project_store, chat_store)
    project_root = tmp_path / "legacy"
    project_root.mkdir()
    controller.select_existing_project_root(str(project_root))
    view = ProjectWorkspaceView(tk_root, controller)
    view.pack(fill="both", expand=True)
    tk_root.update_idletasks()
    view._workspace_tabs.select(view._workspace_tabs.tabs()[2])
    tk_root.update()
    if failure_site == "inspection":
        monkeypatch.setattr(
            controller,
            "inspect_project_integrity",
            lambda: (_ for _ in ()).throw(OSError("test inspection failure")),
        )
    else:
        if not gisai_available():
            pytest.skip(GISAI_ABSENT_REASON)
        import giscode.gisai.inspection.integrity as integrity_module

        monkeypatch.setattr(
            integrity_module,
            "format_integrity_report",
            lambda _report: (_ for _ in ()).throw(ValueError("test rendering failure")),
        )

    view._integrity_view._inspect_button.invoke()
    tk_root.update()

    assert "Inspection failed" in view._integrity_view._status_var.get()
    error_text = view._integrity_view._report_text.get("1.0", "end-1c")
    assert "Inspection could not be completed" in error_text
    assert f"test {failure_site} failure" in error_text
    assert view._integrity_view.winfo_manager() == "pack"
    controller.close()
    project_store.close()
    chat_store.close()
