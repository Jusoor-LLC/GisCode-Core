"""Ctrl+Tab is real navigation between the pages the edition built.

The shell has one notebook and a rail that mirrors it. These tests press the
real key bindings (synthetic Tk events, not direct method calls) so a missing
binding cannot pass, and they check the rail's highlight follows the page.
"""
from __future__ import annotations

import tkinter as tk

import pytest

from giscode.gui.main_window import MainWindow


@pytest.fixture
def window(tk_root):
    window = MainWindow(tk_root)
    tk_root.deiconify()
    tk_root.update()
    # A real window has focus; a withdrawn one does not, and Tk only
    # dispatches key bindings for the focused application.
    tk_root.focus_force()
    tk_root.update()
    try:
        yield window
    finally:
        if getattr(window, "_task_manager", None) is not None:
            window._task_manager.cancel()


def selected(window) -> str:
    return window._notebook.tab(window._notebook.select(), "text")


def press(root, sequence: str) -> None:
    # when="now" dispatches before update(); a queued synthetic key is not
    # delivered to the toplevel bindtag on every Tk build.
    root.event_generate(sequence, when="now")
    root.update()


def test_ctrl_tab_advances_and_ctrl_shift_tab_returns(window, tk_root):
    order = list(window._tab_frames)
    assert selected(window) == order[0]
    press(tk_root, "<Control-Tab>")
    assert selected(window) == order[1]
    press(tk_root, "<Control-Shift-Tab>")
    assert selected(window) == order[0]


def test_ctrl_tab_wraps_at_both_ends(window, tk_root):
    order = list(window._tab_frames)
    for _ in range(len(order) - 1):
        press(tk_root, "<Control-Tab>")
    assert selected(window) == order[-1]
    press(tk_root, "<Control-Tab>")
    assert selected(window) == order[0]
    press(tk_root, "<Control-Shift-Tab>")
    assert selected(window) == order[-1]


def test_the_rail_highlight_follows_the_keyboard(window, tk_root):
    press(tk_root, "<Control-Tab>")
    current = selected(window)
    assert window._nav._active == current
    assert window._nav.items[current]._selected is True


def test_the_existing_alt_shortcuts_still_work(window, tk_root):
    order = list(window._tab_frames)
    press(tk_root, "<Alt-Key-2>")
    assert selected(window) == order[1]
