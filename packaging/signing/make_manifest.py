"""Build the SHA-256 manifest that a signing request is validated against.

Reads only artifact files, never credentials. The output manifest is the single
source of truth for "what the owner is about to sign":

    py -3.13 packaging/signing/make_manifest.py --artifacts dist --out signing/manifest.json

Every entry records size and SHA-256; the header records the release version,
the toolchain and the manifest's own digest, so a later hash mismatch is
detectable before anything reaches a signing service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import windows_version  # noqa: E402

SIGNED_EXTENSIONS = {".exe", ".dll", ".msi", ".ps1"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def toolchain() -> dict[str, str]:
    import platform

    try:
        import PyInstaller

        pyinstaller = PyInstaller.__version__
    except Exception:  # pragma: no cover - build machines without PyInstaller
        pyinstaller = "unavailable"
    return {
        "python": platform.python_version(),
        "pyinstaller": pyinstaller,
        "platform": platform.platform(),
    }


def build_manifest(artifacts: Path) -> dict:
    files = sorted(path for path in artifacts.rglob("*") if path.is_file())
    if not files:
        raise SystemExit(f"no artifacts found under {artifacts}")
    entries = []
    for path in files:
        entry = {
            "name": path.name,
            "relative_path": str(path.relative_to(artifacts)).replace("\\", "/"),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "signed_candidate": path.suffix.lower() in SIGNED_EXTENSIONS,
        }
        if not entry["signed_candidate"]:
            entry["note"] = "not a PE/signable artifact; retained for provenance"
        entries.append(entry)
    unsigned = [entry for entry in entries if entry["signed_candidate"]]
    if not unsigned:
        raise SystemExit("manifest contains no signable artifacts")
    body = {
        "release_version": windows_version.release_version(),
        "publisher_requested": windows_version.PUBLISHER,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "artifacts_root": artifacts.name,
        "toolchain": toolchain(),
        "artifacts": entries,
    }
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, help="directory holding the unsigned artifacts")
    parser.add_argument("--out", required=True, help="path of the manifest to write")
    args = parser.parse_args(argv)

    manifest = build_manifest(Path(args.artifacts).resolve())
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    # Detached digest of the manifest's exact bytes. Re-serialising the JSON in
    # another language cannot reproduce it, so the gate hashes the file itself.
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    sidecar = Path(str(destination) + ".sha256")
    sidecar.write_text(f"{digest}  {destination.name}\n", encoding="utf-8", newline="\n")
    print(f"manifest: {destination} ({len(manifest['artifacts'])} files)")
    print(f"release:  {manifest['release_version']}")
    print(f"digest:   {sidecar.read_text(encoding='utf-8').strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
