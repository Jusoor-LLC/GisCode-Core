# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the Core edition.

The packaging policy is not written here: it is read from
``packaging/exe_allowlist.toml`` and validated before any analysis runs, so a
change to what may ship is a reviewable diff to a data file. The recipe fails
closed: a missing, invalid, contradictory or unrecognised manifest stops the
build instead of producing a bundle nobody reviewed.

Two stages are enforced:

* ``Analysis(..., excludes=...)`` stops excluded packages from being imported at
  all, which covers ordinary and transitive imports;
* ``assert_toc_allowed`` inspects what Analysis actually collected, which also
  catches an excluded module reached through a dynamic import.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(SPECPATH).resolve()
sys.path.insert(0, str(ROOT / "release"))

import boundary  # noqa: E402 - after sys.path is extended

# The edition is chosen at build time and names its own allowlist. The default
# is Core, so the historical command keeps producing the Core product.
EDITIONS = {
    "core": ("GisCode", "packaging/exe_allowlist.toml"),
    "pro": ("GisCodePro", "packaging/exe_allowlist_pro.toml"),
}
EDITION = os.environ.get("GISCODE_BUILD_EDITION", "core").strip().lower() or "core"
if EDITION not in EDITIONS:
    raise SystemExit(
        f"unknown GISCODE_BUILD_EDITION: {EDITION!r} (expected one of {sorted(EDITIONS)})"
    )
PRODUCT_NAME, ALLOWLIST = EDITIONS[EDITION]

try:
    POLICY = boundary.build_pyinstaller_policy(ROOT, allowlist=ROOT / ALLOWLIST)
except (boundary.PyInstallerPolicyError, FileNotFoundError) as error:
    raise SystemExit(f"{EDITION} packaging policy refused the build: {error}")

# The manifest states the artifact name; the recipe must not rename it, or the
# verifier and the shipped file would disagree about what was built.
if (POLICY["document"].get("artifact") or {}).get("name") != PRODUCT_NAME + ".exe":
    raise SystemExit(
        "allowlist artifact name does not match the edition: "
        f"{ALLOWLIST} declares "
        f"{(POLICY['document'].get('artifact') or {}).get('name')!r}, "
        f"{EDITION} builds {PRODUCT_NAME + '.exe'!r}"
    )

# Private acceptance entry. Unset means the product build exactly as before: same
# entry point, EXE name, windowed mode and analysis. ``image`` swaps in a test
# driver in place of the product entry, so the frozen process runs the bundled
# image loader without adding any runtime switch to the shipped executable. Any
# other value stops the build instead of silently producing a product artifact.
ACCEPTANCE_ENTRY = os.environ.get("GISCODE_ACCEPTANCE_ENTRY", "").strip()
if ACCEPTANCE_ENTRY not in ("", "image"):
    raise SystemExit(
        f"unknown GISCODE_ACCEPTANCE_ENTRY: {ACCEPTANCE_ENTRY!r} "
        "(expected unset or 'image')"
    )
if ACCEPTANCE_ENTRY == "image":
    ENTRYPOINT = str(ROOT / "packaging" / "image_acceptance.py")
    EXE_NAME = PRODUCT_NAME + "Acceptance"
    CONSOLE = True
else:
    ENTRYPOINT = POLICY["entrypoint"]
    EXE_NAME = PRODUCT_NAME
    CONSOLE = False

# Machine-learning runtimes are not Core runtime dependencies; excluding them
# keeps the bundle small and the build predictable.
ML_EXCLUDES = [
    "tzdata",
    "pytzdata",
    "torch",
    "transformers",
    "torchaudio",
    "torchvision",
    "torchao",
    "torchtext",
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

RUNTIME_HOOKS: list[str] = []
if ACCEPTANCE_ENTRY == "image":
    # The acceptance executable must be able to prove which build it is, and a
    # plain environment variable cannot do that: a hook baked into the bundle
    # sets the marker, so the product code keeps the automation unreachable in
    # every other build. Generated under a temporary directory, never inside the
    # repository.
    hook_dir = Path(tempfile.mkdtemp(prefix="giscode_acceptance_hook_"))
    hook_path = hook_dir / "pyi_rth_giscode_acceptance.py"
    hook_source = (
        "import sys" + chr(10) + chr(10)
        + "sys._giscode_acceptance_build = True" + chr(10)
    )
    hook_path.write_text(hook_source, encoding="utf-8")
    RUNTIME_HOOKS = [str(hook_path)]

a = Analysis(
    [ENTRYPOINT],
    pathex=[],
    binaries=[],
    datas=list(POLICY["datas"]),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=RUNTIME_HOOKS,
    excludes=list(POLICY["excludes"]) + ML_EXCLUDES,
    noarchive=False,
    optimize=0,
)

# Fail closed on what Analysis actually pulled in, not only on what it was told
# to skip. Only paths inside the package and the packaged assets are judged;
# stdlib and third-party binaries are outside the allowlist's scope.
# Drop only the data the allowlist names explicitly (Tcl module catalogs,
# setuptools sample text, pandas export templates). The assertion below still
# judges everything that remains, so an unlisted third-party data file fails
# the build instead of slipping through.
a.datas = [
    entry
    for entry in a.datas
    if not boundary.is_dropped_data(str(entry[0]), POLICY["drop_datas"])
]

boundary.assert_toc_allowed(list(a.pure) + list(a.datas), POLICY["document"])
# And fail closed on what the bundle is missing: a Pro build that omits the
# package data its capabilities load starts fine and then reports the capability
# unavailable, which is the PRO-02 defect this assertion answers.
boundary.assert_required_data(list(a.datas), POLICY["document"])

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
