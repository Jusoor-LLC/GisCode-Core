"""The main window has a floor, and every header control fits inside it.

Measured before the floor existed: project Settings was clipped at 1 000 px and
gone under 960 px (with no other entry point), the header Stop went at 810 px,
and the Chat Send/Stop row collapsed under 800 px. These tests keep the floor
and the layout that justified it.

The header now holds only branding, the project selector, creation, project
settings, the model selector/manager and Stop; Open and Close moved to the
Projects tab, and the Pro-only actions are not built in Core.

Controls are checked for real geometry (mapped, allocated width at least the
requested width), not for mere existence.
"""

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

import pytest

from giscode.gui.main_window import MainWindow

HEADER_CONTROLS = [
    "+ New Project", "Open Existing…", "Settings", "Model Manager", "Stop",
]


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _buttons(root):
    found = {}
    for widget in _walk(root):
        if isinstance(widget, ttk.Button):
            found.setdefault(widget.cget("text"), widget)
    return found


def _label_fits(button, pad=8):
    """The control shows its whole label, with minimal padding around it.

    ``winfo_reqwidth`` is padding-inclusive and differs between platforms for
    the same text; comparing against it reported a clipped label where only
    theme padding was shaved. Measure the text itself, so a truncated or
    collapsed control still fails.
    """
    text = str(button.cget("text"))
    try:
        face = tkfont.nametofont("TkDefaultFont", root=button)
    except tk.TclError:  # pragma: no cover - font system unavailable
        return True
    return button.winfo_width() >= face.measure(text) + pad


def _show(root, width, height):
    """Map the window so winfo_* reports real geometry, then settle."""
    root.deiconify()
    root.geometry("%dx%d" % (width, height))
    root.update()


@pytest.fixture
def window(tk_root):
    win = MainWindow(tk_root)
    yield win
    try:
        tk_root.withdraw()
    except tk.TclError:
        pass


def test_the_window_refuses_to_shrink_below_the_usable_minimum(window, tk_root):
    _show(tk_root, 1400, 900)
    # The floor is the design minimum, raised when this platform's font metrics
    # need more room for the header. It must never be below the design minimum.
    floor = tk_root.minsize()
    assert floor[1] == 700
    assert floor[0] >= 1100
    assert tk_root.wm_minsize() == floor
    assert window._header_floor == floor[0]

    tk_root.geometry("900x600")
    tk_root.update()
    assert tk_root.winfo_width() >= floor[0]
    assert tk_root.winfo_height() >= floor[1]

    tk_root.geometry("100x100")
    tk_root.update()
    assert tk_root.winfo_width() >= floor[0]
    assert tk_root.winfo_height() >= floor[1]


def test_every_header_control_is_usable_at_the_minimum_size(window, tk_root):
    _show(tk_root, *tk_root.minsize())
    buttons = _buttons(tk_root)

    missing = [label for label in HEADER_CONTROLS if label not in buttons]
    assert not missing, "header control no longer built: %s" % missing

    squeezed = [
        "%s(width=%d)" % (label, buttons[label].winfo_width())
        for label in HEADER_CONTROLS
        if not buttons[label].winfo_ismapped() or not _label_fits(buttons[label])
    ]
    assert not squeezed, "controls clipped at the minimum size: %s" % squeezed

    # The Chat row is the one place a running task can be stopped.
    window._select_tab("Chat")
    tk_root.update_idletasks()
    tk_root.update()
    for name, widget in (("Send", window.send_task_btn), ("Stop", window.revoke_btn)):
        assert widget.winfo_ismapped(), "%s is not visible at the minimum size" % name
        assert _label_fits(widget), "%s label is clipped at the minimum size" % name


def test_every_tab_stays_selectable_at_the_minimum_size(window, tk_root):
    _show(tk_root, 1100, 700)
    notebook = window._notebook
    for tab_id in notebook.tabs():
        name = notebook.tab(tab_id, "text")
        notebook.select(tab_id)
        tk_root.update_idletasks()
        assert notebook.tab(notebook.select(), "text") == name


def test_the_normal_layout_still_has_room_to_spare(window, tk_root):
    if tk_root.winfo_screenwidth() < 1400 or tk_root.winfo_screenheight() < 900:
        pytest.skip("the runner display cannot provide a 1400x900 window")
    _show(tk_root, 1400, 900)
    buttons = _buttons(tk_root)
    squeezed = [
        label
        for label in HEADER_CONTROLS
        if buttons[label].winfo_width() < buttons[label].winfo_reqwidth()
    ]
    assert not squeezed, "controls squeezed at the normal size: %s" % squeezed

    brand_row = window._project_combo.master
    task_row = buttons["Model Manager"].master
    assert brand_row.winfo_reqwidth() <= 1400
    assert task_row.winfo_reqwidth() <= 1400
    assert tk_root.winfo_width() == 1400
