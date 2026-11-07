"""Task 21 - Gis brand logo, splash, top-bar logo and per-project custom logo.

Covers:
* default Gis logo asset availability / packaged-resource resolution mode
* splash integration (window creation + artwork photo + clean close)
* custom logo HTML validation (valid, empty, binary, oversized, non-logo)
* custom logo selection/storage + cancellation + honest invalid-file error
* per-project branding isolation (no cross-project leaks, new project default)
"""
from __future__ import annotations

import giscode.gui.main_window as mw_module
import pytest
from PIL import Image

from giscode.branding import (
    DEFAULT_ASSETS,
    DEFAULT_LOGO_SMALL,
    LogoValidationError,
    default_asset_status,
    gis_logo_path,
    validate_logo_html,
)
from giscode.gui.main_window import MainWindow
from giscode.projects.context import ProjectContext, ProjectContextStore

VALID_SVG = "<svg xmlns='http://www.w3.org/2000/svg' width='64' height='64'><rect width='64' height='64' fill='#2FD4C0'/></svg>"
VALID_HTML = "<html><head><title>MyApp Logo</title></head><body><img src='logo.png' alt='MyApp'/></body></html>"


# ------------------------------------------------------------------ assets


def test_default_gis_logo_assets_are_available():
    status = default_asset_status()
    assert all(status.values()), status


def test_source_launch_resolution_points_to_real_packaged_files():
    for name in DEFAULT_ASSETS:
        path = gis_logo_path(name)
        assert path.is_file()
        assert path.read_bytes()[:8] != b""


def test_small_logo_tile_is_rounded_and_opaque_inner():
    img = Image.open(str(gis_logo_path(DEFAULT_LOGO_SMALL)))
    assert img.size == (48, 48)
    assert img.getpixel((0, 0))[3] == 0, "corner must be transparent"
    assert img.getpixel((3, 24))[3] == 255, "tile body must be opaque"


