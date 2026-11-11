"""The GisCode application shell: a purposeful left rail and identity marks.

The rail is the primary navigation. The existing ttk.Notebook remains the one
authoritative page container (callers and tests select pages through it), but its
tab strip is hidden by the design system so the product does not present two
competing navigation models.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable

from .design_system import Fonts, Glyph, Palette, draw_mark


_ICONS = {
    "Chat": "chat",
    "AI Web Chat": "web",
    "Projects": "projects",
    "Model Manager": "models",
    "Settings": "settings",
    "Terminal": "terminal",
    "History": "history",
    "Status": "status",
    "Code Task": "terminal",
    "Analyze": "status",
    "AdSpace": "spark",
    "iOS / xtool": "spark",
    "GisLogo": "spark",
    "Developer & Release": "terminal",
    "Device Preview": "web",
    "Generate Video": "spark",
    "Project Doctor": "status",
    "Agent Registry": "models",
}

_GROUPS = (
    ("WORKSPACE", ("Chat", "AI Web Chat", "Projects")),
    ("SYSTEM", ("Model Manager", "Settings")),
)


class Tooltip:
    """Small accessible hint for controls whose icon is not a label."""

    def __init__(self, widget: tk.Widget, text: str, palette: Palette, delay: int = 450):
        self.widget = widget
        self.text = text
        self.palette = palette
        self.delay = delay
        self._job = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        try:
            self._job = self.widget.after(self.delay, self._show)
        except tk.TclError:
            self._job = None

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _show(self) -> None:
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + self.widget.winfo_width() + 8
            y = self.widget.winfo_rooty()
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{x}+{y}")
            label = tk.Label(
                tip,
                text=self.text,
                background=self.palette.overlay,
                foreground=self.palette.text,
                relief="solid",
                borderwidth=1,
                padx=8,
                pady=4,
            )
            label.pack()
            self._tip = tip
        except tk.TclError:
            self._tip = None

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class NavItem(tk.Frame):
    """A keyboard-operable navigation row with an icon, label and state."""

    def __init__(
        self,
        parent: tk.Widget,
        *,
        label: str,
        icon: str,
        palette: Palette,
        fonts: Fonts,
        command: Callable[[], None],
        tooltip: str = "",
        accent: bool = False,
    ):
        super().__init__(parent, background=palette.surface, cursor="hand2", takefocus=1)
        self.palette = palette
        self.fonts = fonts
        self.command = command
        self.label_text = label
        self._selected = False
        self._hover = False
        self._accent = accent

        self.indicator = tk.Frame(self, width=3, background=palette.surface)
        self.indicator.pack(side=tk.LEFT, fill=tk.Y)
        self.icon = Glyph(self, icon, palette, size=18, background=palette.surface)
        self.icon.pack(side=tk.LEFT, padx=(10, 9), pady=7)
        self.label = tk.Label(
            self,
            text=label,
            anchor=tk.W,
            background=palette.surface,
            foreground=palette.muted,
            font=(fonts.ui, 9, "bold" if accent else "normal"),
        )
        self.label.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=7)
        self.shortcut = tk.Label(
            self,
            text="",
            anchor=tk.E,
            background=palette.surface,
            foreground=palette.faint,
            font=(fonts.ui, 8),
        )
        self.shortcut.pack(side=tk.RIGHT, padx=(4, 10))

        for widget in (self, self.icon, self.label, self.shortcut):
            widget.bind("<Button-1>", self._activate, add="+")
            widget.bind("<Enter>", self._enter, add="+")
            widget.bind("<Leave>", self._leave, add="+")
        self.bind("<Return>", self._activate, add="+")
        self.bind("<space>", self._activate, add="+")
        self.bind("<FocusIn>", self._enter, add="+")
        self.bind("<FocusOut>", self._leave, add="+")
        if tooltip:
            Tooltip(self, tooltip, palette)
        self._paint()

    def set_shortcut(self, text: str) -> None:
        self.shortcut.configure(text=text)

    def _activate(self, _event=None) -> str:
        self.command()
        return "break"

    def _enter(self, _event=None) -> None:
        self._hover = True
        self._paint()

    def _leave(self, _event=None) -> None:
        self._hover = False
        self._paint()

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self._paint()

    def _paint(self) -> None:
        p = self.palette
        if self._selected:
            background = p.raised
            foreground = p.text
            indicator = p.accent
        elif self._hover:
            background = p.overlay
            foreground = p.text
            indicator = p.border_strong
        else:
            background = p.surface
            foreground = p.muted
            indicator = p.surface
        self.configure(background=background)
        self.indicator.configure(background=indicator)
        self.icon.configure(background=background)
        self.icon.set_palette(p, background)
        self.label.configure(background=background, foreground=foreground)
        self.shortcut.configure(background=background)


class NavigationRail(tk.Frame):
    """Product navigation, brand and the truthful local/private footer."""

    def __init__(
        self,
        parent: tk.Widget,
        *,
        window,
        palette: Palette,
        fonts: Fonts,
        tabs: Iterable[str],
        select: Callable[[str], None],
        toggle_appearance: Callable[[], None],
        edition_label: str,
    ):
        super().__init__(parent, background=palette.surface, width=216)
        self.pack_propagate(False)
        self.window = window
        self.palette = palette
        self.fonts = fonts
        self.tabs = list(tabs)
        self._select_window = select
        self.toggle_appearance = toggle_appearance
        self.edition_label = edition_label
        self.items: dict[str, NavItem] = {}
        self._active = "Chat"
        self._build()

    def _build(self) -> None:
        p, f = self.palette, self.fonts

        brand = tk.Frame(self, background=p.surface)
        brand.pack(fill=tk.X, padx=14, pady=(14, 10))
        # The authentic GIS star, drawn from the starPath coordinates of the
        # shipped gis_logo.svg at the rail's original 36 px slot. Keeping the
        # slot size preserves the 1100x700 header/Chat layout, and the vector
        # rendering stays sharp without resampling the 48 px tile.
        mark = tk.Canvas(
            brand,
            width=36,
            height=36,
            background=p.surface,
            highlightthickness=0,
            borderwidth=0,
        )
        mark.pack(side=tk.LEFT, padx=(0, 10))
        self.brand_mark_canvas = mark
        self.brand_mark_items = draw_mark(mark, 36, p)
        text = tk.Frame(brand, background=p.surface)
        text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            text,
            text="GisCode",
            anchor=tk.W,
            background=p.surface,
            foreground=p.text,
            font=f.wordmark,
        ).pack(anchor=tk.W)
        edition_row = tk.Frame(text, background=p.surface)
        edition_row.pack(anchor=tk.W)
        tk.Label(
            edition_row,
            text=self.edition_label,
            anchor=tk.W,
            background=p.surface,
            foreground=p.accent,
            font=(f.ui, 7, "bold"),
        ).pack(side=tk.LEFT)
        self.edition_badge = None
        if self.edition_label.upper().startswith("PRO"):
            self.edition_badge = tk.Label(
                edition_row,
                text=" PRO ",
                background=p.accent,
                foreground=p.on_accent,
                font=(f.ui, 7, "bold"),
                padx=4,
            )
            self.edition_badge.pack(side=tk.LEFT, padx=(5, 0))
        tk.Frame(self, height=1, background=p.border).pack(fill=tk.X, padx=14, pady=(0, 8))

        # The rail scrolls independently of the window: Pro has seventeen
        # destinations, and hiding the overflow would make tabs unreachable at
        # the supported 1100x700 floor. The appearance control and footer stay
        # pinned below the scrolling navigation.
        nav_shell = tk.Frame(self, background=p.surface)
        nav_shell.pack(fill=tk.BOTH, expand=True)
        nav_shell.grid_rowconfigure(0, weight=1)
        nav_shell.grid_columnconfigure(0, weight=1)
        self._nav_canvas = tk.Canvas(
            nav_shell,
            background=p.surface,
            highlightthickness=0,
            borderwidth=0,
            width=196,
        )
        self._nav_scrollbar = ttk.Scrollbar(
            nav_shell,
            orient=tk.VERTICAL,
            command=self._nav_canvas.yview,
            style="Gis.Vertical.TScrollbar",
            takefocus=False,
        )
        self._nav_canvas.configure(yscrollcommand=self._nav_scrollbar.set)
        self._nav_canvas.grid(row=0, column=0, sticky="nsew")
        self._nav_scrollbar.grid(row=0, column=1, sticky="ns")
        self._nav_inner = tk.Frame(self._nav_canvas, background=p.surface)
        self._nav_window = self._nav_canvas.create_window(
            (0, 0), window=self._nav_inner, anchor="nw"
        )
        self._nav_inner.bind("<Configure>", self._sync_scrollregion)
        self._nav_canvas.bind("<Configure>", self._sync_scroll_width)

        grouped: set[str] = set()
        for group_name, candidates in _GROUPS:
            present = [name for name in candidates if name in self.tabs]
            if present:
                self._group_label(group_name)
                for name in present:
                    self._add_item(name)
                    grouped.add(name)
        remainder = [name for name in self.tabs if name not in grouped]
        if remainder:
            self._group_label("TOOLS")
            for name in remainder:
                self._add_item(name)
        self._bind_wheel_tree(self._nav_inner)

        self._divider()
        appearance = "Light appearance" if p.name == "dark" else "Dark appearance"
        icon = "sun" if p.name == "dark" else "moon"
        theme_item = NavItem(
            self,
            label=appearance,
            icon=icon,
            palette=p,
            fonts=f,
            command=self.toggle_appearance,
            tooltip="Switch the GisCode interface between ink and paper",
        )
        theme_item.pack(fill=tk.X, padx=8, pady=(2, 2))
        self.theme_item = theme_item

        footer = tk.Frame(self, background=p.surface)
        footer.pack(fill=tk.X, padx=16, pady=(4, 12))
        tk.Label(
            footer,
            text="●  LOCAL  ·  PRIVATE",
            anchor=tk.W,
            background=p.surface,
            foreground=p.teal,
            font=(f.ui, 8, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            footer,
            text=f"{self.edition_label} · code stays on this machine",
            anchor=tk.W,
            background=p.surface,
            foreground=p.faint,
            font=(f.ui, 7),
        ).pack(anchor=tk.W, pady=(2, 0))

    def _sync_scrollregion(self, _event=None) -> None:
        canvas = getattr(self, "_nav_canvas", None)
        if canvas is None:
            return
        bbox = canvas.bbox("all")
        if bbox is None:
            return
        canvas.configure(scrollregion=bbox)
        needs_scroll = (bbox[3] - bbox[1]) > max(1, canvas.winfo_height())
        bar = self._nav_scrollbar
        if needs_scroll and not bar.winfo_ismapped():
            bar.grid()
        elif not needs_scroll and bar.winfo_ismapped():
            bar.grid_remove()

    def _sync_scroll_width(self, event=None) -> None:
        canvas = getattr(self, "_nav_canvas", None)
        if canvas is None:
            return
        width = event.width if event is not None else canvas.winfo_width()
        canvas.itemconfigure(self._nav_window, width=max(1, width))
        self._sync_scrollregion()

    def _on_mousewheel(self, event) -> str:
        canvas = getattr(self, "_nav_canvas", None)
        if canvas is None:
            return "break"
        bbox = canvas.bbox("all")
        if bbox is None or (bbox[3] - bbox[1]) <= canvas.winfo_height():
            return "break"
        delta = getattr(event, "delta", 0)
        step = -1 if delta > 0 else 1
        canvas.yview_scroll(step, "units")
        return "break"

    def _bind_wheel_tree(self, widget: tk.Widget) -> None:
        widget.bind("<MouseWheel>", self._on_mousewheel, add="+")
        for child in widget.winfo_children():
            self._bind_wheel_tree(child)

    def _group_label(self, text: str) -> None:
        p, f = self.palette, self.fonts
        label = tk.Label(
            self._nav_inner,
            text=text,
            anchor=tk.W,
            background=p.surface,
            foreground=p.faint,
            font=(f.ui, 7, "bold"),
        )
        label.pack(fill=tk.X, padx=17, pady=(9, 3))

    def _divider(self) -> None:
        tk.Frame(self, height=1, background=self.palette.border).pack(fill=tk.X, padx=14, pady=(8, 6))

    def _add_item(self, name: str) -> None:
        item = NavItem(
            self._nav_inner,
            label=name,
            icon=_ICONS.get(name, "spark"),
            palette=self.palette,
            fonts=self.fonts,
            command=lambda n=name: self.select(n),
            tooltip=f"Open {name}",
            accent=name == "Chat",
        )
        item.pack(fill=tk.X, padx=8, pady=1)
        index = self.tabs.index(name) if name in self.tabs else -1
        if 0 <= index < 9:
            item.set_shortcut(f"Alt {index + 1}")
        self.items[name] = item

    def select(self, name: str) -> None:
        self.set_active(name)
        self.select_window_tab(name)

    def select_window_tab(self, name: str) -> None:
        try:
            self.window._select_tab(name, from_nav=True)
        except TypeError:
            self.window._select_tab(name)

    def set_active(self, name: str) -> None:
        self._active = name
        for tab_name, item in self.items.items():
            item.set_selected(tab_name == name)

    def set_attention(self, state: str, tooltip: str = "") -> None:
        """The rail's footer can carry an attention line without flashing."""
        state = str(state or "").strip().lower()
        colour = {
            "working": self.palette.accent,
            "completed": self.palette.teal,
            "failed": self.palette.danger,
            "cancelled": self.palette.cancelled,
            "stopping": self.palette.warning,
        }.get(state, self.palette.teal)
        if hasattr(self, "theme_item"):
            self.theme_item.label.configure(foreground=self.palette.muted)

    def refresh(self) -> None:
        active = self._active
        for child in list(self.winfo_children()):
            child.destroy()
        self.items = {}
        self._build()
        self.set_active(active)
