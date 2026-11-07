"""Native Tk workspace for a project's Visual App Design foundation."""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import colorchooser, filedialog, ttk
from typing import Any, Optional

from giscode.projects.visual_design import (
    DEFAULT_SHAPE,
    DEFAULT_SCREEN_TYPE,
    DEFAULT_BORDER_COLOR,
    DEFAULT_FILL_COLOR,
    ICON_OPTIONS,
    SHAPE_OPTIONS,
    SCREEN_TYPES,
    VisualDesignDocument,
)


class VisualDesignWorkspace(ttk.Frame):
    """Canvas UI hosted inside the existing Project Workspace view."""

    CANVAS_WIDTH = 2400
    CANVAS_HEIGHT = 1600

    def __init__(self, parent: tk.Misc, controller) -> None:
        super().__init__(parent)
        self._controller = controller
        self._document = VisualDesignDocument.empty()
        self._project_id: Optional[str] = None
        self._selected_id: Optional[str] = None
        self._pending_connection_id: Optional[str] = None
        self._selected_connection: Optional[tuple[str, str]] = None
        self._connect_mode = False
        self._drag_id: Optional[str] = None
        self._drag_offset = (0.0, 0.0)

        self._build_ui()
        self.refresh()

    @property
    def document(self) -> VisualDesignDocument:
        return self._document

    @property
    def selected_screen_id(self) -> Optional[str]:
        return self._selected_id

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=(8, 8, 8, 4))
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="+ Add screen", command=self.add_screen).pack(
            side=tk.LEFT
        )
        self._connect_button = ttk.Button(
            toolbar, text="Connect screens", command=self._toggle_connect_mode
        )
        self._connect_button.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="Delete screen", command=self.delete_selected).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Label(toolbar, text="Screen name:").pack(side=tk.LEFT, padx=(14, 4))
        self._name_var = tk.StringVar(value="")
        self._name_entry = ttk.Entry(toolbar, textvariable=self._name_var, width=24)
        self._name_entry.pack(side=tk.LEFT)
        self._name_entry.bind("<Return>", self._rename_from_entry)
        ttk.Button(toolbar, text="Rename", command=self._rename_from_entry).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        entry_screen_bar = ttk.Frame(self, padding=(8, 0, 8, 4))
        entry_screen_bar.pack(fill=tk.X)
        ttk.Label(entry_screen_bar, text="Entry screen:").pack(side=tk.LEFT)
        self._entry_none_label = "No entry screen"
        self._entry_screen_var = tk.StringVar(value=self._entry_none_label)
        self._entry_screen_combo = ttk.Combobox(
            entry_screen_bar,
            textvariable=self._entry_screen_var,
            values=(self._entry_none_label,),
            state="disabled",
            width=48,
        )
        self._entry_screen_combo.pack(side=tk.LEFT, padx=(6, 0))
        self._entry_screen_combo.bind(
            "<<ComboboxSelected>>", self._on_entry_screen_selected
        )
        self._entry_screen_label_to_id = {self._entry_none_label: None}
        self._entry_screen_id_to_label: dict[str, str] = {}

        ttk.Label(toolbar, text="Screen type:").pack(side=tk.LEFT, padx=(14, 4))
        self._screen_type_var = tk.StringVar(value=DEFAULT_SCREEN_TYPE)
        self._screen_type_combo = ttk.Combobox(
            toolbar,
            textvariable=self._screen_type_var,
            values=SCREEN_TYPES,
            state="disabled",
            width=18,
        )
        self._screen_type_combo.pack(side=tk.LEFT)
        self._screen_type_combo.bind(
            "<<ComboboxSelected>>", self._on_screen_type_selected
        )
        ttk.Label(toolbar, text="Icon:").pack(side=tk.LEFT, padx=(14, 4))
        self._icon_id_to_label = dict(ICON_OPTIONS)
        self._icon_label_to_id = {
            label: icon_id for icon_id, label in ICON_OPTIONS
        }
        self._icon_var = tk.StringVar(value=self._icon_id_to_label[""])
        self._icon_combo = ttk.Combobox(
            toolbar,
            textvariable=self._icon_var,
            values=[label for _icon_id, label in ICON_OPTIONS],
            state="disabled",
            width=18,
        )
        self._icon_combo.pack(side=tk.LEFT)
        self._icon_combo.bind("<<ComboboxSelected>>", self._on_icon_selected)
        ttk.Label(toolbar, text="Shape:").pack(side=tk.LEFT, padx=(14, 4))
        self._shape_id_to_label = dict(SHAPE_OPTIONS)
        self._shape_label_to_id = {
            label: shape_id for shape_id, label in SHAPE_OPTIONS
        }
        self._shape_var = tk.StringVar(value=self._shape_id_to_label[DEFAULT_SHAPE])
        self._shape_combo = ttk.Combobox(
            toolbar,
            textvariable=self._shape_var,
            values=[label for _shape_id, label in SHAPE_OPTIONS],
            state="disabled",
            width=12,
        )
        self._shape_combo.pack(side=tk.LEFT)
        self._shape_combo.bind("<<ComboboxSelected>>", self._on_shape_selected)

        color_bar = ttk.Frame(self, padding=(8, 0, 8, 4))
        color_bar.pack(fill=tk.X)
        self._color_widgets = {}
        self._build_color_control(
            color_bar, "Fill:", "fill_color", DEFAULT_FILL_COLOR
        )
        self._build_color_control(
            color_bar, "Border:", "border_color", DEFAULT_BORDER_COLOR
        )

        description_frame = ttk.LabelFrame(
            self, text="Screen description", padding=(8, 4)
        )
        description_frame.pack(fill=tk.X, padx=8, pady=(0, 4))
        self._description_text = tk.Text(
            description_frame, height=3, wrap=tk.WORD
        )
        self._description_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._description_save_button = ttk.Button(
            description_frame,
            text="Save description",
            command=self._save_description,
            state="disabled",
        )
        self._description_save_button.pack(side=tk.LEFT, padx=(6, 0), anchor=tk.N)

        links_frame = ttk.LabelFrame(self, text="Links / documents", padding=(8, 4))
        links_frame.pack(fill=tk.X, padx=8, pady=(0, 4))
        links_body = ttk.Frame(links_frame)
        links_body.pack(fill=tk.X, expand=True)
        links_list_frame = ttk.Frame(links_body)
        links_list_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._links_listbox = tk.Listbox(
            links_list_frame, height=3, exportselection=False
        )
        links_scrollbar = ttk.Scrollbar(
            links_list_frame, orient=tk.VERTICAL, command=self._links_listbox.yview
        )
        self._links_listbox.configure(yscrollcommand=links_scrollbar.set)
        self._links_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        links_scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self._links_listbox.bind("<<ListboxSelect>>", self._on_link_selected)

        link_editor = ttk.Frame(links_body, padding=(8, 0, 0, 0))
        link_editor.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(link_editor, text="Label:").grid(row=0, column=0, sticky=tk.W)
        self._link_label_entry = ttk.Entry(link_editor, width=24)
        self._link_label_entry.grid(row=0, column=1, sticky=tk.EW, pady=(0, 3))
        ttk.Label(link_editor, text="URL / path:").grid(row=1, column=0, sticky=tk.W)
        self._link_url_entry = ttk.Entry(link_editor, width=24)
        self._link_url_entry.grid(row=1, column=1, sticky=tk.EW)
        self._link_browse_button = ttk.Button(
            link_editor, text="Browse…", command=self._browse_link_path
        )
        self._link_browse_button.grid(row=1, column=2, padx=(4, 0))
        link_actions = ttk.Frame(link_editor)
        link_actions.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(4, 0))
        self._link_add_button = ttk.Button(
            link_actions, text="Add", command=self._add_link
        )
        self._link_add_button.pack(side=tk.LEFT)
        self._link_update_button = ttk.Button(
            link_actions, text="Update", command=self._update_link, state="disabled"
        )
        self._link_update_button.pack(side=tk.LEFT, padx=(4, 0))
        self._link_remove_button = ttk.Button(
            link_actions, text="Remove", command=self._remove_link, state="disabled"
        )
        self._link_remove_button.pack(side=tk.LEFT, padx=(4, 0))
        self._link_save_button = ttk.Button(
            link_actions, text="Save links", command=self._save_links, state="disabled"
        )
        self._link_save_button.pack(side=tk.LEFT, padx=(4, 0))
        link_editor.columnconfigure(1, weight=1)
        self._links_buffer: list[dict[str, Any]] = []

        connection_frame = ttk.LabelFrame(
            self, text="Connection action", padding=(8, 4)
        )
        connection_frame.pack(fill=tk.X, padx=8, pady=(0, 4))
        self._connection_source_var = tk.StringVar(value="Source: —")
        self._connection_destination_var = tk.StringVar(value="Destination: —")
        ttk.Label(
            connection_frame, textvariable=self._connection_source_var
        ).pack(anchor=tk.W)
        ttk.Label(
            connection_frame, textvariable=self._connection_destination_var
        ).pack(anchor=tk.W)
        connection_action_row = ttk.Frame(connection_frame)
        connection_action_row.pack(fill=tk.X, pady=(3, 0))
        ttk.Label(connection_action_row, text="Action label:").pack(side=tk.LEFT)
        self._connection_label_entry = ttk.Entry(connection_action_row)
        self._connection_label_entry.pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 6)
        )
        condition_row = ttk.Frame(connection_frame)
        condition_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(condition_row, text="Condition / transition note:").pack(
            side=tk.LEFT
        )
        self._connection_condition_entry = ttk.Entry(condition_row)
        self._connection_condition_entry.pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 6)
        )
        ttk.Label(
            connection_frame,
            text="Descriptive only — not executed.",
            foreground="gray",
        ).pack(anchor=tk.W, pady=(2, 0))
        connection_actions = ttk.Frame(connection_frame)
        connection_actions.pack(anchor=tk.W, pady=(4, 0))
        self._connection_save_button = ttk.Button(
            connection_actions,
            text="Save label",
            command=self._save_connection_label,
            state="disabled",
        )
        self._connection_save_button.pack(side=tk.LEFT)
        self._connection_clear_button = ttk.Button(
            connection_actions,
            text="Clear label",
            command=self._clear_connection_label,
            state="disabled",
        )
        self._connection_clear_button.pack(side=tk.LEFT, padx=(4, 0))
        self._connection_condition_save_button = ttk.Button(
            connection_actions,
            text="Save condition",
            command=self._save_connection_condition,
            state="disabled",
        )
        self._connection_condition_save_button.pack(side=tk.LEFT, padx=(12, 0))
        self._connection_condition_clear_button = ttk.Button(
            connection_actions,
            text="Clear condition",
            command=self._clear_connection_condition,
            state="disabled",
        )
        self._connection_condition_clear_button.pack(side=tk.LEFT, padx=(4, 0))
        self._connection_remove_button = ttk.Button(
            connection_actions,
            text="Remove connection",
            command=self._remove_selected_connection,
            state="disabled",
        )
        self._connection_remove_button.pack(side=tk.LEFT, padx=(4, 0))
        self._connection_label_entry.configure(state=tk.DISABLED)
        self._connection_condition_entry.configure(state=tk.DISABLED)

        self._hint_var = tk.StringVar(
            value="Select a screen, drag to move it, or use Connect screens and click two screens."
        )
        ttk.Label(self, textvariable=self._hint_var, padding=(8, 2)).pack(
            anchor=tk.W, fill=tk.X
        )

        canvas_frame = ttk.Frame(self, padding=(8, 4, 8, 8))
        canvas_frame.pack(fill=tk.BOTH, expand=True)
        self._canvas = tk.Canvas(
            canvas_frame,
            background="#f6f5f1",
            highlightthickness=1,
            highlightbackground="#d9d7ce",
            scrollregion=(0, 0, self.CANVAS_WIDTH, self.CANVAS_HEIGHT),
        )
        x_scroll = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL, command=self._canvas.xview)
        y_scroll = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(xscrollcommand=x_scroll.set, yscrollcommand=y_scroll.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)
        self._canvas.bind("<B1-Motion>", self._on_drag_motion)
        self._canvas.bind("<ButtonRelease-1>", self._on_drag_release)
        self._canvas.bind("<Button-1>", self._on_canvas_click, add="+")

    def refresh(self) -> None:
        """Reload state from the active project after open/switch/close."""
        project_id = getattr(self._controller, "current_project_id", None)
        self._project_id = project_id
        self._selected_id = None
        self._pending_connection_id = None
        self._selected_connection = None
        self._connect_mode = False
        self._connect_button.state(["!pressed"])
        self._screen_type_var.set(DEFAULT_SCREEN_TYPE)
        self._screen_type_combo.configure(state="disabled")
        self._icon_var.set(self._icon_id_to_label[""])
        self._icon_combo.configure(state="disabled")
        self._shape_var.set(self._shape_id_to_label[DEFAULT_SHAPE])
        self._shape_combo.configure(state="disabled")
        self._update_color_controls(None)
        self._set_description_editor(None)
        self._set_links_editor(None)
        self._set_connection_editor(None)
        if not project_id:
            self._document = VisualDesignDocument.empty()
            self._name_var.set("")
            self._hint_var.set("Open a project to edit its Visual Design.")
        else:
            try:
                getter = getattr(self._controller, "get_visual_design", None)
                data = getter() if callable(getter) else {}
                self._document = VisualDesignDocument.from_dict(data)
                self._hint_var.set(
                    "Select a screen, drag to move it, or use Connect screens and click two screens."
                )
            except Exception as exc:
                self._document = VisualDesignDocument.empty()
                self._hint_var.set(f"Could not load Visual Design: {exc}")
        self._refresh_entry_screen_selector()
        self._render()

    def add_screen(
        self,
        name: str = "New screen",
        *,
        x: Optional[float] = None,
        y: Optional[float] = None,
    ):
        screen = self._document.create_screen(name, x=x, y=y)
        self.select_screen(screen.id)
        self._persist()
        return screen

    def select_screen(self, screen_id: Optional[str]) -> None:
        if screen_id is not None and self._document.get_screen(screen_id) is None:
            raise ValueError(f"unknown screen id: {screen_id!r}")
        self._selected_id = screen_id
        self._selected_connection = None
        self._refresh_entry_screen_selector()
        screen = self._document.get_screen(screen_id) if screen_id else None
        self._name_var.set(screen.name if screen else "")
        self._screen_type_var.set(
            screen.screen_type if screen else DEFAULT_SCREEN_TYPE
        )
        self._screen_type_combo.configure(
            state="readonly" if screen else "disabled"
        )
        icon_id = screen.icon if screen else None
        self._icon_var.set(
            self._icon_id_to_label.get(icon_id or "", self._icon_id_to_label[""])
        )
        self._icon_combo.configure(state="readonly" if screen else "disabled")
        self._shape_var.set(
            self._shape_id_to_label.get(
                screen.shape if screen else DEFAULT_SHAPE,
                self._shape_id_to_label[DEFAULT_SHAPE],
            )
        )
        self._shape_combo.configure(state="readonly" if screen else "disabled")
        self._update_color_controls(screen)
        self._set_description_editor(screen)
        self._set_links_editor(screen)
        self._set_connection_editor(None)
        self._render()

    def _refresh_entry_screen_selector(self) -> None:
        labels = [
            f"{screen.name} [{screen.id}]"
            for screen in self._document.screens
        ]
        self._entry_screen_label_to_id = {self._entry_none_label: None}
        self._entry_screen_id_to_label = {}
        for screen, label in zip(self._document.screens, labels):
            self._entry_screen_label_to_id[label] = screen.id
            self._entry_screen_id_to_label[screen.id] = label

        self._entry_screen_combo.configure(
            values=(self._entry_none_label, *labels),
            state="readonly" if self._project_id else "disabled",
        )
        self._entry_screen_var.set(
            self._entry_screen_id_to_label.get(
                self._document.entry_screen_id, self._entry_none_label
            )
        )

    def _on_entry_screen_selected(self, _event=None):
        display = self._entry_screen_var.get()
        if display not in self._entry_screen_label_to_id:
            self._refresh_entry_screen_selector()
            return "break"
        try:
            self._document.set_entry_screen_id(
                self._entry_screen_label_to_id[display]
            )
        except ValueError as exc:
            self._hint_var.set(str(exc))
            self._refresh_entry_screen_selector()
        else:
            self._refresh_entry_screen_selector()
            self._render()
            self._persist()
        return "break"

    def rename_selected(self, name: str) -> bool:
        if not self._selected_id:
            self._hint_var.set("Select a screen before renaming it.")
            return False
        self._document.rename_screen(self._selected_id, name)
        self._name_var.set(self._document.get_screen(self._selected_id).name)
        self._refresh_entry_screen_selector()
        self._render()
        self._persist()
        return True

    def delete_selected(self) -> bool:
        if not self._selected_id:
            self._hint_var.set("Select a screen before deleting it.")
            return False
        deleted_id = self._selected_id
        self._document.delete_screen(deleted_id)
        self._selected_id = None
        self._pending_connection_id = None
        self._name_var.set("")
        self._refresh_entry_screen_selector()
        self._set_description_editor(None)
        self._set_links_editor(None)
        self._render()
        self._persist()
        return True

    def move_screen(self, screen_id: str, x: float, y: float) -> None:
        self._document.move_screen(screen_id, x, y)
        self._render()
        self._persist()

    def connect_screens(self, source_id: str, destination_id: str) -> None:
        self._document.connect(source_id, destination_id)
        self._pending_connection_id = None
        self._selected_connection = None
        self._set_connection_editor(None)
        self._render()
        self._persist()

    def _persist(self) -> bool:
        if not self._project_id:
            self._hint_var.set("Open a project before saving Visual Design.")
            return False
        try:
            saver = getattr(self._controller, "save_visual_design", None)
            if not callable(saver):
                self._hint_var.set("The active project cannot save Visual Design.")
                return False
            saver(self._document.to_dict())
            self._hint_var.set("Visual Design saved to the active project.")
            return True
        except Exception as exc:
            self._hint_var.set(f"Could not save Visual Design: {exc}")
            return False

    def _toggle_connect_mode(self) -> None:
        self._connect_mode = not self._connect_mode
        self._pending_connection_id = None
        self._selected_connection = None
        self._set_connection_editor(None)
        self._connect_button.state(["pressed"] if self._connect_mode else ["!pressed"])
        self._hint_var.set(
            "Click two different screens to connect them. Click Connect screens to cancel."
            if self._connect_mode
            else "Select a screen, drag to move it, or use Connect screens and click two screens."
        )

    def _on_screen_click(self, screen_id: str, event=None):
        if self._connect_mode:
            if self._pending_connection_id is None:
                self._pending_connection_id = screen_id
                self._selected_id = screen_id
                self._hint_var.set("Now click the destination screen.")
                self._render()
            elif self._pending_connection_id == screen_id:
                self._pending_connection_id = None
                self._hint_var.set("Connection cancelled. Click a destination screen.")
                self._render()
            else:
                source_id = self._pending_connection_id
                try:
                    self.connect_screens(source_id, screen_id)
                    self._hint_var.set("Directed connection created.")
                except ValueError as exc:
                    self._hint_var.set(str(exc))
            return "break"

        self.select_screen(screen_id)
        screen = self._document.get_screen(screen_id)
        point_x = self._canvas.canvasx(event.x) if event is not None else screen.x
        point_y = self._canvas.canvasy(event.y) if event is not None else screen.y
        self._drag_id = screen_id
        self._drag_offset = (point_x - screen.x, point_y - screen.y)
        return "break"

    def _on_drag_motion(self, event) -> None:
        if self._connect_mode or not self._drag_id:
            return
        x = self._canvas.canvasx(event.x) - self._drag_offset[0]
        y = self._canvas.canvasy(event.y) - self._drag_offset[1]
        screen = self._document.move_screen(self._drag_id, x, y)
        tag = self._screen_tag(screen.id)
        self._canvas.coords(
            f"{tag}_box", screen.x, screen.y,
            screen.x + screen.width, screen.y + screen.height,
        )
        self._canvas.coords(
            f"{tag}_label", screen.x + screen.width / 2,
            self._screen_label_y(screen),
        )
        self._canvas.coords(f"{tag}_shape", *self._shape_coordinates(screen))
        if screen.icon_glyph:
            self._canvas.coords(
                f"{tag}_icon", screen.x + screen.width / 2, screen.y + 22
            )
        self._draw_connections()

    def _on_drag_release(self, _event=None) -> None:
        if self._drag_id and not self._connect_mode:
            self._persist()
        self._drag_id = None

    def _on_canvas_click(self, event) -> None:
        current = self._canvas.find_withtag("current")
        if current:
            return
        self.select_screen(None)
        self._pending_connection_id = None

    def _rename_from_entry(self, _event=None):
        self.rename_selected(self._name_var.get())
        return "break"

    def _on_screen_type_selected(self, _event=None):
        if not self._selected_id:
            self._screen_type_var.set(DEFAULT_SCREEN_TYPE)
            return "break"
        try:
            screen = self._document.set_screen_type(
                self._selected_id, self._screen_type_var.get()
            )
        except ValueError as exc:
            selected = self._document.get_screen(self._selected_id)
            self._screen_type_var.set(selected.screen_type)
            self._hint_var.set(str(exc))
        else:
            self._screen_type_var.set(screen.screen_type)
            self._persist()
        return "break"

    def _on_icon_selected(self, _event=None):
        if not self._selected_id:
            self._icon_var.set(self._icon_id_to_label[""])
            return "break"
        icon_id = self._icon_label_to_id.get(self._icon_var.get())
        try:
            self._document.set_icon(self._selected_id, icon_id)
        except ValueError as exc:
            selected = self._document.get_screen(self._selected_id)
            self._icon_var.set(
                self._icon_id_to_label.get(selected.icon or "", self._icon_id_to_label[""])
            )
            self._hint_var.set(str(exc))
        else:
            self._render()
            self._persist()
        return "break"

    def _on_shape_selected(self, _event=None):
        if not self._selected_id:
            self._shape_var.set(self._shape_id_to_label[DEFAULT_SHAPE])
            return "break"
        shape_id = self._shape_label_to_id.get(self._shape_var.get())
        try:
            screen = self._document.set_shape(self._selected_id, shape_id)
        except ValueError as exc:
            selected = self._document.get_screen(self._selected_id)
            self._shape_var.set(self._shape_id_to_label[selected.shape])
            self._hint_var.set(str(exc))
        else:
            self._shape_var.set(self._shape_id_to_label[screen.shape])
            self._render()
            self._persist()
        return "break"

    def _build_color_control(
        self, parent: tk.Misc, label: str, property_name: str, default_color: str
    ) -> None:
        group = ttk.Frame(parent)
        group.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(group, text=label).pack(side=tk.LEFT, padx=(0, 4))
        color_var = tk.StringVar(value=default_color)
        preview = tk.Label(
            group,
            textvariable=color_var,
            width=9,
            relief=tk.SUNKEN,
            borderwidth=1,
            padx=2,
        )
        preview.pack(side=tk.LEFT, padx=(0, 4))
        choose_button = ttk.Button(
            group,
            text="Choose…",
            command=lambda key=property_name: self._choose_color(key),
        )
        choose_button.pack(side=tk.LEFT, padx=(0, 3))
        reset_button = ttk.Button(
            group,
            text="Reset",
            command=lambda key=property_name: self._reset_color(key),
        )
        reset_button.pack(side=tk.LEFT)
        self._color_widgets[property_name] = (
            color_var,
            preview,
            choose_button,
            reset_button,
            default_color,
        )
        self._set_color_preview(property_name, default_color)

    def _set_color_preview(self, property_name: str, color: str) -> None:
        color_var, preview, _choose_button, _reset_button, _default = (
            self._color_widgets[property_name]
        )
        red, green, blue = (
            int(color[index:index + 2], 16) for index in (1, 3, 5)
        )
        luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
        color_var.set(color)
        preview.configure(background=color, foreground="#000000" if luminance > 0.5 else "#ffffff")

    def _update_color_controls(self, screen) -> None:
        for property_name, widgets in self._color_widgets.items():
            _color_var, _preview, choose_button, reset_button, default_color = widgets
            color = getattr(screen, property_name) if screen else default_color
            self._set_color_preview(property_name, color)
            state = "normal" if screen else "disabled"
            choose_button.configure(state=state)
            reset_button.configure(state=state)

    def _choose_color(self, property_name: str) -> None:
        if not self._selected_id:
            return
        screen = self._document.get_screen(self._selected_id)
        current_color = getattr(screen, property_name)
        _rgb, selected_color = colorchooser.askcolor(
            color=current_color,
            parent=self.winfo_toplevel(),
            title=f"Choose screen {property_name.replace('_', ' ')}",
        )
        if selected_color:
            self._set_selected_color(property_name, selected_color)

    def _set_selected_color(self, property_name: str, color: Optional[str]) -> None:
        if not self._selected_id:
            return
        setter = getattr(self._document, f"set_{property_name}")
        try:
            screen = setter(self._selected_id, color)
        except ValueError as exc:
            self._hint_var.set(str(exc))
            self._update_color_controls(
                self._document.get_screen(self._selected_id)
            )
            return
        self._update_color_controls(screen)
        self._render()
        self._persist()

    def _reset_color(self, property_name: str) -> None:
        self._set_selected_color(property_name, None)

    def _set_description_editor(self, screen) -> None:
        self._description_text.configure(state=tk.NORMAL)
        self._description_text.delete("1.0", tk.END)
        if screen is not None:
            self._description_text.insert("1.0", screen.description)
            self._description_save_button.configure(state="normal")
        else:
            self._description_save_button.configure(state="disabled")
        self._description_text.configure(
            state=tk.NORMAL if screen is not None else tk.DISABLED
        )

    def _save_description(self) -> bool:
        if not self._selected_id:
            return False
        description = self._description_text.get("1.0", "end-1c")
        try:
            self._document.set_description(self._selected_id, description)
        except ValueError as exc:
            self._hint_var.set(str(exc))
            return False
        return self._persist()

    def _set_links_editor(self, screen) -> None:
        self._links_buffer = screen.links if screen is not None else []
        self._links_listbox.configure(state=tk.NORMAL)
        self._link_label_entry.configure(state=tk.NORMAL)
        self._link_url_entry.configure(state=tk.NORMAL)
        self._link_browse_button.configure(state=tk.NORMAL)
        self._links_listbox.delete(0, tk.END)
        self._link_label_entry.delete(0, tk.END)
        self._link_url_entry.delete(0, tk.END)
        for record in self._links_buffer:
            display_label = record["label"] or record["url"]
            self._links_listbox.insert(
                tk.END, f"{display_label} — {record['url']}"
            )

        enabled = screen is not None
        self._link_add_button.configure(state="normal" if enabled else "disabled")
        self._link_save_button.configure(state="normal" if enabled else "disabled")
        self._link_update_button.configure(state="disabled")
        self._link_remove_button.configure(state="disabled")
        if not enabled:
            self._links_listbox.configure(state=tk.DISABLED)
            self._link_label_entry.configure(state=tk.DISABLED)
            self._link_url_entry.configure(state=tk.DISABLED)
            self._link_browse_button.configure(state=tk.DISABLED)

    def _on_link_selected(self, _event=None) -> None:
        selection = self._links_listbox.curselection()
        if not selection:
            self._link_label_entry.delete(0, tk.END)
            self._link_url_entry.delete(0, tk.END)
            self._link_update_button.configure(state="disabled")
            self._link_remove_button.configure(state="disabled")
            return
        index = int(selection[0])
        if index >= len(self._links_buffer):
            return
        record = self._links_buffer[index]
        self._link_label_entry.delete(0, tk.END)
        self._link_label_entry.insert(0, record["label"])
        self._link_url_entry.delete(0, tk.END)
        self._link_url_entry.insert(0, record["url"])
        self._link_update_button.configure(state="normal")
        self._link_remove_button.configure(state="normal")

    def _refresh_link_list(self, selected_index: Optional[int] = None) -> None:
        self._links_listbox.configure(state=tk.NORMAL)
        self._links_listbox.delete(0, tk.END)
        for record in self._links_buffer:
            display_label = record["label"] or record["url"]
            self._links_listbox.insert(
                tk.END, f"{display_label} — {record['url']}"
            )
        if selected_index is not None and self._links_buffer:
            selected_index = min(selected_index, len(self._links_buffer) - 1)
            self._links_listbox.selection_set(selected_index)
            self._links_listbox.activate(selected_index)
            self._on_link_selected()
        else:
            self._link_label_entry.delete(0, tk.END)
            self._link_url_entry.delete(0, tk.END)
            self._link_update_button.configure(state="disabled")
            self._link_remove_button.configure(state="disabled")

    def _link_from_fields(self) -> dict[str, str]:
        return {
            "label": self._link_label_entry.get(),
            "url": self._link_url_entry.get(),
        }

    def _browse_link_path(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title="Select a reference file",
            filetypes=(("All files", "*.*"),),
        )
        if path:
            self._link_url_entry.delete(0, tk.END)
            self._link_url_entry.insert(0, path)

    def _add_link(self) -> bool:
        try:
            record = self._document.validate_links([self._link_from_fields()])[0]
        except ValueError as exc:
            self._hint_var.set(str(exc))
            return False
        self._links_buffer.append(record)
        self._refresh_link_list(len(self._links_buffer) - 1)
        return True

    def _update_link(self) -> bool:
        selection = self._links_listbox.curselection()
        if not selection:
            return False
        index = int(selection[0])
        updated = dict(self._links_buffer[index])
        updated.update(self._link_from_fields())
        try:
            updated = self._document.validate_links([updated])[0]
        except ValueError as exc:
            self._hint_var.set(str(exc))
            return False
        self._links_buffer[index] = updated
        self._refresh_link_list(index)
        return True

    def _remove_link(self) -> bool:
        selection = self._links_listbox.curselection()
        if not selection:
            return False
        index = int(selection[0])
        del self._links_buffer[index]
        self._refresh_link_list(index if self._links_buffer else None)
        return True

    def _save_links(self) -> bool:
        if not self._selected_id:
            return False
        try:
            self._document.set_links(self._selected_id, self._links_buffer)
        except ValueError as exc:
            self._hint_var.set(str(exc))
            return False
        return self._persist()

    def _set_connection_editor(self, connection) -> None:
        self._connection_label_entry.configure(state=tk.NORMAL)
        self._connection_label_entry.delete(0, tk.END)
        self._connection_condition_entry.configure(state=tk.NORMAL)
        self._connection_condition_entry.delete(0, tk.END)
        if connection is None:
            self._connection_source_var.set("Source: —")
            self._connection_destination_var.set("Destination: —")
            self._connection_label_entry.configure(state=tk.DISABLED)
            for button in (
                self._connection_save_button,
                self._connection_clear_button,
                self._connection_condition_save_button,
                self._connection_condition_clear_button,
                self._connection_remove_button,
            ):
                button.configure(state="disabled")
            return

        self._connection_condition_entry.insert(0, connection.condition or "")
        source = self._document.get_screen(connection.source_id)
        destination = self._document.get_screen(connection.destination_id)
        self._connection_source_var.set(
            f"Source: {source.name if source else connection.source_id}"
        )
        self._connection_destination_var.set(
            f"Destination: {destination.name if destination else connection.destination_id}"
        )
        self._connection_label_entry.insert(0, connection.label or "")
        for button in (
            self._connection_save_button,
            self._connection_clear_button,
            self._connection_condition_save_button,
            self._connection_condition_clear_button,
            self._connection_remove_button,
        ):
            button.configure(state="normal")

    def _select_connection(self, source_id: str, destination_id: str) -> None:
        connection = next(
            (
                item
                for item in self._document.connections
                if item.source_id == source_id
                and item.destination_id == destination_id
            ),
            None,
        )
        if connection is None:
            return
        self.select_screen(None)
        self._selected_connection = (source_id, destination_id)
        self._set_connection_editor(connection)
        self._render()

    def _save_connection_label(self) -> bool:
        if self._selected_connection is None:
            return False
        source_id, destination_id = self._selected_connection
        try:
            connection = self._document.set_connection_label(
                source_id, destination_id, self._connection_label_entry.get()
            )
        except ValueError as exc:
            self._hint_var.set(str(exc))
            return False
        self._set_connection_editor(connection)
        self._render()
        return self._persist()

    def _clear_connection_label(self) -> bool:
        if self._selected_connection is None:
            return False
        source_id, destination_id = self._selected_connection
        try:
            connection = self._document.set_connection_label(
                source_id, destination_id, None
            )
        except ValueError as exc:
            self._hint_var.set(str(exc))
            return False
        self._set_connection_editor(connection)
        self._render()
        return self._persist()

    def _save_connection_condition(self) -> bool:
        if self._selected_connection is None:
            return False
        source_id, destination_id = self._selected_connection
        try:
            connection = self._document.set_connection_condition(
                source_id,
                destination_id,
                self._connection_condition_entry.get(),
            )
        except ValueError as exc:
            self._hint_var.set(str(exc))
            return False
        self._set_connection_editor(connection)
        self._render()
        return self._persist()

    def _clear_connection_condition(self) -> bool:
        if self._selected_connection is None:
            return False
        source_id, destination_id = self._selected_connection
        try:
            connection = self._document.set_connection_condition(
                source_id, destination_id, None
            )
        except ValueError as exc:
            self._hint_var.set(str(exc))
            return False
        self._set_connection_editor(connection)
        self._render()
        return self._persist()

    def _remove_selected_connection(self) -> bool:
        if self._selected_connection is None:
            return False
        source_id, destination_id = self._selected_connection
        removed = self._document.disconnect(source_id, destination_id)
        self._selected_connection = None
        self._set_connection_editor(None)
        self._render()
        if removed:
            self._persist()
        return removed

    def _delete_connection(self, source_id: str, destination_id: str) -> None:
        if self._document.disconnect(source_id, destination_id):
            if self._selected_connection == (source_id, destination_id):
                self._selected_connection = None
                self._set_connection_editor(None)
            self._render()
            self._persist()

    def _screen_tag(self, screen_id: str) -> str:
        return f"screen_{screen_id}"

    @staticmethod
    def _shape_coordinates(screen) -> tuple[float, ...]:
        x, y, width, height = screen.x, screen.y, screen.width, screen.height
        if screen.shape == "sharp":
            return (x, y, x + width, y + height)
        if screen.shape == "diamond":
            return (
                x + width / 2, y,
                x + width, y + height / 2,
                x + width / 2, y + height,
                x, y + height / 2,
            )
        if screen.shape == "circle":
            diameter = min(width, height)
            left = x + (width - diameter) / 2
            top = y + (height - diameter) / 2
            return (left, top, left + diameter, top + diameter)

        radius = min(width / 2, height / 2)
        if screen.shape == "rounded":
            radius = min(12.0, width / 4, height / 4)
        corners = (
            (x + width - radius, y + radius, -90, 0),
            (x + width - radius, y + height - radius, 0, 90),
            (x + radius, y + height - radius, 90, 180),
            (x + radius, y + radius, 180, 270),
        )
        points: list[float] = []
        for center_x, center_y, start, end in corners:
            for step in range(5):
                angle = math.radians(start + (end - start) * step / 4)
                points.extend(
                    (center_x + radius * math.cos(angle), center_y + radius * math.sin(angle))
                )
        return tuple(points)

    @staticmethod
    def _screen_label_y(screen) -> float:
        return screen.y + screen.height / 2 + 12 if screen.icon_glyph else screen.y + screen.height / 2

    def _render(self) -> None:
        self._canvas.delete("all")
        self._draw_connections()
        for screen in self._document.screens:
            tag = self._screen_tag(screen.id)
            is_selected = screen.id == self._selected_id
            is_pending = screen.id == self._pending_connection_id
            fill = "#e7ecfb" if is_pending else screen.fill_color
            shape_options = {
                "fill": fill,
                "outline": screen.border_color,
                "width": 3 if is_selected or is_pending else 1,
                "tags": ("screen", tag, f"{tag}_shape"),
            }
            if screen.shape == "sharp":
                self._canvas.create_rectangle(
                    *self._shape_coordinates(screen), **shape_options
                )
            elif screen.shape == "circle":
                self._canvas.create_oval(
                    *self._shape_coordinates(screen), **shape_options
                )
            else:
                self._canvas.create_polygon(
                    *self._shape_coordinates(screen), **shape_options
                )
            self._canvas.create_text(
                screen.x + screen.width / 2,
                self._screen_label_y(screen),
                text=(
                    f"▶ {screen.name}"
                    if screen.id == self._document.entry_screen_id
                    else screen.name
                ),
                width=max(30, screen.width - 20),
                fill="#1e1e1a",
                font=("TkDefaultFont", 10, "bold"),
                tags=("screen", tag, f"{tag}_label"),
            )
            if screen.icon_glyph:
                self._canvas.create_text(
                    screen.x + screen.width / 2,
                    screen.y + 22,
                    text=screen.icon_glyph,
                    font=("Segoe UI Emoji", 16),
                    tags=("screen", tag, f"{tag}_icon"),
                )
            self._canvas.tag_bind(
                tag,
                "<Button-1>",
                lambda event, sid=screen.id: self._on_screen_click(sid, event),
            )
            self._canvas.tag_bind(
                tag,
                "<Double-Button-1>",
                lambda _event, sid=screen.id: self._begin_rename(sid),
            )

    def _draw_connections(self) -> None:
        self._canvas.delete("connection")
        for index, connection in enumerate(self._document.connections):
            source = self._document.get_screen(connection.source_id)
            destination = self._document.get_screen(connection.destination_id)
            if source is None or destination is None:
                continue
            line_tag = f"connection_{index}"
            selected = self._selected_connection == (
                connection.source_id,
                connection.destination_id,
            )
            self._canvas.create_line(
                source.x + source.width / 2,
                source.y + source.height / 2,
                destination.x + destination.width / 2,
                destination.y + destination.height / 2,
                fill="#3d4eba" if selected else "#6b6a62",
                width=3 if selected else 2,
                arrow=tk.LAST,
                tags=("connection", line_tag),
            )
            self._canvas.tag_bind(
                line_tag,
                "<Button-1>",
                lambda _event, a=connection.source_id, b=connection.destination_id:
                    self._select_connection(a, b),
            )
        self._canvas.tag_lower("connection")

    def _begin_rename(self, screen_id: str):
        self.select_screen(screen_id)
        self._name_entry.focus_set()
        self._name_entry.selection_range(0, tk.END)
        return "break"


__all__ = ["VisualDesignWorkspace"]
