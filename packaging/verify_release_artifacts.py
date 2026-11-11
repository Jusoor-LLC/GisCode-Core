"""Verify the release executables and write provenance.

Used by both the trusted CI workflow and a developer machine, so the checks a
reviewer sees in CI are the checks that can be reproduced locally.

    py -3.13 packaging/verify_release_artifacts.py --check-source-version

    py -3.13 packaging/verify_release_artifacts.py ^
        --dist build/dist --acceptance build/dist-acceptance ^
        --installer build/dist-installer --provenance build/provenance.json ^
        --require-version 0.1.1

Checks, all fatal:
  * the package version, ``pyproject.toml`` and the packaging config agree;
  * every artifact carries the expected CompanyName/ProductName/version;
  * the installer embeds a byte-identical copy of the built application;
  * artifacts are unsigned (a signed binary here would mean an unexpected step);
  * SHA-256 is recorded for every file.

It never signs, never reads a credential and never mutates an artifact.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import windows_version  # noqa: E402

STRINGS = ("CompanyName", "ProductName", "FileVersion", "ProductVersion",
           "OriginalFilename", "LegalCopyright")


def fail(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"VERIFY FAILED: {message}", file=sys.stderr)
    raise SystemExit(2)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_strings(path: Path) -> dict[str, str]:
    """Read the VS_VERSIONINFO string table through version.dll."""
    if os.name != "nt":
        fail("version resources can only be read on Windows")
    size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        fail(f"{path.name}: no version resource")
    buffer = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
        fail(f"{path.name}: version resource unreadable")
    values: dict[str, str] = {}
    for key in STRINGS:
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        query = f"\\StringFileInfo\\040904B0\\{key}"
        if not ctypes.windll.version.VerQueryValueW(
            buffer, query, ctypes.byref(pointer), ctypes.byref(length)
        ):
            values[key] = ""
            continue
        values[key] = ctypes.wstring_at(pointer.value, length.value).rstrip("\x00")
    return values


def certificate_table_bytes(path: Path) -> int:
    data = path.read_bytes()
    pe = int.from_bytes(data[0x3C:0x40], "little")
    optional = pe + 24
    magic = int.from_bytes(data[optional:optional + 2], "little")
    directory = optional + (112 if magic == 0x20B else 96)
    return int.from_bytes(data[directory + 32: directory + 36], "little")


def check_source_version() -> int:
    package = windows_version.release_version()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["project"]["version"]
    config_text = (ROOT / "giscode" / "packaging" / "config.py").read_text(encoding="utf-8")
    match = re.search(r'version:\s*str\s*=\s*"([^"]+)"', config_text)
    config = match.group(1) if match else "<missing>"
    print(f"giscode.__version__={package} pyproject={declared} packaging_config={config}")
    if not package == declared == config:
        fail("the three version declarations disagree")
    return 0


def embedded_app_sha256(installer: Path) -> tuple[str, int]:
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception:  # pragma: no cover - CI always has PyInstaller
        fail("PyInstaller is required to inspect the installer payload")
    reader = CArchiveReader(str(installer))
    names = [str(name) for name in reader.toc]
    entry = next((name for name in names if name.endswith("GisCode.exe")), None)
    if entry is None:
        fail("installer does not embed GisCode.exe")
    return hashlib.sha256(reader.extract(entry)).hexdigest(), len(names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-source-version", action="store_true")
    parser.add_argument("--dist")
    parser.add_argument("--acceptance")
    parser.add_argument("--installer")
    parser.add_argument("--provenance")
    parser.add_argument("--require-version", default=None)
    args = parser.parse_args(argv)

    if args.check_source_version:
        return check_source_version()
    if not (args.dist and args.acceptance and args.installer):
        parser.error("--dist, --acceptance and --installer are required")

    version = windows_version.release_version()
    expected = args.require_version or version
    if expected != version:
        fail(f"requested version {expected} but the source declares {version}")

    artifacts = {
        "GisCode.exe": Path(args.dist) / "GisCode.exe",
        "GisCodeAcceptanceHardened.exe": Path(args.acceptance) / "GisCodeAcceptanceHardened.exe",
        "GisCodeSetup.exe": Path(args.installer) / "GisCodeSetup.exe",
    }
    records: dict[str, dict] = {}
    for name, path in artifacts.items():
        if not path.is_file():
            fail(f"missing artifact: {path}")
        values = version_strings(path)
        for key in STRINGS:
            if not values.get(key):
                fail(f"{name}: empty {key}")
        if values["ProductName"] != windows_version.PRODUCT:
            fail(f"{name}: ProductName {values['ProductName']!r} != {windows_version.PRODUCT!r}")
        if values["FileVersion"] != expected or values["ProductVersion"] != expected:
            fail(f"{name}: version {values['FileVersion']}/{values['ProductVersion']} != {expected}")
        if values["OriginalFilename"] != name:
            fail(f"{name}: OriginalFilename {values['OriginalFilename']!r}")
        if certificate_table_bytes(path) != 0:
            fail(f"{name}: a code signature is present; this workflow must not sign")
        records[name] = {"bytes": path.stat().st_size, "sha256": sha256(path), "metadata": values}

    embedded, entries = embedded_app_sha256(artifacts["GisCodeSetup.exe"])
    if embedded != records["GisCode.exe"]["sha256"]:
        fail("installer payload differs from the built application")

    provenance = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "release_version": expected,
        "commit_sha": os.environ.get("GITHUB_SHA") or os.environ.get("GISCODE_COMMIT", ""),
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "runner_image": os.environ.get("ImageOS", ""),
        "toolchain": {
            "python": platform.python_version(),
            "pyinstaller": _pyinstaller_version(),
            "platform": platform.platform(),
        },
        "artifacts": records,
        "installer_payload": {"embedded_sha256": embedded, "entries": entries,
                              "embedded_equals_app": True},
        "signed": False,
        "note": ("Unsigned build. Byte-for-byte reproducibility is not claimed: "
                 "PyInstaller's base_library.zip is not deterministic."),
    }
    for name, record in records.items():
        print(f"OK {name} | {record['bytes']} | {record['sha256']} | "
              f"{record['metadata']['FileVersion']} | unsigned")
    if args.provenance:
        destination = Path(args.provenance)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(provenance, indent=2) + "\n",
                               encoding="utf-8", newline="\n")
        print(f"provenance: {destination} ({sha256(destination)})")
    return 0


def _pyinstaller_version() -> str:
    try:
        import PyInstaller

        return PyInstaller.__version__
    except Exception:  # pragma: no cover
        return "unavailable"


if __name__ == "__main__":
    raise SystemExit(main())
