"""The new shell must be real navigation, not decoration."""
from __future__ import annotations

import tkinter as tk

from giscode.gui.main_window import MainWindow


def test_rail_clicks_select_the_real_page(tk_root):
    window = MainWindow(tk_root)
    tk_root.deiconify()
    tk_root.update()
    try:
        for name in ("Projects", "Settings", "Model Manager", "Chat"):
            item = window._nav.items[name]
            item.event_generate("<Button-1>")
            tk_root.update()
            selected = window._notebook.tab(window._notebook.select(), "text")
            assert selected == name
            assert item._selected is True
    finally:
        if window._task_manager is not None:
            window._task_manager.cancel()


def test_rail_is_complete_and_every_item_has_a_command(tk_root):
    window = MainWindow(tk_root)
    try:
        assert set(window._nav.items) == set(window._tab_frames)
        for name, item in window._nav.items.items():
            assert callable(item.command)
            assert item.label.cget("text") == name
    finally:
        if window._task_manager is not None:
            window._task_manager.cancel()


def test_attention_states_are_explicit_and_not_flashing(tk_root):
    window = MainWindow(tk_root)
    tk_root.deiconify()
    tk_root.update()
    try:
        assert window._attention_chip.winfo_ismapped()
        for state, fragment in (
            ("Working", "WORKING"),
            ("Completed", "COMPLETED"),
            ("Cancelled", "CANCELLED"),
            ("Timed out", "TIMED OUT"),
            ("Failed", "FAILED"),
        ):
            window._set_attention(state)
            assert fragment in window._attention_chip.cget("text")
        assert window._attention_strip.cget("height") == 3
    finally:
        if window._task_manager is not None:
            window._task_manager.cancel()
