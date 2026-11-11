# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the per-user installer ``GisCodeSetup.exe``.

The installer carries ``GisCode.exe`` as a data file, so build (and, for a
release, sign) the application first. Point ``GISCODE_APP_EXE`` at that binary;
when the variable is unset the recipe uses ``dist/GisCode.exe``.

Run from the repository root so ``SPECPATH`` resolves as expected::

    py -3.13 -m PyInstaller --noconfirm --clean \
        --distpath dist-installer --workpath build/pyinstaller-installer \
        packaging/windows_installer/GisCodeSetup.spec
"""
import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windows_version import version_file  # noqa: E402

APP_EXE = Path(os.environ.get("GISCODE_APP_EXE", str(ROOT / "dist" / "GisCode.exe"))).resolve()
if not APP_EXE.is_file():
    raise SystemExit(f"GisCodeSetup.spec: application executable not found: {APP_EXE}")

a = Analysis(
    [str(ROOT / "packaging" / "windows_installer" / "giscode_setup.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(APP_EXE), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="GisCodeSetup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_file(
        file_description="GisCode Core installer",
        original_filename="GisCodeSetup.exe",
        internal_name="GisCodeSetup",
    ),
)
