"""The product must show the authentic Gis star, not a redesigned mark."""
from __future__ import annotations

import tkinter as tk

import pytest
from PIL import Image

from giscode.branding import logo
from giscode.gui import design_system as ds


def test_shipped_assets_are_the_authentic_star():
    assert logo.DEFAULT_LOGO_MARK == "gis_logo_small.png"
    assert logo.DEFAULT_LOGO_MARK_LARGE == "gis_logo_splash.png"
    assert logo.APP_ICON == "gis_logo.ico"
    status = logo.default_asset_status()
    assert status and all(status.values()), status


def test_star_artwork_keeps_its_original_geometry_and_colours():
    svg = logo.gis_logo_path(logo.LOGO_SVG).read_text(encoding="utf-8")
    assert "Official Gis logo" in svg
    assert "M150,50 L179.29,79.29" in svg
    assert "#2FD4C0" in svg and "#D3A75C" in svg


def test_icon_packages_windows_sizes():
    image = Image.open(logo.gis_logo_path(logo.APP_ICON))
    sizes = set(image.info.get("sizes", set()))
    assert {(16, 16), (24, 24), (32, 32), (48, 48)} <= sizes


def test_mark_tile_is_used_at_its_native_size():
    small = Image.open(logo.gis_logo_path(logo.DEFAULT_LOGO_MARK))
    splash = Image.open(logo.gis_logo_path(logo.DEFAULT_LOGO_MARK_LARGE))
    assert small.size == (48, 48)
    assert splash.size == (620, 620)


def test_draw_mark_uses_the_star_not_the_retired_ring():
    try:
        root = tk.Tk()
        root.withdraw()
    except tk.TclError:
        pytest.skip("Tk display unavailable")
    try:
        canvas = tk.Canvas(root, width=48, height=48)
        ids = ds.draw_mark(canvas, 48, ds.palette_for("dark"))
        assert ids
        kinds = [canvas.type(item) for item in ids]
        assert "polygon" in kinds
        assert "oval" not in kinds and "arc" not in kinds
        colours = {canvas.itemcget(item, "fill") for item in ids}
        assert ds.GIS_BRAND_INK in colours
        assert ds._brand_gradient(0.0) == "#2fd4c0"
        assert ds._brand_gradient(1.0) == "#d3a75c"
    finally:
        root.destroy()


def test_shell_and_about_show_the_authentic_artwork():
    try:
        root = tk.Tk()
        root.withdraw()
    except tk.TclError:
        pytest.skip("Tk display unavailable")
    from giscode.gui.main_window import MainWindow

    window = None
    try:
        window = MainWindow(root)
        root.update_idletasks()
        items = getattr(window._nav, "brand_mark_items", None)
        canvas = getattr(window._nav, "brand_mark_canvas", None)
        assert items and canvas is not None, "rail brand mark did not render"
        kinds = [canvas.type(item) for item in items]
        assert "polygon" in kinds
        assert "oval" not in kinds and "arc" not in kinds
        window._show_about()
        about = getattr(window, "_about_mark_photo", None)
        assert about is not None and about.width() >= 128
        assert about.width() == about.height()
    finally:
        if window is not None and getattr(window, "_task_manager", None) is not None:
            try:
                window._task_manager.cancel()
            except Exception:
                pass
        root.destroy()