def test_splash_png_is_branded_dark_artwork():
    path = gis_logo_path("gis_logo_splash.png")
    img = Image.open(str(path))
    assert img.size[0] >= 300
    px = img.convert("RGBA").getpixel((img.size[0] // 2, img.size[1] // 2))
    assert px[3] == 255, "splash artwork must be opaque"


# ------------------------------------------------------------------ splash


def test_splash_creates_window_with_gis_artwork(tk_root):
    from giscode.gui.splash import GisSplash

    splash = GisSplash(tk_root, hold_ms=20000)
    assert splash.window.winfo_exists() == 1
    assert splash._photo is not None, "splash must load the Gis artwork"
    assert splash.window.cget("bg") == "#080B12"
    splash.close()
    assert splash.closed is True


def test_splash_photo_references_packaged_default_asset(tk_root):
    from giscode.gui.splash import GisSplash

    splash = GisSplash(tk_root, hold_ms=20000)
    assert splash._photo.width() > 100 and splash._photo.height() > 100
    splash.close()


# ------------------------------------------------------------------ validation


def test_valid_svg_logo_accepted():
    assert validate_logo_html(VALID_SVG) == VALID_SVG


def test_valid_html_logo_with_img_accepted():
    assert validate_logo_html(VALID_HTML) == VALID_HTML


def test_empty_logo_rejected_honestly():
    with pytest.raises(LogoValidationError) as exc:
        validate_logo_html("   \n  ")
    assert "empty" in str(exc.value)


def test_binary_file_rejected_honestly():
    with pytest.raises(LogoValidationError) as exc:
        validate_logo_html("PK\x03\x04\x00\x00<bogus>")
    assert "binary" in str(exc.value)


def test_non_logo_markup_rejected_honestly():
    with pytest.raises(LogoValidationError) as exc:
        validate_logo_html("<p>just some plain text, no logo here</p>")
    assert "does not look like a logo" in str(exc.value)


def test_oversized_logo_rejected_honestly():
    big = "<svg>" + "x" * (256 * 1024)
    with pytest.raises(LogoValidationError) as exc:
        validate_logo_html(big)
    assert "at most" in str(exc.value)


# ------------------------------------------------------------------ storage / isolation


def test_custom_logo_round_trip_via_project_context_store(tmp_path):
    store = ProjectContextStore(tmp_path)
    ctx = ProjectContext.create(root_path=str(tmp_path / "ProjectX"))
    store.save_custom_logo(ctx.project_id, VALID_HTML)
    loaded = store.load_all()[ctx.project_id]
    assert loaded.custom_logo == VALID_HTML
    assert store.get_custom_logo(ctx.project_id) == VALID_HTML


def test_custom_logo_does_not_leak_between_projects(tmp_path):
    store = ProjectContextStore(tmp_path)
    a = ProjectContext.create(root_path=str(tmp_path / "ProjectA"))
    b = ProjectContext.create(root_path=str(tmp_path / "ProjectB"))
    store.save_custom_logo(a.project_id, VALID_HTML)
    assert store.get_custom_logo(a.project_id) == VALID_HTML
    assert store.get_custom_logo(b.project_id) is None


def test_new_project_defaults_to_gis_branding(tmp_path):
    store = ProjectContextStore(tmp_path)
    fresh = ProjectContext.create(root_path=str(tmp_path / "ProjectC"))
    assert not (fresh.custom_logo or "")
    assert store.get_custom_logo(fresh.project_id) is None


def test_custom_logo_remains_isolated_after_context_save(tmp_path):
    store = ProjectContextStore(tmp_path)
    a = ProjectContext.create(root_path=str(tmp_path / "ProjectA"))
    a.custom_logo = VALID_SVG
    a.enter_section("Backend")
    store.save(a)
    b = ProjectContext.create(root_path=str(tmp_path / "ProjectB"))
    store.save(b)
    restored_a = store.load_all()[a.project_id]
    restored_b = store.load_all()[b.project_id]
    assert restored_a.custom_logo == VALID_SVG
    assert restored_b.custom_logo == ""


# ------------------------------------------------------------------ GUI integration


def _window(tk_root, tmp_path):
    mw = MainWindow(tk_root)
    mw._ctx_store = ProjectContextStore(tmp_path)
    return mw


def test_top_bar_shows_gis_logo_and_custom_logo_control(tk_root, tmp_path, monkeypatch):
    # Custom Logo is a Pro control; Core keeps the official GisCode logo only.
    monkeypatch.setenv("GISCODE_EDITION", "pro")
    mw = _window(tk_root, tmp_path)
    assert mw._gis_logo_photo is not None, "top bar must render the Gis logo"
    assert mw.custom_logo_btn is not None, "Custom Logo control must exist"
    assert mw.branding_var.get() == "Gis branding (default)"


def test_core_top_bar_keeps_the_logo_and_drops_the_custom_logo_control(tk_root, tmp_path):
    mw = _window(tk_root, tmp_path)
    assert mw._gis_logo_photo is not None, "top bar must render the Gis logo"
    assert mw.custom_logo_btn is None, "Core must not expose the Pro branding control"


def test_cancel_custom_logo_picker_changes_nothing(tk_root, tmp_path, monkeypatch):
    mw = _window(tk_root, tmp_path)
    ctx = ProjectContext.create(root_path=str(tmp_path / "AppZ"))
    mw._active_project_context = ctx
    monkeypatch.setattr(mw_module.filedialog, "askopenfilename", lambda **kw: "")
    mw._choose_custom_logo()
    assert not (ctx.custom_logo or "")
    assert mw._ctx_store.get_custom_logo(ctx.project_id) is None
    assert mw.branding_var.get() == "Gis branding (default)"


def test_invalid_logo_file_shows_honest_error_and_stores_nothing(
    tk_root, tmp_path, monkeypatch
):
    bad = tmp_path / "not_a_logo.txt"
    bad.write_text("hello world, no markup", encoding="utf-8")
    mw = _window(tk_root, tmp_path)
    ctx = ProjectContext.create(root_path=str(tmp_path / "AppY"))
    mw._active_project_context = ctx
    errors = []
    monkeypatch.setattr(mw_module.filedialog, "askopenfilename", lambda **kw: str(bad))
    monkeypatch.setattr(
        mw_module.messagebox,
        "showerror",
        lambda title, msg: errors.append((title, msg)),
    )
    mw._choose_custom_logo()
    assert errors, "invalid file must produce a user-facing error"
    assert "not usable" in errors[0][0] or "not usable" in errors[0][1]
    assert mw._ctx_store.get_custom_logo(ctx.project_id) is None


def test_valid_logo_file_is_accepted_and_stored_per_project(
    tk_root, tmp_path, monkeypatch
):
    good = tmp_path / "logo.html"
    good.write_text(VALID_HTML, encoding="utf-8")
    mw = _window(tk_root, tmp_path)
    ctx = ProjectContext.create(root_path=str(tmp_path / "AppX"))
    mw._active_project_context = ctx
    monkeypatch.setattr(mw_module.filedialog, "askopenfilename", lambda **kw: str(good))
    mw._choose_custom_logo()
    assert mw._ctx_store.get_custom_logo(ctx.project_id) == VALID_HTML
    assert ctx.custom_logo == VALID_HTML
    assert mw.branding_var.get() == "Custom logo"


def test_project_switch_loads_its_own_logo_and_others_stay_default(
    tk_root, tmp_path, monkeypatch
):
    store = ProjectContextStore(tmp_path)
    mw = _window(tk_root, tmp_path)
    proj_a = ProjectContext.create(root_path=str(tmp_path / "ProjA"))
    proj_b = ProjectContext.create(root_path=str(tmp_path / "ProjB"))
    store.save_custom_logo(proj_a.project_id, VALID_SVG)

    with monkeypatch.context() as mp:
        mp.setattr(mw_module.filedialog, "askdirectory", lambda **kw: proj_a.root_path)
        mw._open_project()
    assert mw._active_project_context is not None
    assert mw._active_project_context.custom_logo == VALID_SVG
    assert mw.branding_var.get() == "Custom logo"

    with monkeypatch.context() as mp:
        mp.setattr(mw_module.filedialog, "askdirectory", lambda **kw: proj_b.root_path)
        mw._open_project()
    assert mw._active_project_context.custom_logo == ""
    assert mw.branding_var.get() == "Gis branding (default)"