"""Tk workspace checks for the native Visual Design canvas."""
from __future__ import annotations

import tkinter as tk
from types import SimpleNamespace

import pytest

from giscode.gui.project_workspace import ProjectWorkspaceController, ProjectWorkspaceView
from giscode.projects import ProjectStore
from giscode.storage import ChatStore
from tests.conftest import tk_available, tk_skip_reason


# ``tk_available`` retries: a single transient Tcl init failure while pytest
# imports modules must not disable the whole module.
_TK_OK = tk_available()

pytestmark = pytest.mark.skipif(
    not _TK_OK, reason=tk_skip_reason()
)


def test_native_visual_design_workspace_and_project_switch(
    tmp_path, monkeypatch, tk_root
):
    project_store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    chat_store = ChatStore(db_path=str(tmp_path / "chats.db"))
    controller = ProjectWorkspaceController(project_store, chat_store)
    root = tk_root
    try:
        project_a = controller.create_project(
            "Canvas A", str(tmp_path / "canvas-a"), mode="visual_app_design"
        )
        a_id = project_a.project_id
        view = ProjectWorkspaceView(root, controller)
        view.pack(fill=tk.BOTH, expand=True)
        root.update_idletasks()

        tabs = [
            view._workspace_tabs.tab(tab_id, "text")
            for tab_id in view._workspace_tabs.tabs()
        ]
        assert tabs == ["Planer", "Visual Design", "Integrity"]

        canvas_view = view._visual_design_view
        welcome = canvas_view.add_screen("Welcome", x=80, y=70)
        assert canvas_view._entry_screen_var.get() == "No entry screen"
        assert canvas_view.document.entry_screen_id is None
        welcome_entry_label = f"Welcome [{welcome.id}]"
        canvas_view._entry_screen_combo.set(welcome_entry_label)
        canvas_view._entry_screen_combo.event_generate("<<ComboboxSelected>>")
        root.update()
        assert canvas_view.document.entry_screen_id == welcome.id
        assert "entry_screen_id" not in welcome.properties
        welcome_label_item = canvas_view._canvas.find_withtag(
            "screen_" + welcome.id + "_label"
        )
        assert canvas_view._canvas.itemcget(welcome_label_item[0], "text") == "▶ Welcome"
        assert canvas_view._entry_screen_combo.get() == welcome_entry_label
        assert canvas_view._description_text.get("1.0", "end-1c") == ""
        welcome_description = "Show a friendly greeting.\nOffer sign-in and guest access."
        canvas_view._description_text.insert("1.0", welcome_description)
        canvas_view._description_save_button.invoke()
        assert welcome.description == welcome_description
        assert welcome.properties["description"] == welcome_description
        assert welcome.links == []
        assert canvas_view._links_listbox.size() == 0

        picker_path = r"C:\reference folder\requirements.pdf"
        dialog = {"result": picker_path}
        dialog_calls = []

        def fake_open_file_dialog(**kwargs):
            dialog_calls.append(kwargs)
            return dialog["result"]

        monkeypatch.setattr(
            "giscode.gui.visual_design.filedialog.askopenfilename",
            fake_open_file_dialog,
        )
        canvas_view._link_label_entry.insert(0, "Selected local file")
        canvas_view._link_url_entry.insert(0, "existing text")
        canvas_view._link_browse_button.invoke()
        assert canvas_view._link_url_entry.get() == picker_path
        assert welcome.links == []
        assert dialog_calls[-1]["parent"] is root

        canvas_view._link_url_entry.delete(0, tk.END)
        canvas_view._link_url_entry.insert(0, "https://manual.test/reference")
        dialog["result"] = ""
        canvas_view._link_browse_button.invoke()
        assert canvas_view._link_url_entry.get() == "https://manual.test/reference"
        dialog["result"] = picker_path

        canvas_view._link_label_entry.delete(0, tk.END)
        canvas_view._link_url_entry.delete(0, tk.END)
        canvas_view._link_label_entry.insert(0, "Login requirements")
        canvas_view._link_url_entry.insert(0, "https://example.test/login")
        canvas_view._link_add_button.invoke()
        canvas_view._link_label_entry.delete(0, tk.END)
        canvas_view._link_url_entry.delete(0, tk.END)
        canvas_view._link_browse_button.invoke()
        assert canvas_view._link_url_entry.get() == picker_path
        canvas_view._link_add_button.invoke()
        assert canvas_view._links_listbox.size() == 2
        canvas_view._link_save_button.invoke()
        assert welcome.links == [
            {"label": "Login requirements", "url": "https://example.test/login"},
            {"label": "", "url": picker_path},
        ]
        stored_links = next(
            item["properties"]["links"]
            for item in controller.get_visual_design()["screens"]
            if item["id"] == welcome.id
        )
        assert stored_links == welcome.links

        canvas_view._links_listbox.selection_set(0)
        canvas_view._links_listbox.event_generate("<<ListboxSelect>>")
        canvas_view._link_label_entry.delete(0, tk.END)
        canvas_view._link_label_entry.insert(0, "Updated requirements")
        canvas_view._link_url_entry.delete(0, tk.END)
        canvas_view._link_url_entry.insert(0, "https://example.test/revised")
        canvas_view._link_update_button.invoke()
        canvas_view._links_listbox.selection_clear(0, tk.END)
        canvas_view._links_listbox.selection_set(1)
        canvas_view._links_listbox.event_generate("<<ListboxSelect>>")
        canvas_view._link_remove_button.invoke()
        canvas_view._link_save_button.invoke()
        assert welcome.links == [
            {
                "label": "Updated requirements",
                "url": "https://example.test/revised",
            }
        ]
        assert canvas_view._screen_type_combo.get() == "Generic"
        canvas_view._screen_type_combo.set("Dashboard")
        canvas_view._screen_type_combo.event_generate("<<ComboboxSelected>>")
        root.update()
        assert welcome.screen_type == "Dashboard"
        assert canvas_view._icon_combo.get() == "No icon"
        canvas_view._icon_combo.set("🏠 Home")
        canvas_view._icon_combo.event_generate("<<ComboboxSelected>>")
        root.update()
        assert welcome.icon == "home"
        icon_item = canvas_view._canvas.find_withtag("screen_" + welcome.id + "_icon")
        assert len(icon_item) == 1
        assert canvas_view._canvas.itemcget(icon_item[0], "text") == "🏠"
        assert canvas_view._shape_combo.get() == "Rounded"
        canvas_view._shape_combo.set("Circle")
        canvas_view._shape_combo.event_generate("<<ComboboxSelected>>")
        root.update()
        assert welcome.shape == "circle"
        shape_item = canvas_view._canvas.find_withtag("screen_" + welcome.id + "_shape")
        assert len(shape_item) == 1
        assert canvas_view._canvas.type(shape_item[0]) == "oval"
        color_choice = {"value": "#E7ECFB"}
        monkeypatch.setattr(
            "giscode.gui.visual_design.colorchooser.askcolor",
            lambda **_kwargs: (None, color_choice["value"]),
        )
        assert canvas_view._color_widgets["fill_color"][0].get() == "#FFFFFF"
        assert canvas_view._color_widgets["border_color"][0].get() == "#DAD8CE"
        canvas_view._choose_color("fill_color")
        color_choice["value"] = "#3D4EBA"
        canvas_view._choose_color("border_color")
        assert welcome.fill_color == "#E7ECFB"
        assert welcome.border_color == "#3D4EBA"
        shape_item = canvas_view._canvas.find_withtag("screen_" + welcome.id + "_shape")
        assert canvas_view._canvas.itemcget(shape_item[0], "fill") == "#E7ECFB"
        assert canvas_view._canvas.itemcget(shape_item[0], "outline") == "#3D4EBA"

        main = canvas_view.add_screen("Main", x=380, y=210)
        assert canvas_view.document.entry_screen_id == welcome.id
        assert canvas_view._description_text.get("1.0", "end-1c") == ""
        canvas_view._description_save_button.invoke()
        assert main.description == ""
        assert main.links == []
        assert canvas_view._links_listbox.size() == 0
        canvas_view._link_save_button.invoke()
        assert canvas_view._screen_type_combo.get() == "Generic"
        assert canvas_view._icon_combo.get() == "No icon"
        assert canvas_view._shape_combo.get() == "Rounded"
        assert canvas_view._color_widgets["fill_color"][0].get() == "#FFFFFF"
        assert canvas_view._color_widgets["border_color"][0].get() == "#DAD8CE"
        canvas_view.select_screen(welcome.id)
        assert canvas_view._description_text.get("1.0", "end-1c") == welcome_description
        assert canvas_view._links_buffer == [
            {
                "label": "Updated requirements",
                "url": "https://example.test/revised",
            }
        ]
        assert canvas_view._screen_type_combo.get() == "Dashboard"
        assert canvas_view._icon_combo.get() == "🏠 Home"
        assert canvas_view._shape_combo.get() == "Circle"
        assert canvas_view._color_widgets["fill_color"][0].get() == "#E7ECFB"
        assert canvas_view._color_widgets["border_color"][0].get() == "#3D4EBA"
        canvas_view._reset_color("fill_color")
        canvas_view._reset_color("border_color")
        assert welcome.fill_color == "#FFFFFF"
        assert welcome.border_color == "#DAD8CE"
        assert "fill_color" not in welcome.properties
        assert "border_color" not in welcome.properties
        shape_item = canvas_view._canvas.find_withtag("screen_" + welcome.id + "_shape")
        assert canvas_view._canvas.itemcget(shape_item[0], "fill") == "#FFFFFF"
        assert canvas_view._canvas.itemcget(shape_item[0], "outline") == "#DAD8CE"
        color_choice["value"] = "#E7ECFB"
        canvas_view._choose_color("fill_color")
        color_choice["value"] = "#3D4EBA"
        canvas_view._choose_color("border_color")
        canvas_view._icon_combo.set("No icon")
        canvas_view._icon_combo.event_generate("<<ComboboxSelected>>")
        root.update()
        assert welcome.icon is None
        assert "icon" not in welcome.properties
        assert not canvas_view._canvas.find_withtag(
            "screen_" + welcome.id + "_icon"
        )
        extra = canvas_view.add_screen("Welcome", x=700, y=300)
        duplicate_labels = canvas_view._entry_screen_combo.cget("values")
        assert f"Welcome [{welcome.id}]" in duplicate_labels
        assert f"Welcome [{extra.id}]" in duplicate_labels
        assert f"Welcome [{welcome.id}]" != f"Welcome [{extra.id}]"
        canvas_view._entry_screen_combo.set(f"Welcome [{extra.id}]")
        canvas_view._entry_screen_combo.event_generate("<<ComboboxSelected>>")
        root.update()
        assert canvas_view.document.entry_screen_id == extra.id
        canvas_view._entry_screen_combo.set(f"Welcome [{welcome.id}]")
        canvas_view._entry_screen_combo.event_generate("<<ComboboxSelected>>")
        root.update()
        assert canvas_view.document.entry_screen_id == welcome.id
        canvas_view._on_screen_click(
            welcome.id, SimpleNamespace(x=90, y=80)
        )
        assert canvas_view.selected_screen_id == welcome.id
        shape_coords_before_drag = canvas_view._canvas.coords(
            "screen_" + welcome.id + "_shape"
        )
        canvas_view._name_var.set("Welcome screen")
        canvas_view._rename_from_entry()
        canvas_view._on_drag_motion(SimpleNamespace(x=140, y=120))
        canvas_view._on_drag_release()
        assert canvas_view._document.entry_screen_id == welcome.id
        assert canvas_view._entry_screen_var.get() == f"Welcome screen [{welcome.id}]"
        welcome_label_item = canvas_view._canvas.find_withtag(
            "screen_" + welcome.id + "_label"
        )
        assert canvas_view._canvas.itemcget(welcome_label_item[0], "text") == \
            "▶ Welcome screen"
        shape_coords_after_drag = canvas_view._canvas.coords(
            "screen_" + welcome.id + "_shape"
        )
        assert shape_coords_after_drag != shape_coords_before_drag
        assert welcome.shape == "circle"
        canvas_view._toggle_connect_mode()
        canvas_view._on_screen_click(welcome.id)
        canvas_view._on_screen_click(main.id)
        canvas_view._toggle_connect_mode()

        lines = canvas_view._canvas.find_withtag("connection")
        assert len(lines) == 1
        assert canvas_view._canvas.itemcget(lines[0], "arrow") == "last"
        canvas_view._select_connection(welcome.id, main.id)
        assert canvas_view._selected_connection == (welcome.id, main.id)
        assert canvas_view._connection_source_var.get() == "Source: Welcome screen"
        assert canvas_view._connection_destination_var.get() == "Destination: Main"
        assert canvas_view._connection_label_entry.get() == ""
        assert canvas_view._connection_condition_entry.get() == ""
        assert len(canvas_view.document.connections) == 1
        canvas_view._connection_label_entry.insert(0, "Tap Continue")
        canvas_view._connection_save_button.invoke()
        canvas_view._connection_condition_entry.insert(
            0, "When authentication succeeds"
        )
        canvas_view._connection_condition_save_button.invoke()
        assert canvas_view.document.connections[0].label == "Tap Continue"
        assert canvas_view.document.connections[0].condition == \
            "When authentication succeeds"

        # Selecting a screen after a connection clears the connection editor.
        canvas_view.select_screen(welcome.id)
        assert canvas_view._selected_connection is None
        assert canvas_view._connection_save_button.instate(["disabled"])
        assert canvas_view._connection_condition_save_button.instate(["disabled"])

        canvas_view._toggle_connect_mode()
        canvas_view._on_screen_click(main.id)
        canvas_view._on_screen_click(extra.id)
        canvas_view._toggle_connect_mode()
        canvas_view._select_connection(main.id, extra.id)
        assert canvas_view._connection_source_var.get() == "Source: Main"
        assert canvas_view._connection_destination_var.get() == "Destination: Welcome"
        canvas_view._connection_label_entry.insert(0, "Open details")
        canvas_view._connection_save_button.invoke()
        canvas_view._connection_condition_entry.insert(0, "When the user continues")
        canvas_view._connection_condition_save_button.invoke()
        assert canvas_view.document.connections[1].label == "Open details"
        assert canvas_view.document.connections[1].condition == "When the user continues"
        canvas_view._connection_condition_entry.delete(0, tk.END)
        canvas_view._connection_condition_entry.insert(0, " \t ")
        canvas_view._connection_condition_save_button.invoke()
        assert canvas_view.document.connections[1].condition is None
        assert "condition" not in canvas_view.document.connections[1].to_dict()
        canvas_view._connection_condition_entry.delete(0, tk.END)
        canvas_view._connection_condition_entry.insert(0, "When the user continues")
        canvas_view._connection_condition_save_button.invoke()
        canvas_view._connection_clear_button.invoke()
        assert canvas_view.document.connections[1].label is None
        assert "label" not in canvas_view.document.connections[1].to_dict()
        assert canvas_view.document.connections[1].condition == "When the user continues"
        canvas_view._connection_condition_clear_button.invoke()
        assert canvas_view.document.connections[1].condition is None
        assert "condition" not in canvas_view.document.connections[1].to_dict()
        canvas_view._connection_remove_button.invoke()
        assert len(canvas_view.document.connections) == 1
        assert canvas_view.document.connections[0].label == "Tap Continue"

        # Line selection is non-destructive; removal is an explicit control.
        canvas_view._select_connection(welcome.id, main.id)
        assert len(canvas_view._canvas.find_withtag("connection")) == 1
        assert canvas_view._connection_remove_button.instate(["!disabled"])
        canvas_view.select_screen(extra.id)
        assert canvas_view.delete_selected() is True
        assert canvas_view.document.get_screen(extra.id) is None
        assert len(canvas_view.document.connections) == 1
        canvas_view._select_connection(welcome.id, main.id)
        canvas_view._connection_remove_button.invoke()
        assert canvas_view.document.connections == []
        assert not canvas_view._canvas.find_withtag("connection")

        canvas_view._toggle_connect_mode()
        canvas_view._on_screen_click(welcome.id)
        canvas_view._on_screen_click(main.id)
        canvas_view._toggle_connect_mode()
        assert len(canvas_view._canvas.find_withtag("connection")) == 1

        # Deleting an endpoint removes its connection as well.
        canvas_view.select_screen(welcome.id)
        assert canvas_view.delete_selected() is True
        assert canvas_view.document.connections == []
        assert canvas_view.document.entry_screen_id is None
        assert canvas_view._entry_screen_var.get() == "No entry screen"

        # Restore the A flow for project-switch checks.
        welcome = canvas_view.add_screen("Welcome screen", x=130, y=110)
        assert canvas_view.document.entry_screen_id is None
        assert canvas_view._entry_screen_var.get() == "No entry screen"
        welcome_description = "Welcome the user.\nShow sign-in and guest access options."
        canvas_view._description_text.insert("1.0", welcome_description)
        canvas_view._description_save_button.invoke()
        canvas_view._link_label_entry.insert(0, "Design notes")
        canvas_view._link_url_entry.insert(0, "C:/project/design-notes.md")
        canvas_view._link_add_button.invoke()
        canvas_view._link_save_button.invoke()
        canvas_view._screen_type_combo.set("Dashboard")
        canvas_view._screen_type_combo.event_generate("<<ComboboxSelected>>")
        canvas_view._entry_screen_combo.set(
            f"Welcome screen [{welcome.id}]"
        )
        canvas_view._entry_screen_combo.event_generate("<<ComboboxSelected>>")
        canvas_view._icon_combo.set("🚀 Launch")
        canvas_view._icon_combo.event_generate("<<ComboboxSelected>>")
        canvas_view._shape_combo.set("Diamond")
        canvas_view._shape_combo.event_generate("<<ComboboxSelected>>")
        color_choice["value"] = "#E7ECFB"
        canvas_view._choose_color("fill_color")
        color_choice["value"] = "#3D4EBA"
        canvas_view._choose_color("border_color")
        root.update()
        canvas_view._toggle_connect_mode()
        canvas_view._on_screen_click(welcome.id)
        canvas_view._on_screen_click(main.id)
        canvas_view._toggle_connect_mode()
        canvas_view._select_connection(welcome.id, main.id)
        canvas_view._connection_label_entry.insert(0, "Tap Sign in")
        canvas_view._connection_save_button.invoke()
        canvas_view._connection_condition_entry.insert(
            0, "When authentication succeeds"
        )
        canvas_view._connection_condition_save_button.invoke()

        saved_a = controller.get_visual_design()
        assert len(saved_a["screens"]) == 2
        assert saved_a["entry_screen_id"] == welcome.id
        assert saved_a["connections"] == [
            {
                "from": welcome.id,
                "to": main.id,
                "label": "Tap Sign in",
                "condition": "When authentication succeeds",
            }
        ]
        welcome_data = next(item for item in saved_a["screens"] if item["id"] == welcome.id)
        assert welcome_data["x"] == 130
        assert welcome_data["properties"]["icon"] == "launch"
        assert welcome_data["properties"]["shape"] == "diamond"
        assert welcome_data["properties"]["fill_color"] == "#E7ECFB"
        assert welcome_data["properties"]["border_color"] == "#3D4EBA"
        assert welcome_data["properties"]["description"] == welcome_description
        assert welcome_data["properties"]["links"] == [
            {"label": "Design notes", "url": "C:/project/design-notes.md"}
        ]

        project_b = controller.create_project(
            "Canvas B", str(tmp_path / "canvas-b"), mode="visual_app_design"
        )
        b_id = project_b.project_id
        view.show_active_project("Canvas B", b_id)
        assert canvas_view.document.screens == []
        assert canvas_view._selected_connection is None
        assert canvas_view._connection_condition_entry.get() == ""
        assert canvas_view._entry_screen_var.get() == "No entry screen"
        b_screen = canvas_view.add_screen("Only B", x=500, y=90)
        assert canvas_view._description_text.get("1.0", "end-1c") == ""
        b_description = "List available projects."
        canvas_view._description_text.insert("1.0", b_description)
        canvas_view._description_save_button.invoke()
        assert canvas_view._links_listbox.size() == 0
        canvas_view._link_label_entry.insert(0, "Project API")
        canvas_view._link_url_entry.insert(0, "https://example.test/project-api")
        canvas_view._link_add_button.invoke()
        canvas_view._link_save_button.invoke()
        assert b_screen.description == b_description
        assert canvas_view._color_widgets["fill_color"][0].get() == "#FFFFFF"
        assert canvas_view._color_widgets["border_color"][0].get() == "#DAD8CE"
        b_details = canvas_view.add_screen("Details B", x=760, y=260)
        canvas_view._entry_screen_combo.set(f"Only B [{b_screen.id}]")
        canvas_view._entry_screen_combo.event_generate("<<ComboboxSelected>>")
        root.update()
        assert canvas_view.document.entry_screen_id == b_screen.id
        canvas_view._toggle_connect_mode()
        canvas_view._on_screen_click(b_screen.id)
        canvas_view._on_screen_click(b_details.id)
        canvas_view._toggle_connect_mode()
        canvas_view._select_connection(b_screen.id, b_details.id)
        canvas_view._connection_label_entry.insert(0, "Open project")
        canvas_view._connection_save_button.invoke()
        canvas_view._connection_condition_entry.insert(
            0, "When the user chooses a project"
        )
        canvas_view._connection_condition_save_button.invoke()

        controller.open_project(a_id)
        view.show_active_project("Canvas A", a_id)
        assert canvas_view.document.entry_screen_id == welcome.id
        assert canvas_view._entry_screen_var.get() == f"Welcome screen [{welcome.id}]"
        assert canvas_view._screen_type_combo.get() == "Generic"
        assert {screen.name for screen in canvas_view.document.screens} == {
            "Welcome screen", "Main"
        }
        assert len(canvas_view.document.connections) == 1
        canvas_view.select_screen(welcome.id)
        assert canvas_view._description_text.get("1.0", "end-1c") == welcome_description
        assert canvas_view._links_buffer == [
            {"label": "Design notes", "url": "C:/project/design-notes.md"}
        ]
        assert canvas_view._screen_type_combo.get() == "Dashboard"
        assert canvas_view._icon_combo.get() == "🚀 Launch"
        assert canvas_view._shape_combo.get() == "Diamond"
        assert canvas_view._color_widgets["fill_color"][0].get() == "#E7ECFB"
        assert canvas_view._color_widgets["border_color"][0].get() == "#3D4EBA"
        shape_item = canvas_view._canvas.find_withtag("screen_" + welcome.id + "_shape")
        assert canvas_view._canvas.type(shape_item[0]) == "polygon"
        canvas_view._select_connection(welcome.id, main.id)
        assert canvas_view._connection_source_var.get() == "Source: Welcome screen"
        assert canvas_view._connection_destination_var.get() == "Destination: Main"
        assert canvas_view._connection_label_entry.get() == "Tap Sign in"
        assert canvas_view._connection_condition_entry.get() == \
            "When authentication succeeds"

        controller.open_project(b_id)
        view.show_active_project("Canvas B", b_id)
        assert canvas_view.document.entry_screen_id == b_screen.id
        assert canvas_view._entry_screen_var.get() == f"Only B [{b_screen.id}]"
        assert {screen.name for screen in canvas_view.document.screens} == {
            "Only B", "Details B"
        }
        assert canvas_view.document.screens[0].id == b_screen.id
        assert canvas_view._selected_connection is None
        assert canvas_view._connection_remove_button.instate(["disabled"])
        assert canvas_view._connection_condition_entry.get() == ""
        assert canvas_view._screen_type_combo.get() == "Generic"
        assert canvas_view._icon_combo.get() == "No icon"
        assert canvas_view._shape_combo.get() == "Rounded"
        canvas_view.select_screen(b_screen.id)
        assert canvas_view._description_text.get("1.0", "end-1c") == b_description
        assert canvas_view._links_buffer == [
            {"label": "Project API", "url": "https://example.test/project-api"}
        ]
        canvas_view._screen_type_combo.set("Search")
        canvas_view._screen_type_combo.event_generate("<<ComboboxSelected>>")
        canvas_view._icon_combo.set("🔍 Search")
        canvas_view._icon_combo.event_generate("<<ComboboxSelected>>")
        canvas_view._shape_combo.set("Sharp")
        canvas_view._shape_combo.event_generate("<<ComboboxSelected>>")
        color_choice["value"] = "#FBEBDD"
        canvas_view._choose_color("fill_color")
        color_choice["value"] = "#2F9E52"
        canvas_view._choose_color("border_color")
        root.update()
        assert canvas_view.document.get_screen(b_screen.id).screen_type == "Search"
        assert canvas_view.document.get_screen(b_screen.id).icon == "search"
        assert canvas_view.document.get_screen(b_screen.id).shape == "sharp"
        assert canvas_view.document.get_screen(b_screen.id).fill_color == "#FBEBDD"
        assert canvas_view.document.get_screen(b_screen.id).border_color == "#2F9E52"
        shape_item = canvas_view._canvas.find_withtag("screen_" + b_screen.id + "_shape")
        assert canvas_view._canvas.type(shape_item[0]) == "rectangle"

        controller.open_project(a_id)
        view.show_active_project("Canvas A", a_id)
        canvas_view.select_screen(welcome.id)
        assert canvas_view._description_text.get("1.0", "end-1c") == welcome_description
        assert canvas_view._links_buffer == [
            {"label": "Design notes", "url": "C:/project/design-notes.md"}
        ]
        assert canvas_view._screen_type_combo.get() == "Dashboard"
        assert canvas_view._icon_combo.get() == "🚀 Launch"
        assert canvas_view._shape_combo.get() == "Diamond"
        assert canvas_view._color_widgets["fill_color"][0].get() == "#E7ECFB"
        assert canvas_view._color_widgets["border_color"][0].get() == "#3D4EBA"
        controller.open_project(b_id)
        view.show_active_project("Canvas B", b_id)
        assert canvas_view._selected_connection is None
        assert canvas_view._connection_condition_entry.get() == ""
        canvas_view.select_screen(b_screen.id)
        assert canvas_view._description_text.get("1.0", "end-1c") == b_description
        assert canvas_view._links_buffer == [
            {"label": "Project API", "url": "https://example.test/project-api"}
        ]
        assert canvas_view._screen_type_combo.get() == "Search"
        assert canvas_view._icon_combo.get() == "🔍 Search"
        assert canvas_view._shape_combo.get() == "Sharp"
        assert canvas_view._color_widgets["fill_color"][0].get() == "#FBEBDD"
        assert canvas_view._color_widgets["border_color"][0].get() == "#2F9E52"
        canvas_view._select_connection(b_screen.id, b_details.id)
        assert canvas_view._connection_source_var.get() == "Source: Only B"
        assert canvas_view._connection_destination_var.get() == "Destination: Details B"
        assert canvas_view._connection_label_entry.get() == "Open project"
        assert canvas_view._connection_condition_entry.get() == \
            "When the user chooses a project"

        legacy = controller.create_project(
            "Legacy colors",
            str(tmp_path / "legacy-colors"),
            mode="visual_app_design",
            plan_data={
                "visual_design": {
                    "screens": [
                        {
                            "id": "legacy-color-screen",
                            "name": "Legacy colors",
                            "x": 40,
                            "y": 50,
                            "width": 180,
                            "height": 88,
                            "properties": {
                                "fill_color": "invalid-fill",
                                "border_color": "#12345",
                                "other": "preserve",
                                "links": [
                                    {
                                        "label": "Valid reference",
                                        "url": "https://example.test/reference",
                                    },
                                    {"label": "Malformed reference"},
                                    12,
                                ],
                            },
                        }
                    ],
                    "connections": [],
                }
            },
        )
        view.show_active_project("Legacy colors", legacy.project_id)
        legacy_screen = canvas_view.document.get_screen("legacy-color-screen")
        canvas_view.select_screen(legacy_screen.id)
        assert canvas_view._description_text.get("1.0", "end-1c") == ""
        assert canvas_view._links_buffer == [
            {
                "label": "Valid reference",
                "url": "https://example.test/reference",
            }
        ]
        assert canvas_view._links_listbox.size() == 1
        assert canvas_view._color_widgets["fill_color"][0].get() == "#FFFFFF"
        assert canvas_view._color_widgets["border_color"][0].get() == "#DAD8CE"
        assert legacy_screen.properties == {
            "fill_color": "invalid-fill",
            "border_color": "#12345",
            "other": "preserve",
            "links": [
                {
                    "label": "Valid reference",
                    "url": "https://example.test/reference",
                },
                {"label": "Malformed reference"},
                12,
            ],
        }
        assert "description" not in legacy_screen.properties
        rendered_shape = canvas_view._canvas.find_withtag(
            "screen_legacy-color-screen_shape"
        )
        assert canvas_view._canvas.itemcget(rendered_shape[0], "fill") == "#FFFFFF"
        assert canvas_view._canvas.itemcget(rendered_shape[0], "outline") == "#DAD8CE"
    finally:
        controller.close()
        project_store.close()
        chat_store.close()
