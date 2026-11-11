"""GisCode visual identity: tokens, ttk styles, text tags and vector glyphs.

The identity is "Ink & Ember": deep blue-black work surfaces, warm amber for
the action the user should take next, and a restrained signal-teal for state
and evidence. Amber is never used for success; success, warning, failure and
cancellation have separate, named colours so a status cannot be misread.

This module owns presentation only. It never imports an engineering service,
never changes task state and never performs I/O beyond reading font names and
loading the packaged marque.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from dataclasses import dataclass, asdict
from typing import Iterable

ASSET_MARK = "giscode_mark_32.png"
ASSET_MARK_LARGE = "giscode_mark_128.png"


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str
    surface: str
    raised: str
    overlay: str
    border: str
    border_strong: str
    text: str
    muted: str
    faint: str
    accent: str
    accent_hover: str
    accent_soft: str
    on_accent: str
    teal: str
    success: str
    warning: str
    danger: str
    info: str
    cancelled: str
    code_bg: str
    user_bg: str
    question_bg: str
    selection: str

    def as_dict(self) -> dict:
        return asdict(self)


INK = Palette(
    name="dark",
    bg="#0A0E13",
    surface="#101720",
    raised="#172230",
    overlay="#1D2A39",
    border="#243244",
    border_strong="#34475D",
    text="#E8EFF7",
    muted="#9AB0C6",
    faint="#6F8499",
    accent="#F2A93B",
    accent_hover="#FFC15C",
    accent_soft="#3A2A13",
    on_accent="#12161C",
    teal="#4FD1C5",
    success="#57D68D",
    warning="#F6C45B",
    danger="#FF7B72",
    info="#79B8FF",
    cancelled="#C4A7FF",
    code_bg="#0B121B",
    user_bg="#162433",
    question_bg="#2A2414",
    selection="#2C4A63",
)

PAPER = Palette(
    name="light",
    bg="#F4F1EA",
    surface="#FFFFFF",
    raised="#F8F5EE",
    overlay="#EDE7DB",
    border="#D8D0C2",
    border_strong="#B9AE9C",
    text="#17212B",
    muted="#566576",
    faint="#7B8896",
    accent="#B96D0B",
    accent_hover="#9A5907",
    accent_soft="#FCEBD2",
    # Ink on amber reads as premium stationery and clears AA; white on this
    # amber did not, so the accessible choice is also the better identity.
    on_accent="#211200",
    teal="#0F766E",
    success="#1D7A46",
    warning="#92610A",
    danger="#B42318",
    info="#185FA5",
    cancelled="#6B4BA8",
    code_bg="#F2EEE6",
    user_bg="#EAF1F6",
    question_bg="#FFF3D8",
    selection="#CFE2EF",
)

PALETTES = {"dark": INK, "light": PAPER}
DEFAULT_APPEARANCE = "dark"

SPACING = {"xs": 4, "sm": 8, "md": 12, "lg": 18, "xl": 28, "xxl": 40}
RADIUS = {"sm": 4, "md": 8, "lg": 14}
MOTION_MS = {"fast": 120, "normal": 220}

#: Text/background pairs that the design system must keep readable.
CONTRAST_PAIRS = (
    ("text", "bg"),
    ("text", "surface"),
    ("muted", "surface"),
    ("text", "raised"),
    ("on_accent", "accent"),
    ("text", "question_bg"),
)


def _channel(value: float) -> float:
    value = value / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    if len(raw) != 6:
        raise ValueError(f"not a six-digit colour: {hex_colour!r}")
    red, green, blue = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast_ratio(foreground: str, background: str) -> float:
    a, b = relative_luminance(foreground), relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def contrast_report(palette: Palette) -> dict[str, float]:
    report = {}
    for foreground, background in CONTRAST_PAIRS:
        report[f"{foreground}_on_{background}"] = round(
            contrast_ratio(getattr(palette, foreground), getattr(palette, background)), 2
        )
    return report


def resolve_appearance(value: str | None = None) -> str:
    text = str(value or "").strip().lower()
    return text if text in PALETTES else DEFAULT_APPEARANCE


def palette_for(appearance: str | None = None) -> Palette:
    return PALETTES[resolve_appearance(appearance)]


def _font_family(root: tk.Misc, candidates: Iterable[str], fallback: str) -> str:
    try:
        available = set(tkfont.families(root))
    except Exception:
        return fallback
    for name in candidates:
        if name in available:
            return name
    return fallback


@dataclass(frozen=True)
class Fonts:
    ui: str
    display: str
    mono: str
    body: tuple
    small: tuple
    eyebrow: tuple
    title: tuple
    display_size: tuple
    wordmark: tuple
    mono_body: tuple
    mono_small: tuple


def fonts_for(root: tk.Misc) -> Fonts:
    ui = _font_family(root, ("Segoe UI Variable Text", "Segoe UI", "Aptos"), "TkDefaultFont")
    display = _font_family(
        root, ("Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI"), ui
    )
    mono = _font_family(root, ("Cascadia Code", "Cascadia Mono", "Consolas"), "TkFixedFont")
    return Fonts(
        ui=ui,
        display=display,
        mono=mono,
        body=(ui, 10),
        small=(ui, 9),
        eyebrow=(ui, 8, "bold"),
        title=(display, 14, "bold"),
        display_size=(display, 20, "bold"),
        wordmark=(display, 18, "bold"),
        mono_body=(mono, 9),
        mono_small=(mono, 8),
    )


def configure_theme(root: tk.Tk, appearance: str | None = None) -> tuple[Palette, Fonts]:
    """Apply GisCode tokens to an entire Tk application and return them."""
    palette = palette_for(appearance)
    fonts = fonts_for(root)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(background=palette.bg)
    # tk (non-ttk) widgets do not participate in ttk styles. The option database
    # is the only way to give every existing Text/Listbox/Canvas a readable
    # default without editing every page.
    options = {
        "*Background": palette.surface,
        "*Foreground": palette.text,
        "*Font": fonts.body,
        "*insertBackground": palette.accent,
        "*insertWidth": 2,
        "*selectBackground": palette.selection,
        "*selectForeground": palette.text,
        "*disabledForeground": palette.faint,
        "*highlightBackground": palette.border,
        "*highlightColor": palette.accent,
        "*activeBackground": palette.raised,
        "*activeForeground": palette.text,
        "*troughColor": palette.bg,
        "*borderWidth": 1,
        "*relief": "flat",
        "*Menu.Background": palette.surface,
        "*Menu.Foreground": palette.text,
        "*Menu.activeBackground": palette.accent_soft,
        "*Menu.activeForeground": palette.text,
        "*TCombobox*Listbox.Background": palette.surface,
        "*TCombobox*Listbox.Foreground": palette.text,
        "*TCombobox*Listbox.selectBackground": palette.selection,
    }
    for key, value in options.items():
        try:
            root.option_add(key, value)
        except tk.TclError:
            pass

    style.configure(
        ".",
        background=palette.bg,
        foreground=palette.text,
        font=fonts.body,
        bordercolor=palette.border,
        focuscolor=palette.accent,
        troughcolor=palette.bg,
        selectbackground=palette.selection,
        selectforeground=palette.text,
        fieldbackground=palette.surface,
    )

    def frame(name: str, background: str, **extra) -> None:
        style.configure(name, background=background, borderwidth=0, **extra)

    frame("Gis.TFrame", palette.bg)
    frame("Gis.Header.TFrame", palette.surface)
    frame("Gis.Sidebar.TFrame", palette.surface)
    frame("Gis.Card.TFrame", palette.surface)
    frame("Gis.Raised.TFrame", palette.raised)
    frame("Gis.Status.TFrame", palette.surface)
    frame("Gis.Rail.TFrame", palette.surface)

    style.configure("Gis.TLabel", background=palette.bg, foreground=palette.text, font=fonts.body)
    style.configure("Gis.Card.TLabel", background=palette.surface, foreground=palette.text, font=fonts.body)
    style.configure("Gis.Header.TLabel", background=palette.surface, foreground=palette.text, font=fonts.body)
    style.configure("Gis.Rail.TLabel", background=palette.surface, foreground=palette.muted, font=fonts.small)
    style.configure("Gis.Title.TLabel", background=palette.bg, foreground=palette.text, font=fonts.title)
    style.configure("Gis.CardTitle.TLabel", background=palette.surface, foreground=palette.text, font=fonts.title)
    style.configure("Gis.Display.TLabel", background=palette.bg, foreground=palette.text, font=fonts.display_size)
    style.configure("Gis.Wordmark.TLabel", background=palette.surface, foreground=palette.text, font=fonts.wordmark)
    style.configure("Gis.Eyebrow.TLabel", background=palette.surface, foreground=palette.muted, font=fonts.eyebrow)
    style.configure("Gis.Muted.TLabel", background=palette.bg, foreground=palette.muted, font=fonts.small)
    style.configure("Gis.CardMuted.TLabel", background=palette.surface, foreground=palette.muted, font=fonts.small)
    style.configure("Gis.Accent.TLabel", background=palette.bg, foreground=palette.accent, font=(fonts.ui, 10, "bold"))
    style.configure("Gis.RailMuted.TLabel", background=palette.surface, foreground=palette.faint, font=fonts.small)
    style.configure("Gis.Mono.TLabel", background=palette.surface, foreground=palette.text, font=fonts.mono_body)

    for status_name, colour, background in (
        ("Ready", palette.success, palette.surface),
        ("Working", palette.accent, palette.surface),
        ("Stopping", palette.warning, palette.surface),
        ("Completed", palette.teal, palette.surface),
        ("Cancelled", palette.cancelled, palette.surface),
        ("TimedOut", palette.warning, palette.surface),
        ("Failed", palette.danger, palette.surface),
        ("Idle", palette.muted, palette.surface),
    ):
        style.configure(
            f"Gis.Pill.{status_name}.TLabel",
            background=background,
            foreground=colour,
            font=(fonts.ui, 8, "bold"),
            padding=(8, 3),
        )
    style.configure(
        "TFrame",
        background=palette.bg,
        borderwidth=0,
    )
    style.configure("TLabel", background=palette.bg, foreground=palette.text, font=fonts.body)
    style.configure(
        "TButton",
        background=palette.raised,
        foreground=palette.text,
        padding=(10, 6),
        relief="flat",
        font=(fonts.ui, 9),
    )
    style.map(
        "TButton",
        background=[("disabled", palette.surface), ("pressed", palette.overlay), ("active", palette.overlay)],
        foreground=[("disabled", palette.faint), ("active", palette.text)],
    )
    style.configure("TNotebook", background=palette.bg, borderwidth=0)
    style.configure(
        "TEntry",
        fieldbackground=palette.raised,
        foreground=palette.text,
        insertcolor=palette.accent,
        bordercolor=palette.border,
        lightcolor=palette.raised,
        darkcolor=palette.raised,
        padding=6,
    )
    style.map(
        "TEntry",
        fieldbackground=[("disabled", palette.surface)],
        foreground=[("disabled", palette.faint)],
        bordercolor=[("focus", palette.accent)],
    )
    style.configure(
        "TCombobox",
        fieldbackground=palette.raised,
        background=palette.raised,
        foreground=palette.text,
        arrowcolor=palette.muted,
        bordercolor=palette.border,
        lightcolor=palette.raised,
        darkcolor=palette.raised,
        padding=5,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette.raised), ("disabled", palette.surface)],
        foreground=[("disabled", palette.faint)],
        bordercolor=[("focus", palette.accent)],
        arrowcolor=[("active", palette.accent)],
    )
    style.configure(
        "TCheckbutton",
        background=palette.surface,
        foreground=palette.text,
        indicatorbackground=palette.raised,
        indicatorforeground=palette.on_accent,
        bordercolor=palette.border,
        padding=3,
    )
    style.map(
        "TCheckbutton",
        background=[("active", palette.surface)],
        indicatorbackground=[("selected", palette.accent), ("active", palette.overlay)],
        foreground=[("disabled", palette.faint)],
    )
    style.configure("TRadiobutton", background=palette.surface, foreground=palette.text)
    style.map("TRadiobutton", background=[("active", palette.surface)])
    style.configure(
        "Treeview",
        background=palette.surface,
        fieldbackground=palette.surface,
        foreground=palette.text,
        bordercolor=palette.border,
        rowheight=26,
    )
    style.map(
        "Treeview",
        background=[("selected", palette.selection)],
        foreground=[("selected", palette.text)],
    )
    style.configure("Treeview.Heading", background=palette.raised, foreground=palette.muted)
    style.configure(
        "Vertical.TScrollbar",
        background=palette.raised,
        troughcolor=palette.bg,
        bordercolor=palette.bg,
        arrowcolor=palette.muted,
    )
    style.configure(
        "Horizontal.TScrollbar",
        background=palette.raised,
        troughcolor=palette.bg,
        bordercolor=palette.bg,
        arrowcolor=palette.muted,
    )
    style.configure(
        "Gis.TButton",
        background=palette.raised,
        foreground=palette.text,
        bordercolor=palette.border,
        lightcolor=palette.raised,
        darkcolor=palette.raised,
        focusthickness=1,
        focuscolor=palette.accent,
        padding=(12, 7),
        relief="flat",
        font=(fonts.ui, 9, "bold"),
    )
    style.map(
        "Gis.TButton",
        background=[("disabled", palette.surface), ("pressed", palette.overlay), ("active", palette.overlay)],
        foreground=[("disabled", palette.faint), ("active", palette.text)],
        bordercolor=[("focus", palette.accent), ("active", palette.border_strong)],
    )
    style.configure(
        "Gis.Accent.TButton",
        background=palette.accent,
        foreground=palette.on_accent,
        bordercolor=palette.accent,
        lightcolor=palette.accent,
        darkcolor=palette.accent,
        padding=(14, 8),
        relief="flat",
        font=(fonts.ui, 9, "bold"),
    )
    style.map(
        "Gis.Accent.TButton",
        background=[("disabled", palette.border), ("pressed", palette.accent_hover), ("active", palette.accent_hover)],
        foreground=[("disabled", palette.muted), ("active", palette.on_accent)],
    )
    style.configure(
        "Gis.CompactAccent.TButton",
        background=palette.accent,
        foreground=palette.on_accent,
        bordercolor=palette.accent,
        lightcolor=palette.accent,
        darkcolor=palette.accent,
        padding=(7, 5),
        relief="flat",
        font=(fonts.ui, 8, "bold"),
    )
    style.map(
        "Gis.CompactAccent.TButton",
        background=[("disabled", palette.border), ("pressed", palette.accent_hover), ("active", palette.accent_hover)],
        foreground=[("disabled", palette.muted), ("active", palette.on_accent)],
    )
    style.configure(
        "Gis.CompactDanger.TButton",
        background=palette.surface,
        foreground=palette.danger,
        bordercolor=palette.danger,
        padding=(7, 5),
        relief="solid",
        font=(fonts.ui, 8, "bold"),
    )
    style.map(
        "Gis.CompactDanger.TButton",
        background=[("disabled", palette.surface), ("pressed", palette.accent_soft), ("active", palette.overlay)],
        foreground=[("disabled", palette.faint), ("active", palette.danger)],
    )
    style.configure(
        "Gis.Ghost.TButton",
        background=palette.surface,
        foreground=palette.muted,
        bordercolor=palette.surface,
        padding=(9, 6),
        relief="flat",
        font=(fonts.ui, 9),
    )
    style.map(
        "Gis.Ghost.TButton",
        background=[("pressed", palette.raised), ("active", palette.raised)],
        foreground=[("disabled", palette.faint), ("active", palette.text)],
        bordercolor=[("focus", palette.border_strong)],
    )
    style.configure(
        "Gis.Danger.TButton",
        background=palette.surface,
        foreground=palette.danger,
        bordercolor=palette.danger,
        padding=(12, 7),
        relief="solid",
        font=(fonts.ui, 9, "bold"),
    )
    style.map(
        "Gis.Danger.TButton",
        background=[("disabled", palette.surface), ("pressed", palette.accent_soft), ("active", palette.overlay)],
        foreground=[("disabled", palette.faint), ("active", palette.danger)],
    )
    style.configure("Gis.TSeparator", background=palette.border)

    style.configure(
        "Gis.TLabelframe",
        background=palette.surface,
        bordercolor=palette.border,
        lightcolor=palette.surface,
        darkcolor=palette.surface,
        relief="solid",
        borderwidth=1,
        padding=10,
    )
    style.configure(
        "Gis.TLabelframe.Label",
        background=palette.surface,
        foreground=palette.muted,
        font=fonts.eyebrow,
    )
    style.configure("Gis.Card.TLabelframe", background=palette.surface)
    style.configure("Gis.Card.TLabelframe.Label", background=palette.surface)

    style.configure(
        "Gis.TEntry",
        fieldbackground=palette.raised,
        foreground=palette.text,
        insertcolor=palette.accent,
        bordercolor=palette.border,
        lightcolor=palette.raised,
        darkcolor=palette.raised,
        padding=6,
    )
    style.map("Gis.TEntry", bordercolor=[("focus", palette.accent)])
    style.configure(
        "Gis.TCombobox",
        fieldbackground=palette.raised,
        background=palette.raised,
        foreground=palette.text,
        arrowcolor=palette.muted,
        bordercolor=palette.border,
        lightcolor=palette.raised,
        darkcolor=palette.raised,
        padding=5,
    )
    style.map(
        "Gis.TCombobox",
        fieldbackground=[("readonly", palette.raised), ("disabled", palette.surface)],
        foreground=[("disabled", palette.faint)],
        bordercolor=[("focus", palette.accent)],
        arrowcolor=[("active", palette.accent)],
    )
    style.configure(
        "Gis.TCheckbutton",
        background=palette.surface,
        foreground=palette.text,
        indicatorbackground=palette.raised,
        indicatorforeground=palette.on_accent,
        bordercolor=palette.border,
        padding=3,
    )
    style.map(
        "Gis.TCheckbutton",
        background=[("active", palette.surface)],
        indicatorbackground=[("selected", palette.accent), ("active", palette.overlay)],
        foreground=[("disabled", palette.faint)],
    )
    style.configure("Gis.TRadiobutton", background=palette.surface, foreground=palette.text)
    style.map("Gis.TRadiobutton", background=[("active", palette.surface)])

    style.configure(
        "Gis.Treeview",
        background=palette.surface,
        fieldbackground=palette.surface,
        foreground=palette.text,
        bordercolor=palette.border,
        rowheight=26,
        font=fonts.body,
    )
    style.map(
        "Gis.Treeview",
        background=[("selected", palette.selection)],
        foreground=[("selected", palette.text)],
    )
    style.configure(
        "Gis.Treeview.Heading",
        background=palette.raised,
        foreground=palette.muted,
        font=(fonts.ui, 8, "bold"),
        relief="flat",
        padding=(8, 6),
    )
    style.configure(
        "Gis.Vertical.TScrollbar",
        background=palette.raised,
        troughcolor=palette.bg,
        bordercolor=palette.bg,
        arrowcolor=palette.muted,
    )
    style.configure(
        "Gis.Horizontal.TScrollbar",
        background=palette.raised,
        troughcolor=palette.bg,
        bordercolor=palette.bg,
        arrowcolor=palette.muted,
    )
    # Navigation is a rail, not a second strip of tabs. The notebook remains the
    # one authoritative page container (tests and callers select it directly),
    # but its tab chrome is removed so it cannot compete with the rail.
    try:
        # Element names are not style-prefixed: Notebook.client is the same
        # element the base "TNotebook" layout uses. A prefixed name is accepted
        # by layout() but draws nothing, which left the old tab strip visible.
        style.layout("Gis.TNotebook", [("Notebook.client", {"sticky": "nswe"})])
        style.configure("Gis.TNotebook", background=palette.bg, borderwidth=0, tabmargins=0)
        style.configure(
            "Gis.TNotebook.Tab",
            background=palette.surface,
            foreground=palette.muted,
            padding=(0, 0),
        )
        # The selected tab must remain selected to keep its pane mapped, but it
        # must not draw a duplicate label above the rail. Collapse the tab
        # element itself; the page remains mapped and programmatic selection
        # still reports the real tab title.
        style.layout("Gis.TNotebook.Tab", [])
        # Nested page tabs (Projects -> Planer / Visual Design / Integrity) are
        # local section navigation and stay visible, but use the same ink and
        # ember language instead of the light default theme.
        style.configure("Gis.SubTabs.TNotebook", background=palette.bg, borderwidth=0)
        style.configure(
            "Gis.SubTabs.TNotebook.Tab",
            background=palette.surface,
            foreground=palette.muted,
            padding=(13, 7),
            borderwidth=0,
            font=(fonts.ui, 9, "bold"),
        )
        style.map(
            "Gis.SubTabs.TNotebook.Tab",
            background=[("selected", palette.raised), ("active", palette.overlay)],
            foreground=[("selected", palette.text), ("active", palette.text)],
        )
    except tk.TclError:
        pass

    try:
        style.configure("TPanedwindow", background=palette.bg)
        style.configure("Sash", sashthickness=6, background=palette.bg)
    except tk.TclError:
        pass

    root._gis_palette = palette  # type: ignore[attr-defined]
    root._gis_fonts = fonts  # type: ignore[attr-defined]
    root._gis_appearance = palette.name  # type: ignore[attr-defined]
    return palette, fonts


def style_text(widget: tk.Text, palette: Palette, fonts: Fonts) -> None:
    """Apply the reading surface and conversation tags to a transcript."""
    widget.configure(
        background=palette.code_bg if widget.cget("height") else palette.surface,
        foreground=palette.text,
        insertbackground=palette.accent,
        selectbackground=palette.selection,
        selectforeground=palette.text,
        font=fonts.body,
        relief="flat",
        borderwidth=0,
        highlightthickness=1,
        highlightbackground=palette.border,
        highlightcolor=palette.accent,
        padx=14,
        pady=12,
        spacing1=2,
        spacing3=2,
    )
    widget.tag_configure(
        "agent",
        foreground=palette.text,
        lmargin1=0,
        lmargin2=0,
        spacing1=10,
        spacing3=8,
    )
    widget.tag_configure(
        "agent_name",
        foreground=palette.teal,
        font=(fonts.ui, 9, "bold"),
        spacing1=10,
        spacing3=2,
    )
    widget.tag_configure(
        "user",
        background=palette.user_bg,
        foreground=palette.text,
        lmargin1=10,
        lmargin2=10,
        rmargin=10,
        spacing1=8,
        spacing3=8,
    )
    widget.tag_configure(
        "user_name",
        foreground=palette.accent,
        font=(fonts.ui, 9, "bold"),
        lmargin1=10,
        spacing1=8,
        spacing3=2,
    )
    widget.tag_configure(
        "question",
        background=palette.question_bg,
        foreground=palette.text,
        font=(fonts.ui, 10, "bold"),
        lmargin1=8,
        lmargin2=8,
        rmargin=8,
        spacing1=8,
        spacing3=8,
        borderwidth=1,
        relief="solid",
    )
    widget.tag_configure(
        "question_name",
        background=palette.question_bg,
        foreground=palette.accent,
        font=(fonts.ui, 9, "bold"),
        lmargin1=8,
        spacing1=8,
    )
    widget.tag_configure(
        "report",
        background=palette.raised,
        foreground=palette.text,
        font=fonts.mono_body,
        lmargin1=12,
        lmargin2=12,
        rmargin=12,
        spacing1=8,
        spacing3=8,
    )
    widget.tag_configure("code", font=fonts.mono_body, background=palette.code_bg, foreground=palette.teal)
    widget.tag_configure("tool", foreground=palette.muted, font=(fonts.ui, 9, "italic"))
    widget.tag_configure("error", foreground=palette.danger)
    widget.tag_configure("success", foreground=palette.success)
    widget.tag_configure("muted", foreground=palette.faint, font=fonts.small)


def style_input(widget: tk.Text, palette: Palette, fonts: Fonts) -> None:
    widget.configure(
        background=palette.raised,
        foreground=palette.text,
        insertbackground=palette.accent,
        selectbackground=palette.selection,
        selectforeground=palette.text,
        font=fonts.body,
        relief="flat",
        borderwidth=0,
        highlightthickness=1,
        highlightbackground=palette.border,
        highlightcolor=palette.accent,
        padx=12,
        pady=10,
        spacing1=2,
        spacing3=2,
    )


#: The authentic Gis star path, taken point-for-point from
#: ``giscode/assets/gis_logo.svg`` (starPath, viewBox 30 30 240 240), which
#: was extracted from the original ``gis-logo.html`` artwork.
GIS_STAR_POINTS = (
    (150.0, 50.0), (179.29, 79.29), (220.71, 79.29), (220.71, 120.71),
    (250.0, 150.0), (220.71, 179.29), (220.71, 220.71), (179.29, 220.71),
    (150.0, 250.0), (120.71, 220.71), (79.29, 220.71), (79.29, 179.29),
    (50.0, 150.0), (79.29, 120.71), (79.29, 79.29), (120.71, 79.29),
)
#: The logo's own gradient endpoints, as shipped in gis_logo.svg. These are
#: brand colours and are deliberately not remapped to the UI palette.
GIS_BRAND_TEAL = (47, 212, 192)
GIS_BRAND_GOLD = (211, 167, 92)
GIS_BRAND_INK = "#0A0E13"


def _brand_gradient(t: float) -> str:
    """Teal-to-gold point on the logo's original gradient stroke."""
    t = min(1.0, max(0.0, t))
    r, g, b = (
        int(round(a + (c - a) * t))
        for a, c in zip(GIS_BRAND_TEAL, GIS_BRAND_GOLD)
    )
    return f"#{r:02x}{g:02x}{b:02x}"


