# -*- mode: python ; coding: utf-8 -*-
"""Self-contained PyInstaller recipe for the public Core checkout.

``GisCode.spec`` depends on ``packaging/exe_allowlist.toml`` and
``release/boundary.py``, which are not part of this exported Core checkout.
This recipe keeps the same product intent without those release-only files:

* the Core GUI entry point is ``giscode/main.py``;
* only the three branding assets are bundled under ``assets/``;
* the ChatGPT browser is a real Core feature and needs pywebview/WebView2;
* GisAI, Pro modules and optional ML runtimes are excluded.

User data is never bundled: settings, DPAPI credentials, chats, logs and the
ChatGPT browser profile stay under ``%USERPROFILE%\\.giscode`` at runtime.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

import sys

ROOT = Path(SPECPATH).resolve()

sys.path.insert(0, str(ROOT))

from windows_version import version_file  # noqa: E402

ASSET_NAMES = (
    "gis_logo.svg",
    "gis_logo_small.png",
    "gis_logo_splash.png",
    "gis_logo.ico",
    "giscode_mark.svg",
    "giscode_mark_32.png",
    "giscode_mark_128.png",
    "giscode.ico",
)
datas = [
    (str(ROOT / "giscode" / "assets" / name), "assets")
    for name in ASSET_NAMES
]
# Reproduce the release documents without putting source or user data in the
# bundle. They are reference files only; no runtime code reads them.
for name in ("LICENSE", "NOTICE", "TRADEMARK.md"):
    source = ROOT / name
    if source.is_file():
        datas.append((str(source), "."))

# ``browser/worker.py`` imports pywebview lazily. PyInstaller does see that
# import, but the EdgeChromium backend is selected at runtime, so include it
# explicitly. ``clr`` comes from pythonnet and is required by pywebview's
# Windows backend.
hiddenimports = collect_submodules("webview") + [
    "clr",
    "pythonnet",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "PIL.Image",
    "PIL.ImageTk",
    # Recovery CLI: reached through ``python -m giscode.agentic.recover_edits``,
    # not an import in the GUI entry point, so name it explicitly for the
    # packaged Core recovery path.
    "giscode.agentic.recover_edits",
]

excludes = [
    # Private/Pro surfaces that are absent from the Core export or must not ship.
    "giscode.gisai",
    "giscode.developer_release",
    "giscode.remote",
    "giscode.ios",
    "giscode.gui.developer_release_workspace",
    "giscode.gui.gislogo_view",
    "giscode.gui.ios_xtool_view",
    "giscode.gui.pro_tabs",
    "giscode.gui.main_window_task9_orig",
    # Optional ML runtimes: Core uses the external llama-server path.
    "llama_cpp",
    "numpy",
    "torch",
    "torchaudio",
    "torchvision",
    "transformers",
    "tensorflow",
    "tensorflow_addons",
    "tensorflow_datasets",
    "tensorflow_estimator",
    "tensorflow_hub",
    "tensorflow_io",
    "tensorflow_model_optimization",
    "tensorflow_probability",
    "tensorflow_ranking",
    "tensorflow_recommenders",
    "tensorflow_text",
    "tensorflowjs",
]

a = Analysis(
    [str(ROOT / "giscode" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    name="GisCode",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "giscode" / "assets" / "gis_logo.ico"),
    version=version_file(
        file_description="GisCode Core",
        original_filename="GisCode.exe",
        internal_name="GisCode",
    ),
)
