"""Official Gis branding assets and custom-logo (HTML) handling.

The default Gis logo is shipped as packaged raster assets generated from the
actual logo artwork in ``gis-logo.html``:

* :data:`DEFAULT_LOGO_SMALL`  - rounded dark tile used in the top bar.
* :data:`DEFAULT_LOGO_SPLASH` - full splash artwork shown at startup.
* :data:`LOGO_SVG`            - static SVG reference copy of the mark.

:func:`gis_logo_path` resolves the assets in both source-launch and
frozen (``dist\\GisCode.exe``) mode, so the packaged application never
depends on the developer's ``gis-logo.html`` existing elsewhere.

Users may attach a custom logo **HTML** file to the app/project they are
building. The HTML is validated sufficiently to be used as a logo
(see :func:`validate_logo_html`); it is stored per project (never globally)
in that project's context record - GisCode itself always keeps the official
Gis branding as the default.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

DEFAULT_LOGO_SMALL = "gis_logo_small.png"
DEFAULT_LOGO_SPLASH = "gis_logo_splash.png"
LOGO_SVG = "gis_logo.svg"
#: The authentic Gis star. ``gis_logo_small.png`` (48 px tile) and
#: ``gis_logo_splash.png`` (620 px star with the GIS wordmark) shipped in the
#: initial public release and were extracted from the original
#: ``gis-logo.html`` artwork; ``gis_logo.ico`` is the same 48 px tile packaged
#: as a multi-size Windows icon. The Ink & Ember shell uses these assets
#: unchanged so the brand mark is the original one, not a redesign.
DEFAULT_LOGO_MARK = "gis_logo_small.png"
DEFAULT_LOGO_MARK_LARGE = "gis_logo_splash.png"
APP_ICON = "gis_logo.ico"

DEFAULT_ASSETS = (
    DEFAULT_LOGO_SMALL,
    DEFAULT_LOGO_SPLASH,
    LOGO_SVG,
    DEFAULT_LOGO_MARK,
    DEFAULT_LOGO_MARK_LARGE,
    APP_ICON,
)

MAX_CUSTOM_LOGO_BYTES = 256 * 1024

# Recognizable logo-intent markup. A candidate file must contain one of these
# to be considered usable as a logo.
_LOGO_MARKERS = ("<svg", "<img", "<canvas", "<picture", "<text ")


class LogoValidationError(ValueError):
    """Raised when a chosen logo HTML document cannot be used as a logo."""


def assets_dir() -> Path:
    """Directory that holds the packaged logo assets.

    Frozen mode (PyInstaller): ``sys._MEIPASS/assets``.
    Source mode: ``giscode/assets`` next to this package.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        base = Path(meipass) if meipass else Path(sys.executable).parent
        return base / "assets"
    return Path(__file__).resolve().parent.parent / "assets"


def gis_logo_path(name: str = DEFAULT_LOGO_SMALL) -> Path:
    """Absolute path to a default Gis logo asset (works in the EXE too)."""
    return assets_dir() / name


def default_asset_status() -> dict:
    """Small helper used by checks/tests: which default assets exist."""
    return {name: gis_logo_path(name).is_file() for name in DEFAULT_ASSETS}


def validate_logo_html(text: str) -> str:
    """Validate candidate logo HTML and return the normalized form.

    Raises :class:`LogoValidationError` with an honest, user-facing message
    when the file size, structure or logo markers make it unusable.
    """
    if not isinstance(text, str) or not text.strip():
        raise LogoValidationError("The selected file is empty - it cannot be used as a logo.")

    raw = text.encode("utf-8", errors="replace")
    if len(raw) > MAX_CUSTOM_LOGO_BYTES:
        raise LogoValidationError(
            f"The selected file is {len(raw):,} bytes; logo HTML must be at most "
            f"{MAX_CUSTOM_LOGO_BYTES:,} bytes."
        )

    stripped = text.strip()
    lowered = stripped.lower()
    if "\x00" in stripped[:4096]:
        raise LogoValidationError("The selected file appears to be a binary file, not a logo HTML document.")

    if not any(marker in lowered for marker in _LOGO_MARKERS):
        raise LogoValidationError(
            "The selected file does not look like a logo document - it contains no "
            "recognizable logo markup (SVG, <img>, or similar)."
        )

    if "<" not in lowered:
        raise LogoValidationError("The selected file contains no markup and cannot be used as a logo.")

    return stripped


def read_logo_file(path: str) -> Optional[str]:
    """Best-effort read of a logo HTML file for the picker flow.

    Reads at most :data:`MAX_CUSTOM_LOGO_BYTES` + 1 bytes so oversized
    selections are reported honestly instead of being truncated silently.
    """
    with open(path, "rb") as fh:
        data = fh.read(MAX_CUSTOM_LOGO_BYTES + 1)
    return data.decode("utf-8", errors="replace")