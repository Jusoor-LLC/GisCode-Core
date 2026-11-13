"""The Windows version resources must be complete before a signing request.

SignPath Foundation requires every signed binary to carry metadata attributes
and enforces them with file-metadata restrictions, so an empty version resource
blocks a signing request outright. These tests keep the generator, the specs and
``giscode.__version__`` in agreement.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import windows_version  # noqa: E402

REQUIRED_STRINGS = (
    "CompanyName",
    "FileDescription",
    "FileVersion",
    "InternalName",
    "LegalCopyright",
    "OriginalFilename",
    "ProductName",
    "ProductVersion",
)

TARGETS = (
    ("GisCodeCore.spec", "GisCode.exe"),
    ("GisCodeAcceptance.spec", "GisCodeAcceptance.exe"),
    ("GisCodeAcceptanceHardened.spec", "GisCodeAcceptanceHardened.exe"),
    ("packaging/windows_installer/GisCodeSetup.spec", "GisCodeSetup.exe"),
)


def test_release_version_comes_from_the_package():
    source = (ROOT / "giscode" / "__init__.py").read_text(encoding="utf-8")
    assert f'__version__ = "{windows_version.release_version()}"' in source
    assert windows_version.numeric_version()[:3] == tuple(
        int(part) for part in windows_version.release_version().split(".")[:3]
    )


@pytest.mark.parametrize("_,exe_name", TARGETS)
def test_rendered_resource_is_complete(_, exe_name, tmp_path):
    path = windows_version.version_file(
        file_description="GisCode Core",
        original_filename=exe_name,
        internal_name=Path(exe_name).stem,
        dest_dir=tmp_path,
    )
    text = Path(path).read_text(encoding="utf-8")
    for key in REQUIRED_STRINGS:
        assert f"StringStruct('{key}'" in text, f"{exe_name}: missing {key}"
    assert windows_version.release_version() in text
    assert f"filevers={windows_version.numeric_version()}" in text
    assert f"prodvers={windows_version.numeric_version()}" in text
    assert "''" not in text, "an empty metadata value would fail the metadata restriction"


@pytest.mark.parametrize("_,exe_name", TARGETS)
def test_rendered_resource_parses_as_pyinstaller_version_info(_, exe_name, tmp_path):
    versioninfo = pytest.importorskip("PyInstaller.utils.win32.versioninfo")
    path = windows_version.version_file(
        file_description="GisCode Core",
        original_filename=exe_name,
        internal_name=Path(exe_name).stem,
        dest_dir=tmp_path,
    )
    info = versioninfo.load_version_info_from_text_file(str(path))
    assert info is not None
    major, minor, patch, build = windows_version.numeric_version()
    expected_ms = (major << 16) | minor
    expected_ls = (patch << 16) | build
    assert info.ffi.fileVersionMS == expected_ms
    assert info.ffi.fileVersionLS == expected_ls
    assert info.ffi.productVersionMS == expected_ms
    assert info.ffi.productVersionLS == expected_ls


@pytest.mark.parametrize("spec_path,exe_name", TARGETS)
def test_specs_pass_the_version_resource(spec_path, exe_name):
    spec = (ROOT / spec_path).read_text(encoding="utf-8")
    assert "from windows_version import version_file" in spec
    assert "version=version_file(" in spec
    assert f'original_filename="{exe_name}"' in spec
    assert "version_file()" not in spec, "the version resource needs explicit metadata"


def test_installer_recipe_embeds_a_configurable_application():
    spec = (ROOT / "packaging" / "windows_installer" / "GisCodeSetup.spec").read_text(
        encoding="utf-8"
    )
    assert "GISCODE_APP_EXE" in spec
    assert "datas=[(str(APP_EXE), \".\")]" in spec
    # console=True and upx=True are what the qualified installer was built with.
    assert "console=True" in spec
    assert "upx=True" in spec


def test_installer_version_literal_matches_the_package():
    """The standalone installer cannot import giscode, so its literal must be
    pinned to the package version by a test rather than left to drift."""
    import re

    text = (ROOT / "packaging" / "windows_installer" / "giscode_setup.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^VERSION = "([^"]+)"', text, re.MULTILINE)
    assert match is not None, "the installer declares no VERSION literal"
    assert match.group(1) == windows_version.release_version()


def test_publisher_identity_matches_the_notice_file():
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert windows_version.PUBLISHER in notice
    assert windows_version.COPYRIGHT.split("2026 ")[-1] in notice
