"""Gis logo splash shown at application startup.

The intended startup sequence is::

    Gis Logo Splash -> GisCode main UI

Two splash styles are provided:

* :class:`GisSplash` - lightweight borderless :class:`tk.Toplevel` static
  branding (legacy behavior used by ``--splash-check`` and the branding
  regression suite).
* :class:`GisLaserSplash` - the animated startup: the Gis logo appears, a
  laser beam scans across the artwork revealing it, and the same branding
  then fades/morphs into the already-built GisCode interface via
  ``on_transition``.  All animation is driven by ``after()`` callbacks so the
  Tk UI thread never blocks on model discovery, model loading or inference.

If the logo asset is missing the splash degrades to text rather than failing
startup.
"""
from __future__ import annotations

import tkinter as tk

from giscode.branding.logo import DEFAULT_LOGO_SPLASH, gis_logo_path

DEFAULT_HOLD_MS = 1700
SPLASH_BG = "#080B12"

# Laser animation palette (kept inside the Gis dark palette).
BEAM_CORE = "#7AF4FF"
BEAM_INNER = "#2EC9E0"
BEAM_GLOW = "#0F4050"
BEAM_FADE = "#0B2A36"


class GisSplash:
    """A self-contained splash window that can be closed programmatically."""

    def __init__(self, root: tk.Tk, hold_ms: int = DEFAULT_HOLD_MS) -> None:
        self._root = root
        self._closed = False
        self._after_id = None
        self._photo = None

        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.configure(bg=SPLASH_BG, cursor="watch")
        self._build()
        self._center()
        try:
            self.window.lift()
        except Exception:
            pass
        try:
            self._after_id = self.window.after(hold_ms, self.close)
        except Exception:
            pass

    def _build(self) -> None:
        self._photo_error = None
        self._photo = None
        path = str(gis_logo_path(DEFAULT_LOGO_SPLASH))
        for _ in range(3):
            try:
                self._photo = tk.PhotoImage(file=path)
            except Exception as _exc:
                self._photo_error = repr(_exc)
                try:
                    self.window.update_idletasks()
                except Exception:
                    pass
                continue
            break
        frame = tk.Frame(self.window, bg=SPLASH_BG, padx=24, pady=24)
        frame.pack()
        if self._photo is not None:
            tk.Label(frame, image=self._photo, bg=SPLASH_BG, bd=0).pack()
        tk.Label(
            frame,
            text="GISCODE",
            bg=SPLASH_BG,
            fg="#EDEFF5",
            font=("Segoe UI", 14, "bold"),
        ).pack(pady=(18, 2))
        tk.Label(
            frame,
            text="AI-POWERED SOFTWARE SOLUTIONS",
            bg=SPLASH_BG,
            fg="#B8C0CC",
            font=("Segoe UI", 8),
        ).pack()

    def _center(self) -> None:
        try:
            self.window.update_idletasks()
            w = self.window.winfo_reqwidth()
            h = self.window.winfo_reqheight()
            x = max(0, (self._root.winfo_screenwidth() - w) // 2)
            y = max(0, (self._root.winfo_screenheight() - h) // 2)
            self.window.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def close(self) -> None:
        """Dismiss the splash. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        if self._after_id is not None:
            try:
                self.window.after_cancel(self._after_id)
            except Exception:
                pass
        try:
            self.window.destroy()
        except Exception:
            pass

    @property
    def closed(self) -> bool:
        return self._closed


def show_splash(root: tk.Tk, hold_ms: int = DEFAULT_HOLD_MS) -> GisSplash:
    """Create and return the Gis splash for ``root``."""
    return GisSplash(root, hold_ms=hold_ms)


class GisLaserSplash:
    """Animated startup branding: Gis logo -> laser beam scan -> GisCode UI.

    The splash is a borderless, canvas-drawn :class:`tk.Toplevel`. The Gis
    artwork is painted immediately, then a laser beam sweeps (left-to-right,
    twice) across the artwork, revealing it; finally the same branding morphs
    into the real GisCode interface: the :class:`GisSplash` window fades out
    while ``on_transition`` is invoked on the Tk thread so the caller can
    ``deiconify()`` the already-built main window at the identical screen
    position. There is never a second unrelated window; the main window is the
    destination of the transition.

    Everything is ``after()``-driven: no ``sleep``, no busy loop, no network,
    no model work on the Tk thread. Model autoload stays on the deferred
    background thread and the UI thread remains responsive while this plays.
    """

    # Movement timings (milliseconds).
    FRAME_MS = 16
    SCAN_PASSES = 2
    HOLD_AFTER_SCAN_MS = 220
    FADE_STEPS = 18
    FADE_DELTA_MS = 16

    def __init__(self, root: tk.Tk, on_transition=None, hold_ms: int = DEFAULT_HOLD_MS * 2) -> None:
        self._root = root
        self._on_transition = on_transition
        self._closed = False
        self._morphing = False
        self._after_id = None
        self._photo = None
        self._photo_error = None
        self.hold_ms = int(hold_ms) if hold_ms is not None else int(DEFAULT_HOLD_MS * 2)

        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.configure(bg=SPLASH_BG)
        try:
            self.window.attributes("-topmost", True)
        except Exception:
            pass
        self._build()
        self._center()
        try:
            self.window.lift()
        except Exception:
            pass
        try:
            self.window.update_idletasks()
        except Exception:
            pass

    # ------------------------------------------------------------------ ctor
    def _build(self) -> None:
        self._photo_error = None
        self._photo = None
        path = str(gis_logo_path(DEFAULT_LOGO_SPLASH))
        for _ in range(3):
            try:
                self._photo = tk.PhotoImage(file=path)
            except Exception as _exc:
                self._photo_error = repr(_exc)
                try:
                    self.window.update_idletasks()
                except Exception:
                    pass
                continue
            break

        self._canvas = tk.Canvas(self.window, bg=SPLASH_BG, highlightthickness=0, bd=0)
        self._canvas.pack(fill="both", expand=True)

        if self._photo is not None:
            px = self._photo.width()
            py = self._photo.height()
            cx = max(320, px // 2 + 40)
            cy = max(300, py // 2 + 30)
            self._logo_canvas_item = self._canvas.create_image(cx, cy, image=self._photo, anchor="center")
            # Article region (the box the laser sweeps across).
            self._art_x0 = cx - px // 2
            self._art_x1 = cx + px // 2
            self._art_y0 = cy - py // 2
            self._art_y1 = cy + py // 2
        else:
            self._logo_canvas_item = None
            self._art_x0 = 60
            self._art_x1 = 560
            self._art_y0 = 80
            self._art_y1 = 400

        # Reveal overlay: covers everything to the right of the beam so the
        # artwork appears progressively as the laser sweeps left -> right.
        self._reveal_item = self._canvas.create_rectangle(
            self._art_x0, self._art_y0, self._art_x1, self._art_y1,
            fill=SPLASH_BG, outline="",
        )

        # Laser beam: a leading bright core + layered glow to its left.
        beam_y0 = self._art_y0 - 6
        beam_y1 = self._art_y1 + 6
        self._beam_core = self._canvas.create_line(
            self._art_x0, beam_y0, self._art_x0, beam_y1,
            fill=BEAM_CORE, width=2,
        )
        self._beam_inner = self._canvas.create_line(
            self._art_x0 - 4, beam_y0, self._art_x0 - 4, beam_y1,
            fill=BEAM_INNER, width=3,
        )
        self._beam_glow = self._canvas.create_line(
            self._art_x0 - 14, beam_y0, self._art_x0 - 14, beam_y1,
            fill=BEAM_GLOW, width=7,
        )
        self._beam_fade = self._canvas.create_line(
            self._art_x0 - 30, beam_y0, self._art_x0 - 30, beam_y1,
            fill=BEAM_FADE, width=12,
        )

        # Branding text under the artwork.
        tk.Label(
            self.window,
            text="GISCODE",
            bg=SPLASH_BG,
            fg="#EDEFF5",
            font=("Segoe UI", 14, "bold"),
        ).pack(pady=(0, 0))
        tk.Label(
            self.window,
            text="AI-POWERED SOFTWARE SOLUTIONS",
            bg=SPLASH_BG,
            fg="#B8C0CC",
            font=("Segoe UI", 8),
        ).pack()

        self._beam_x = float(self._art_x0)
        self._pass = 0
        self._fade_step = 0
        self._after_id = None

    def _center(self) -> None:
        try:
            self.window.update_idletasks()
            w = self.window.winfo_reqwidth()
            h = self.window.winfo_reqheight()
            x = max(0, (self._root.winfo_screenwidth() - w) // 2)
            y = max(0, (self._root.winfo_screenheight() - h) // 2)
            self.window.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    # ------------------------------------------------------------- motion API
    def start(self) -> None:
        """Begin the laser scan animation. Safe to call once."""
        if self._after_id is not None:
            return
        self._after_id = self.window.after(0, self._tick)

    def _tick(self) -> None:
        if self._closed:
            return
        if self._morphing:
            self._morph_step()
            return
        self._scan_step()
        if not self._morphing:
            self._after_id = self.window.after(self.FRAME_MS, self._tick)

    def _sweep_distance(self) -> int:
        return max(1, int(self._art_x1 - self._art_x0))

    def _scan_step(self) -> None:
        step = max(1, self._sweep_distance() // 40)
        self._beam_x += float(step)
        if self._beam_x >= self._art_x1:
            self._pass += 1
            if self._pass >= self.SCAN_PASSES:
                self._enter_morph()
                return
            self._beam_x = float(self._art_x0)
        self._paint_scan()

    def _paint_scan(self) -> None:
        try:
            # Reveal everything left of the beam.
            if self._reveal_item is not None:
                self._canvas.coords(
                    self._reveal_item,
                    self._beam_x, self._art_y0, self._art_x1, self._art_y1,
                )
            for item, dy in (
                (self._beam_core, 0),
                (self._beam_inner, 4),
                (self._beam_glow, 14),
                (self._beam_fade, 30),
            ):
                if item is None:
                    continue
                bx = self._beam_x - dy
                self._canvas.coords(item, bx, self._art_y0 - 6, bx, self._art_y1 + 6)
        except Exception:
            pass

    def _enter_morph(self) -> None:
        """Fade the branding out over the real interface and call on_transition."""
        if self._morphing or self._closed:
            return
        self._morphing = True
        self._fade_step = 0
        # Freeze the artwork fully revealed for the morph.
        try:
            if self._reveal_item is not None:
                self._canvas.coords(self._reveal_item, self._art_x1, self._art_y0, self._art_x1, self._art_y1)
            for item in (self._beam_core, self._beam_inner, self._beam_glow, self._beam_fade):
                if item is not None:
                    self._canvas.itemconfig(item, state="hidden")
        except Exception:
            pass
        # Transition starts exactly here: the real interface is revealed at the
        # identical screen position and the branding fades out over it.
        if self._on_transition is not None:
            try:
                self._on_transition()
            except Exception:
                pass
        self._after_id = self.window.after(self.HOLD_AFTER_SCAN_MS, self._tick)

    def _morph_step(self) -> None:
        self._fade_step += 1
        try:
            alpha = max(0.0, 1.0 - self._fade_step / float(self.FADE_STEPS))
            self.window.attributes("-alpha", alpha)
        except Exception:
            alpha = 1.0
        if self._fade_step >= self.FADE_STEPS or alpha <= 0.0:
            self.close()
            return
        self._after_id = self.window.after(self.FADE_DELTA_MS, self._tick)

    # ----------------------------------------------------------------- close
    def close(self) -> None:
        """Dismiss the splash. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        if self._after_id is not None:
            try:
                self.window.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        try:
            self.window.attributes("-alpha", 1.0)
        except Exception:
            pass
        try:
            self.window.destroy()
        except Exception:
            pass

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def morphing(self) -> bool:
        return self._morphing

    @property
    def beam_x(self) -> float:
        return self._beam_x

    # Test hook: advance exactly one animation step synchronously.
    def advance_frame(self) -> None:
        if self._closed:
            return
        if self._morphing:
            self._morph_step()
        else:
            self._scan_step()


from giscode.gui.launch_sequence import GisLaunchSequence

# Keep the existing module-level seam so startup tests and integrators can
# replace the implementation without changing launch_gui().
GisLaserSplash = GisLaunchSequence


def show_laser_splash(root: tk.Tk, on_transition=None, hold_ms: int = DEFAULT_HOLD_MS * 2) -> GisLaserSplash:
    """Create and return the animated Gis laser splash for ``root``."""
    return GisLaserSplash(root, on_transition=on_transition, hold_ms=hold_ms)


def run_splash_check() -> int:
    """Packaged verify hook: render the splash from the real asset and quit."""
    try:
        root = tk.Tk()
        splash = show_splash(root, hold_ms=800)
        root.update_idletasks()
        print("SPLASH_CHECK_WINDOW:", splash.window.winfo_exists() == 1)
        print("SPLASH_CHECK_PHOTO:", splash._photo is not None)
        print("SPLASH_CHECK_BG:", splash.window.cget("bg"))
        print("SPLASH_CHECK_OK")
        root.after(900, root.quit)
        root.mainloop()
        splash.close()
        try:
            root.destroy()
        except Exception:
            pass
    except Exception as exc:  # pragma: no cover - fails loudly on purpose
        print(f"SPLASH_CHECK_FAIL: {type(exc).__name__}: {exc}")
        return 30
    return 0
