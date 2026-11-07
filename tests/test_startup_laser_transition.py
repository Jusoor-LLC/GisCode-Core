"""Focused regression tests: Gis laser-beam startup transition.

Production change (ONLY giscode/gui/splash.py, giscode/gui/__init__.py and one
fast-path guard in giscode/inference/runtime.py):

  * ``launch_gui()`` now creates the Tk root and immediately WITHDRAWS it, then
    shows the animated :class:`GisLaserSplash` (logo -> laser beam scan ->
    morph). The real MainWindow is built while the root is hidden, and the root
    is only ``deiconify()``-ed when the splash's transition fires - so there is
    no "small blank window followed by a 2s freeze" startup sequence.
  * The laser splash is ``after()``-driven on the Tk thread (never blocks on
    model discovery/loading/inference), and the deferred model autoload on its
    background thread is untouched.
  * ``SubprocessLlamaCppRuntime.get_model_status`` gained a non-blocking
    fast-path (no HTTP when the owned llama-server subprocess is not running),
    which removes the ~2s blocking ``urlopen`` probe that used to stall the
    synchronous first poll during MainWindow construction.

These tests drive the real splash widgets on a real (withdrawn) Tk root:

  * A. the beam sweeps across the artwork frame by frame and then morphs; the
    transition callback is invoked exactly as the branding turns into the app.
  * B. ``launch_gui`` keeps the root withdrawn while the splash plays and only
    deiconifies (centered) once the transition fires; closing the root cleanly
    leaves no orphan after-widgets.
  * C. the runtime fast-path: a real ``SubprocessLlamaCppRuntime`` that has not
    started a server returns ``get_model_status() is None`` WITHOUT any network
    call, so the first synchronous poll cannot stall the GUI thread.
"""
from __future__ import annotations

import time

import pytest

from giscode.gui.splash import show_laser_splash

from giscode.inference.runtime import SubprocessLlamaCppRuntime


# ------------------------------------------------------------------ A. splash


def test_laser_splash_beam_sweeps_then_morphs_and_closes(tk_root):
    transitions = []
    splash = show_laser_splash(tk_root, on_transition=lambda: transitions.append(1))
    assert splash._photo is not None, "laser splash must load the Gis artwork"
    x0 = splash.beam_x
    for _ in range(400):
        if splash.closed:
            break
        splash.advance_frame()
    assert splash.beam_x > x0, "the laser beam must sweep across the artwork"
    assert transitions, "the transition callback must fire at morph time"
    assert splash.closed, "the splash must close after the morph fade"
    splash.close()
    assert splash.closed is True


def test_laser_splash_is_borderless_dark_canvas(tk_root):
    splash = show_laser_splash(tk_root)
    assert int(splash.window.overrideredirect()) == 1
    assert splash.window.cget("bg") == "#080B12"
    splash.close()


# ------------------------------------------------------------------ B. launch_gui


def test_launch_gui_root_withdrawn_then_revealed_on_transition(monkeypatch):
    import giscode.gui.splash as splash_mod

    # Speed the animation up so event pumping finishes in well under a second.
    monkeypatch.setattr(splash_mod.GisLaserSplash, "FRAME_MS", 1)
    monkeypatch.setattr(splash_mod.GisLaserSplash, "HOLD_AFTER_SCAN_MS", 1)
    monkeypatch.setattr(splash_mod.GisLaserSplash, "FADE_DELTA_MS", 1)

    captured = {}

    def _record(root, on_transition=None, hold_ms=None):
        s = splash_mod.GisLaserSplash(root, on_transition=on_transition, hold_ms=hold_ms)
        captured["splash"] = s
        return s

    monkeypatch.setattr(splash_mod, "show_laser_splash", _record)

    from giscode.gui import launch_gui

    root = launch_gui()
    try:
        assert root.state() == "withdrawn", (
            "the root must stay withdrawn until the transition fires"
        )
        assert "splash" in captured, "launch_gui must show the laser splash"

        deadline = time.time() + 6.0
        while time.time() < deadline:
            if captured["splash"].closed:
                break
            try:
                root.update()
            except Exception:
                break
            time.sleep(0.005)

        assert root.state() == "normal", (
            "the transition must deiconify the fully-built root"
        )
        assert captured["splash"].closed, "the splash closes after the morph"
    finally:
        root.destroy()


# ------------------------------------------------------------------ C. runtime fast-path


def test_subprocess_runtime_get_model_status_no_network_when_not_running(monkeypatch):
    called = []

    def _no_call(*args, **kwargs):
        called.append(1)
        raise AssertionError("network must not be touched when server is down")

    runtime = SubprocessLlamaCppRuntime()
    runtime._server_port = 57345
    runtime._process = None
    monkeypatch.setattr("giscode.inference.runtime.urllib.request.urlopen", _no_call)

    assert runtime.get_model_status() is None
    assert called == [], "down server must not trigger a blocking HTTP probe"

    class _Gone:
        def poll(self):
            return -1

    runtime._process = _Gone()
    assert runtime.get_model_status() is None
    assert called == [], "terminated server must not trigger a blocking HTTP probe"