def draw_mark(canvas: tk.Canvas, size: int, palette: Palette, x: int = 0, y: int = 0) -> list[int]:
    """Draw the authentic Gis star; returns canvas item ids.

    The geometry comes from the original logo artwork, not a redrawn
    approximation, and the stroke keeps the logo's teal-to-gold gradient.
    Used only as a fallback when the packaged PNG cannot be loaded.
    """
    pad = max(2, size // 10)
    scale = (size - 2 * pad) / 240.0
    points = [
        (x + pad + (px - 30.0) * scale, y + pad + (py - 30.0) * scale)
        for px, py in GIS_STAR_POINTS
    ]
    ids: list[int] = [
        canvas.create_polygon(points, fill=GIS_BRAND_INK, outline="")
    ]
    count = len(points)
    for index in range(count):
        first = points[index]
        second = points[(index + 1) % count]
        ids.append(
            canvas.create_line(
                first[0], first[1], second[0], second[1],
                fill=_brand_gradient(index / (count - 1)),
                width=max(2, size // 14),
                capstyle="round",
                joinstyle="round",
            )
        )
    return ids


ICON_NAMES = (
    "chat", "web", "projects", "models", "settings", "history", "status",
    "plus", "open", "stop", "send", "check", "alert", "info", "moon", "sun",
    "spark", "terminal",
)


class Glyph(tk.Canvas):
    """A small, theme-aware line icon drawn with real vector primitives."""

    def __init__(self, parent, name: str, palette: Palette, size: int = 18, background: str | None = None, **kwargs):
        super().__init__(
            parent,
            width=size,
            height=size,
            background=background or palette.surface,
            highlightthickness=0,
            borderwidth=0,
            **kwargs,
        )
        self.name = name
        self.size = size
        self.palette = palette
        self.draw()

    def set_palette(self, palette: Palette, background: str | None = None) -> None:
        self.palette = palette
        if background:
            self.configure(background=background)
        self.draw()

    def draw(self) -> None:
        self.delete("all")
        p = self.palette
        s = self.size
        w = max(1.4, s / 12)
        c = p.muted
        a = p.accent
        t = p.teal
        m = s / 2
        name = self.name
        if name == "chat":
            self.create_rectangle(3, 4, s - 3, s - 7, outline=c, width=w)
            self.create_line(7, s - 4, 10, s - 7, fill=c, width=w)
            self.create_line(7, 8, s - 7, 8, fill=c, width=w)
            self.create_line(7, 11, s - 9, 11, fill=c, width=w)
        elif name == "web":
            self.create_oval(3, 3, s - 3, s - 3, outline=c, width=w)
            self.create_line(3, m, s - 3, m, fill=c, width=w)
            self.create_line(m, 3, m, s - 3, fill=c, width=w)
            self.create_arc(6, 3, s - 6, s - 3, start=90, extent=180, style="arc", outline=c, width=w)
        elif name == "projects":
            self.create_line(3, 6, 8, 6, 10, 8, s - 3, 8, fill=c, width=w)
            self.create_rectangle(3, 8, s - 3, s - 3, outline=c, width=w)
            self.create_line(7, 12, s - 7, 12, fill=c, width=w)
        elif name == "models":
            self.create_rectangle(5, 5, s - 5, s - 5, outline=t, width=w)
            self.create_rectangle(8, 8, s - 8, s - 8, outline=t, width=w)
            for x1, y1, x2, y2 in ((m, 2, m, 5), (m, s - 5, m, s - 2), (2, m, 5, m), (s - 5, m, s - 2, m)):
                self.create_line(x1, y1, x2, y2, fill=t, width=w)
        elif name == "settings":
            for yy in (5, 9, 13):
                self.create_line(3, yy, s - 3, yy, fill=c, width=w)
            self.create_oval(5, 3, 9, 7, fill=p.surface, outline=a, width=w)
            self.create_oval(s - 10, 7, s - 6, 11, fill=p.surface, outline=a, width=w)
            self.create_oval(6, 11, 10, 15, fill=p.surface, outline=a, width=w)
        elif name == "history":
            self.create_arc(3, 3, s - 3, s - 3, start=45, extent=300, style="arc", outline=c, width=w)
            self.create_line(m, m, m, 6, fill=a, width=w)
            self.create_line(m, m, s - 6, m, fill=a, width=w)
        elif name == "status":
            self.create_line(2, 12, 6, 12, 8, 7, 11, 15, 13, 9, 16, 9, fill=t, width=w)
            self.create_oval(14, 7, 17, 10, fill=a, outline="")
        elif name == "plus":
            self.create_line(m, 4, m, s - 4, fill=a, width=w)
            self.create_line(4, m, s - 4, m, fill=a, width=w)
        elif name == "open":
            self.create_line(3, 6, 8, 6, 10, 8, s - 3, 8, fill=c, width=w)
            self.create_line(3, 8, 3, s - 3, s - 3, s - 3, fill=c, width=w)
            self.create_line(10, 12, 15, 7, fill=a, width=w)
            self.create_line(11, 7, 15, 7, 15, 11, fill=a, width=w)
        elif name == "stop":
            self.create_rectangle(5, 5, s - 5, s - 5, fill=a, outline="")
        elif name == "send":
            self.create_line(3, s - 4, s - 3, 4, fill=a, width=w)
            self.create_line(7, 5, s - 3, 4, s - 4, s - 8, fill=a, width=w)
        elif name == "check":
            self.create_line(3, 10, 7, 14, s - 3, 5, fill=p.success, width=w)
        elif name == "alert":
            self.create_polygon(m, 3, s - 3, s - 4, 3, s - 4, outline=p.danger, fill="", width=w)
            self.create_line(m, 7, m, 12, fill=p.danger, width=w)
            self.create_oval(m - 1, 13, m + 1, 15, fill=p.danger, outline="")
        elif name == "info":
            self.create_oval(3, 3, s - 3, s - 3, outline=p.info, width=w)
            self.create_line(m, 8, m, 13, fill=p.info, width=w)
            self.create_oval(m - 1, 5, m + 1, 7, fill=p.info, outline="")
        elif name == "moon":
            self.create_arc(4, 3, s - 3, s - 3, start=80, extent=230, style="arc", outline=c, width=w)
            self.create_arc(2, 5, s - 5, s - 1, start=120, extent=180, style="arc", outline=p.surface, width=w)
        elif name == "sun":
            self.create_oval(6, 6, s - 6, s - 6, outline=a, width=w)
            for x1, y1, x2, y2 in ((m, 2, m, 4), (m, s - 4, m, s - 2), (2, m, 4, m), (s - 4, m, s - 2, m)):
                self.create_line(x1, y1, x2, y2, fill=a, width=w)
        elif name == "spark":
            self.create_line(m, 3, m, s - 3, fill=a, width=w)
            self.create_line(3, m, s - 3, m, fill=a, width=w)
            self.create_line(6, 6, s - 6, s - 6, fill=t, width=max(1, w - 0.4))
            self.create_line(s - 6, 6, 6, s - 6, fill=t, width=max(1, w - 0.4))
        elif name == "terminal":
            self.create_rectangle(3, 4, s - 3, s - 4, outline=c, width=w)
            self.create_line(6, 8, 9, 11, 6, 14, fill=a, width=w)
            self.create_line(11, 14, s - 6, 14, fill=a, width=w)
        else:
            self.create_oval(5, 5, s - 5, s - 5, outline=c, width=w)
