"""Windows PE version resources for the GisCode Core PyInstaller recipes.

One source of truth: the version comes from ``giscode.__version__`` and the
publisher identity from ``NOTICE`` (the project's own copyright holder). The
PyInstaller ``version=`` argument wants a path to a ``VSVersionInfo`` script, so
:func:`version_file` renders that script deterministically and returns the path.

Only the ``*.spec`` recipes import this module. The application never imports it
at run time, and nothing here is bundled into the frozen executables.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Publisher identity as stated by this project's own NOTICE file. It is the
# documented copyright holder, not a verified company registration: the owning
# legal entity is an owner decision, and the signing certificate is issued to
# SignPath Foundation anyway (see packaging/signing/README.md).
PUBLISHER = "Jusoor Company LLC"
PRODUCT = "GisCode Core"
COPYRIGHT = "Copyright (c) 2026 Jusoor Company LLC"

OUTPUT_DIR = ROOT / "build" / "version-resources"
LANGUAGE_TABLE = "040904B0"  # en-US, Unicode (matches the pid/lcid below)
LANGUAGE_ID = 1033
CODEPAGE = 1200


def release_version() -> str:
    """Return the packaged release version, read from ``giscode/__init__.py``."""
    text = (ROOT / "giscode" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([0-9][0-9A-Za-z.\-+]*)"', text, re.MULTILINE)
    if match is None:
        raise RuntimeError("giscode/__init__.py does not define a numeric __version__")
    return match.group(1)


def numeric_version() -> tuple[int, int, int, int]:
    """Return ``(major, minor, patch, build)`` for ``FixedFileInfo``."""
    parts = [int(part) for part in re.findall(r"\d+", release_version())[:4]]
    return tuple((parts + [0, 0, 0, 0])[:4])  # type: ignore[return-value]


def render(
    *,
    file_description: str,
    original_filename: str,
    internal_name: str,
    product_name: str = PRODUCT,
) -> str:
    """Render a PyInstaller ``VSVersionInfo`` script for one executable."""
    version = release_version()
    quad = numeric_version()
    strings = (
        ("CompanyName", PUBLISHER),
        ("FileDescription", file_description),
        ("FileVersion", version),
        ("InternalName", internal_name),
        ("LegalCopyright", COPYRIGHT),
        ("OriginalFilename", original_filename),
        ("ProductName", product_name),
        ("ProductVersion", version),
    )
    structs = ",\n".join(
        f"            StringStruct({name!r}, {value!r})" for name, value in strings
    )
    return (
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers={quad},\n"
        f"    prodvers={quad},\n"
        "    mask=0x3f,\n"
        "    flags=0x0,\n"
        "    OS=0x40004,\n"
        "    fileType=0x1,\n"
        "    subtype=0x0,\n"
        "    date=(0, 0),\n"
        "  ),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable(\n"
        f"        {LANGUAGE_TABLE!r},\n"
        "        [\n"
        f"{structs}\n"
        "        ],\n"
        "      )\n"
        "    ]),\n"
        f"    VarFileInfo([VarStruct('Translation', [{LANGUAGE_ID}, {CODEPAGE}])]),\n"
        "  ],\n"
        ")\n"
    )


def version_file(
    *,
    file_description: str,
    original_filename: str,
    internal_name: str,
    product_name: str = PRODUCT,
    dest_dir: str | Path | None = None,
) -> str:
    """Write the version resource for ``original_filename`` and return its path."""
    text = render(
        file_description=file_description,
        original_filename=original_filename,
        internal_name=internal_name,
        product_name=product_name,
    )
    destination = Path(dest_dir) if dest_dir is not None else OUTPUT_DIR
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{Path(original_filename).stem}.version.txt"
    path.write_text(text, encoding="utf-8", newline="\n")
    return str(path)
