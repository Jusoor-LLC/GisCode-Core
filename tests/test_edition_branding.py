"""Edition identity and GisAI truth must be visible, distinct and accurate."""
from __future__ import annotations

import tkinter as tk

import pytest

from giscode import edition
from giscode.gui.main_window import MainWindow


class _FakeEngine:
    """Minimal GisAI engine surface used by the header poller."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def set_edit_approval_callback(self, _callback) -> None:
        return None

    def get_active_task(self):
        return None


def _close(window) -> None:
    manager = getattr(window, "_task_manager", None)
    if manager is not None:
        try:
            manager.cancel()
        except Exception:
            pass


def test_edition_badge_and_identity_match_the_build(tk_root):
    window = MainWindow(tk_root)
    try:
        expected = edition.edition().upper()
        assert window._identity_eyebrow.cget("text") == f"{expected} EDITION"
        if edition.is_pro():
            assert window._edition_badge is not None
            assert window._edition_badge.cget("text").strip() == "PRO"
            assert window._nav.edition_badge is not None
            assert window._nav.edition_badge.cget("text").strip() == "PRO"
        else:
            assert window._edition_badge is None
            assert window._nav.edition_badge is None
            assert expected == "CORE"
    finally:
        _close(window)


def test_gisai_never_claims_powered_without_an_engine(tk_root):
    window = MainWindow(tk_root)
    try:
        assert window._gisai_engine is None
        assert window._gisai_powered is False
        text = window._gisai_header_label.cget("text")
        assert "Powered" not in text
        assert "Unavailable" in text
    finally:
        _close(window)


def test_gisai_powered_is_pro_only_and_disabled_is_not_powered(tk_root):
    engine = _FakeEngine(enabled=True)
    window = MainWindow(tk_root, gisai_engine=engine)
    try:
        window._poll_gisai_state()
        text = window._gisai_header_label.cget("text")
        if edition.is_pro():
            assert window._gisai_powered is True
            assert text == "GisAI Powered"
        else:
            assert "Powered" not in text
            assert text.startswith("GisAI · ")

        engine.enabled = False
        window._set_gisai_indicator("Ready")
        assert window._gisai_powered is False
        assert "Powered" not in window._gisai_header_label.cget("text")
        assert "Disabled" in window._gisai_header_label.cget("text")
    finally:
        _close(window)


def test_branding_stays_visible_across_every_tab(tk_root):
    window = MainWindow(tk_root)
    tk_root.deiconify()
    tk_root.update()
    try:
        for name in window._tab_frames:
            window._select_tab(name)
            tk_root.update()
            assert window._gisai_header_label.winfo_ismapped()
            if edition.is_pro():
                assert window._edition_badge.winfo_ismapped()
    finally:
        _close(window)


def _texts(widget) -> list[str]:
    values: list[str] = []
    try:
        values.append(str(widget.cget("text")))
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        values.extend(_texts(child))
    return values


def test_every_edition_tab_has_a_real_builder(tk_root):
    """A missing dependency must not silently become a placeholder tab."""
    window = MainWindow(tk_root)
    try:
        for name, frame in window._tab_frames.items():
            assert frame.winfo_children(), f"{name} has no content"
            combined = "\n".join(_texts(frame))
            assert "is not included in this build" not in combined, name
    finally:
        _close(window)


def test_compact_window_keeps_primary_controls_reachable(tk_root):
    window = MainWindow(tk_root)
    tk_root.geometry("1100x700")
    tk_root.deiconify()
    window._select_tab("Chat")
    tk_root.update()
    try:
        assert window.send_task_btn.winfo_ismapped()
        assert window.revoke_btn.winfo_ismapped()
        assert window.task_entry.winfo_height() > 20
        if edition.is_pro():
            assert window._nav._nav_scrollbar.winfo_ismapped()
    finally:
        _close(window)


@pytest.mark.skipif(not edition.is_pro(), reason="rail overflow is a Pro requirement")
def test_pro_rail_exposes_all_seventeen_tabs_and_scrolls(tk_root):
    window = MainWindow(tk_root)
    tk_root.deiconify()
    tk_root.update()
    try:
        assert len(window._nav.items) == 17
        assert set(window._nav.items) == set(window._tab_frames)
        bbox = window._nav._nav_canvas.bbox("all")
        assert bbox is not None
        if (bbox[3] - bbox[1]) > window._nav._nav_canvas.winfo_height():
            assert window._nav._nav_scrollbar.winfo_ismapped()
            window._nav._nav_canvas.yview_moveto(1.0)
            tk_root.update()
            assert window._nav._nav_canvas.yview()[1] > 0.9
    finally:
        _close(window)
