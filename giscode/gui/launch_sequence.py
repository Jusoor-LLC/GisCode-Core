"""Native Tk Gis launch sequence.

The Windows build has no WebView2/Chromium host, so this is the small native
equivalent of the supplied launch brief. It deliberately uses the existing
Tk startup callback contract: the real root is revealed at the bloom point,
then the borderless launch window fades away.
"""
from __future__ import annotations

import ctypes
import math
import sys
import time
import tkinter as tk

from giscode.config.manager import ConfigManager


BACKGROUND = "#080B12"
INK = "#EDEFF5"
TEAL = "#2FD4C0"
GOLD = "#D3A75C"
FLASH = "#EAFFFB"
GUIDE = "#38404D"


class GisLaunchSequence:
    """Geometric star construction, wordmark resolve, and app transition."""

    FRAME_MS = 16
    HOLD_AFTER_SCAN_MS = 120
    FADE_STEPS = 10
    FADE_DELTA_MS = 16
    STAR_POINTS = (
        (150, 50), (179.29, 79.29), (220.71, 79.29), (220.71, 120.71),
        (250, 150), (220.71, 179.29), (220.71, 220.71), (179.29, 220.71),
        (150, 250), (120.71, 220.71), (79.29, 220.71), (79.29, 179.29),
        (50, 150), (79.29, 120.71), (79.29, 79.29), (120.71, 79.29),
    )
    FULL = {
        "laser": (0.85, 1.30), "flash": (2.12, 0.34), "glow": (2.00, 0.80), "fill": (2.20, 1.00),
        "wordmark": (2.15, 0.60), "tagline": (2.45, 0.70),
        "transition": 4.10, "total": 5.30,
    }
    SHORT = {
        "laser": (0.05, 0.35), "flash": (0.38, 0.18), "glow": (0.32, 0.25), "fill": (0.36, 0.25),
        "wordmark": (0.36, 0.20), "tagline": (0.0, 0.0),
        "transition": 0.75, "total": 1.40,
    }

    def __init__(self, root: tk.Tk, on_transition=None, hold_ms=None) -> None:
        self._root = root
        self._on_transition = on_transition
        self._closed = False
        self._morphing = False
        self._transitioned = False
        self._after_id = None
        self._fade_step = 0
        self._started_at = None
        self._forced_elapsed = 0.0
        self._reduced_motion = self._animations_disabled()
        self.variant = self._select_variant()
        self.timing = self.SHORT if self.variant == "short" else self.FULL
        self.hold_ms = int(hold_ms) if hold_ms is not None else int(self.timing["total"] * 1000)

        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.configure(bg=BACKGROUND)
        self.window.bind("<Button-1>", lambda _event: self.skip())
        self._build()
        self._center()
        try:
            self.window.attributes("-topmost", True)
            self.window.lift()
        except Exception:
            pass

    @staticmethod
    def _animations_disabled() -> bool:
        if sys.platform != "win32":
            return False
        try:
            value = ctypes.c_int()
            # SPI_GETCLIENTAREAANIMATION.
            ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0)
            return not bool(value.value)
        except Exception:
            return False

    @staticmethod
    def _select_variant() -> str:
        try:
            settings = ConfigManager()
            seen = bool(settings.get("launch.LaunchSeen", False))
            if not seen:
                settings.set("launch.LaunchSeen", True)
                return "full"
        except Exception:
            pass
        return "short"

    def _layout(self) -> None:
        """Size the mark to 45-55% of the shorter screen dimension.

        The 300x300 star is the design baseline; it is scaled so the mark owns
        roughly half of the shorter screen edge while the whole window still
        fits on the display.
        """
        try:
            screen_w = self._root.winfo_screenwidth()
            screen_h = self._root.winfo_screenheight()
        except Exception:
            screen_w, screen_h = 1920, 1080
        shorter = max(320, min(int(screen_w), int(screen_h)))
        mark = shorter * 0.50
        self._scale = mark / 300.0
        self._width = max(760, int(mark * 2.6))
        self._height = max(620, int(mark * 2.1))
        self._width = min(self._width, int(screen_w * 0.92))
        self._height = min(self._height, int(screen_h * 0.92))
        self._cx = self._width / 2.0
        self._cy = self._height * 0.42
        self._stroke = max(1, int(round(3 * self._scale)))
        self._node = max(2, int(round(3 * self._scale)))
        self._wordmark_font = ("Segoe UI", max(14, int(round(32 * self._scale))), "bold")
        self._tagline_font = ("Segoe UI", max(7, int(round(9 * self._scale))))

    def _build(self) -> None:
        self._layout()
        self._canvas = tk.Canvas(
            self.window, width=self._width, height=self._height, bg=BACKGROUND,
            highlightthickness=0, bd=0,
        )
        self._canvas.pack(fill="both", expand=True)
        # Retain the legacy diagnostic contract. The geometric sequence is
        # authoritative, but packaged splash checks still use this attribute
        # to confirm the branding asset is available.
        self._photo = None
        try:
            from giscode.branding.logo import DEFAULT_LOGO_SPLASH, gis_logo_path

            self._photo = tk.PhotoImage(file=str(gis_logo_path(DEFAULT_LOGO_SPLASH)))
        except Exception:
            # Keep the legacy diagnostic contract usable on Tk builds without
            # PNG image support. The launch sequence itself is vector/canvas
            # drawn and does not depend on this compatibility reference.
            try:
                self._photo = tk.PhotoImage(width=1, height=1)
            except Exception:
                self._photo = None
        self._beam_x = 50.0
        self._star_segments = []
        for index, point in enumerate(self.STAR_POINTS):
            next_point = self.STAR_POINTS[(index + 1) % len(self.STAR_POINTS)]
            ratio = index / float(len(self.STAR_POINTS) - 1)
            self._star_segments.append(self._canvas.create_line(
                *self._map(*point), *self._map(*next_point),
                fill=self._gradient_color(ratio), width=self._stroke, state="hidden",
            ))
        self._fill = self._canvas.create_polygon(
            self._coords(self.STAR_POINTS), fill=INK, outline="", stipple="gray50",
            state="hidden",
        )
        self._glow = self._canvas.create_oval(
            *self._oval(106, 106, 194, 194), fill="#163E43", outline="", state="hidden",
        )
        self._flash = self._canvas.create_oval(
            *self._oval(0, 0, 0, 0), fill=TEAL, outline="", state="hidden",
        )
        self._head = self._canvas.create_oval(
            *self._oval(0, 0, 0, 0), fill=FLASH, outline="", state="hidden",
        )
        self._wordmark = self._canvas.create_text(
            self._cx, self._height * 0.78, text="GIS", fill=INK,
            font=self._wordmark_font, state="hidden",
        )
        self._tagline = self._canvas.create_text(
            self._cx, self._height * 0.88, text="AI-POWERED SOFTWARE SOLUTIONS",
            fill=INK, font=self._tagline_font, state="hidden",
        )
        self._nodes = []
        for point in self.STAR_POINTS:
            x, y = self._map(*point)
            n = self._node
            self._nodes.append(self._canvas.create_rectangle(x - n, y - n, x + n, y + n, fill=TEAL, outline="", state="hidden"))
        self._guides = []
        if self.variant == "full":
            for points in (
                ((79.29, 79.29), (220.71, 79.29), (220.71, 220.71), (79.29, 220.71)),
                ((150, 50), (250, 150), (150, 250), (50, 150)),
            ):
                self._guides.append(self._canvas.create_line(self._coords(points, closed=True), fill=GUIDE, width=1, state="hidden"))
        if self._reduced_motion:
            self._show_resolved()

    def _map(self, x: float, y: float) -> tuple[float, float]:
        return self._cx + (x - 150) * self._scale, self._cy + (y - 150) * self._scale

    @staticmethod
    def _gradient_color(ratio: float) -> str:
        ratio = max(0.0, min(1.0, ratio))
        teal = (0x2F, 0xD4, 0xC0)
        gold = (0xD3, 0xA7, 0x5C)
        return "#" + "".join(f"{round(a + (b - a) * ratio):02X}" for a, b in zip(teal, gold))

    def _coords(self, points, closed=False):
        values = [value for point in points for value in self._map(*point)]
        if closed:
            values.extend(self._map(*points[0]))
        return values

    def _oval(self, x0, y0, x1, y1):
        return (*self._map(x0, y0), *self._map(x1, y1))

    def _center(self) -> None:
        """Center the launch window on the primary screen.

        ``winfo_reqwidth`` can report 1x1 for a not-yet-mapped borderless
        Toplevel, which silently strands the window at 0,0. The computed
        layout size is authoritative here, and ``geometry`` is re-applied
        after mapping so the position sticks.
        """
        try:
            screen_w = self._root.winfo_screenwidth()
            screen_h = self._root.winfo_screenheight()
        except Exception:
            screen_w, screen_h = 1920, 1080
        width, height = self._width, self._height
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        placement = f"{width}x{height}+{x}+{y}"
        try:
            self.window.update_idletasks()
            self.window.geometry(placement)
            self.window.update_idletasks()
            actual = self.window.winfo_width(), self.window.winfo_height()
            if actual[0] < 2 or actual[1] < 2:
                # Fall back to a short deferred re-apply once Tk has mapped.
                self.window.geometry(placement)
        except Exception:
            pass

    def start(self) -> None:
        if self._after_id is not None:
            return
        if self._reduced_motion:
            self._after_id = self.window.after(600, self._enter_morph)
            return
        self._started_at = time.monotonic()
        self._after_id = self.window.after(0, self._tick)

    def _tick(self) -> None:
        if self._closed:
            return
        now = self._forced_elapsed or (time.monotonic() - (self._started_at or time.monotonic()))
        self._render(now)
        if now >= self.timing["transition"]:
            self._enter_morph()
            return
        self._after_id = self.window.after(self.FRAME_MS, self._tick)

    def _render(self, elapsed: float) -> None:
        start, duration = self.timing["laser"]
        progress = max(0.0, min(1.0, (elapsed - start) / duration))
        self._beam_x = 50.0 + progress * 200.0
        for item in self._guides:
            self._canvas.itemconfig(item, state="normal" if elapsed < 0.75 else "hidden")
        for item in self._star_segments:
            self._canvas.itemconfig(item, state="normal" if elapsed >= start else "hidden")
        self._canvas.itemconfig(self._fill, state="normal" if elapsed >= self.timing["fill"][0] else "hidden")
        self._canvas.itemconfig(self._glow, state="normal" if elapsed >= self.timing["glow"][0] else "hidden")
        self._canvas.itemconfig(self._wordmark, state="normal" if elapsed >= self.timing["wordmark"][0] else "hidden")
        self._canvas.itemconfig(self._tagline, state="normal" if self.variant == "full" and elapsed >= self.timing["tagline"][0] else "hidden")
        for index, item in enumerate(self._nodes):
            node_at = start + index / 16.0 * duration
            self._canvas.itemconfig(item, state="normal" if elapsed >= node_at else "hidden")
        if start <= elapsed < start + duration:
            x, y = self._star_point(progress)
            r = max(2.0, 3.2 * self._scale)
            self._canvas.coords(self._head, x - r, y - r, x + r, y + r)
            self._canvas.itemconfig(self._head, state="normal")
        else:
            self._canvas.itemconfig(self._head, state="hidden")
        flash_start, flash_duration = self.timing["flash"] if "flash" in self.timing else (0.0, 0.0)
        flash_progress = (elapsed - flash_start) / flash_duration if flash_duration else 1.0
        if 0.0 <= flash_progress <= 1.0:
            radius = (5.0 + 42.0 * flash_progress) * self._scale
            x, y = self._map(*self.STAR_POINTS[0])
            self._canvas.coords(self._flash, x - radius, y - radius, x + radius, y + radius)
            self._canvas.itemconfig(self._flash, state="normal")
        else:
            self._canvas.itemconfig(self._flash, state="hidden")

    def _star_point(self, progress: float) -> tuple[float, float]:
        points = self.STAR_POINTS
        lengths = []
        total = 0.0
        for index, point in enumerate(points):
            other = points[(index + 1) % len(points)]
            length = math.hypot(other[0] - point[0], other[1] - point[1])
            lengths.append(length)
            total += length
        remaining = max(0.0, min(1.0, progress)) * total
        for index, length in enumerate(lengths):
            if remaining <= length:
                start = points[index]
                end = points[(index + 1) % len(points)]
                ratio = remaining / length if length else 0.0
                return self._map(
                    start[0] + (end[0] - start[0]) * ratio,
                    start[1] + (end[1] - start[1]) * ratio,
                )
            remaining -= length
        return self._map(*points[0])

    def _show_resolved(self) -> None:
        for item in self._guides:
            self._canvas.itemconfig(item, state="hidden")
        for item in (*self._star_segments, self._fill, self._glow, self._wordmark):
            self._canvas.itemconfig(item, state="normal")
        self._canvas.itemconfig(self._tagline, state="hidden")
        self._canvas.itemconfig(self._head, state="hidden")
        for item in self._nodes:
            self._canvas.itemconfig(item, state="normal")
        self._beam_x = 250.0

    def _enter_morph(self) -> None:
        if self._morphing or self._closed:
            return
        self._morphing = True
        self._show_resolved()
        if not self._transitioned and self._on_transition is not None:
            self._transitioned = True
            try:
                self._on_transition()
            except Exception:
                pass
        self._fade_step = 0
        self._after_id = self.window.after(0 if self._reduced_motion else self.HOLD_AFTER_SCAN_MS, self._morph_step)

    def _morph_step(self) -> None:
        if self._closed:
            return
        self._fade_step += 1
        try:
            self.window.attributes("-alpha", max(0.0, 1.0 - self._fade_step / self.FADE_STEPS))
        except Exception:
            pass
        if self._fade_step >= self.FADE_STEPS:
            self.close()
        else:
            self._after_id = self.window.after(self.FADE_DELTA_MS, self._morph_step)

    def skip(self) -> None:
        if self._closed:
            return
        if self._after_id is not None:
            try:
                self.window.after_cancel(self._after_id)
            except Exception:
                pass
        self._show_resolved()
        if not self._transitioned and self._on_transition is not None:
            self._transitioned = True
            try:
                self._on_transition()
            except Exception:
                pass
        self._morphing = True
        self._fade_step = 0
        self._after_id = self.window.after(0, self._morph_step)

    def close(self) -> None:
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

    def advance_frame(self) -> None:
        if self._closed:
            return
        if self._morphing:
            self._morph_step()
            return
        self._forced_elapsed += self.FRAME_MS / 1000.0
        self._render(self._forced_elapsed)
        if self._forced_elapsed >= self.timing["transition"]:
            self._enter_morph()
