"""Gis branding: default logo assets and per-project custom logo support."""
from giscode.branding.logo import (
    DEFAULT_ASSETS,
    DEFAULT_LOGO_SMALL,
    DEFAULT_LOGO_SPLASH,
    LOGO_SVG,
    LogoValidationError,
    MAX_CUSTOM_LOGO_BYTES,
    assets_dir,
    default_asset_status,
    gis_logo_path,
    read_logo_file,
    validate_logo_html,
)

__all__ = [
    "DEFAULT_ASSETS",
    "DEFAULT_LOGO_SMALL",
    "DEFAULT_LOGO_SPLASH",
    "LOGO_SVG",
    "LogoValidationError",
    "MAX_CUSTOM_LOGO_BYTES",
    "assets_dir",
    "default_asset_status",
    "gis_logo_path",
    "read_logo_file",
    "validate_logo_html",
]