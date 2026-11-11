# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the Core acceptance executable.

This is the production Core bundle with exactly three deliberate differences:

* a PyInstaller runtime hook sets ``sys._giscode_acceptance_build``, which is
  what makes ``giscode.main``'s automation reachable at all;
* the entry point is still ``giscode/main.py`` - the product entry - so the
  scenario drives the same ``MainWindow`` the product builds;
* the executable is a console build named ``GisCodeAcceptance``.

The product recipe (``GisCodeCore.spec``) is untouched, so a normal build keeps
the marker absent and the automation unreachable even if the environment
variables are inherited. The generated hook lives in a temporary directory and
is never written into the repository. User data is never bundled.
"""

import tempfile
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
)
datas = [
    (str(ROOT / "giscode" / "assets" / name), "assets")
    for name in ASSET_NAMES
]
for name in ("LICENSE", "NOTICE", "TRADEMARK.md"):
    source = ROOT / name
    if source.is_file():
        datas.append((str(source), "."))

hiddenimports = collect_submodules("webview") + [
    "clr",
    "pythonnet",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "PIL.Image",
    "PIL.ImageTk",
    # Imported lazily from within the acceptance gate, so name it explicitly:
    # a bundle that silently omitted it would only fail at the moment of use.
    "giscode.acceptance_runtime",
]

excludes = [
    "giscode.gisai",
    "giscode.developer_release",
    "giscode.remote",
    "giscode.ios",
    "giscode.gui.developer_release_workspace",
    "giscode.gui.gislogo_view",
    "giscode.gui.ios_xtool_view",
    "giscode.gui.pro_tabs",
    "giscode.gui.main_window_task9_orig",
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
    "tensorflow_io_gcs_filesystem",
    "tensorflow_metadata",
    "tensorflow_model_optimization",
    "tensorflow_probability",
    "tensorflow_ranking",
    "tensorflow_recommenders",
    "tensorflow_text",
    "tensorflowjs",
]

# The marker is baked into the executable: an environment variable cannot set it,
# so no inherited variable can switch the automation on in another build.
hook_dir = Path(tempfile.mkdtemp(prefix="giscode_acceptance_hook_"))
hook_path = hook_dir / "pyi_rth_giscode_acceptance.py"
hook_path.write_text(
    "import sys" + chr(10) + chr(10)
    + "sys._giscode_acceptance_build = True" + chr(10),
    encoding="utf-8",
)

a = Analysis(
    [str(ROOT / "giscode" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(hook_path)],
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
    name="GisCodeAcceptance",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "giscode" / "assets" / "gis_logo.ico"),
    version=version_file(
        file_description="GisCode Core acceptance build",
        original_filename="GisCodeAcceptance.exe",
        internal_name="GisCodeAcceptance",
    ),
)
