"""Managed llama.cpp runtime installation.

The runtime is installed under the user's GisCode profile, never inside the
application directory and never through a system package manager.  Each
component is downloaded from the official pinned release, checked against its
manifest SHA-256, extracted with traversal protection, and then probed with
``--version`` before it is recorded as usable.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from giscode.ai_module.artifacts import (
    Cancelled,
    DownloadError,
    download_component,
    extract_zip,
)
from giscode.ai_module.manifest import (
    RUNTIME_LICENSE,
    RUNTIME_RELEASE_URL,
    RUNTIME_VERSION,
    RuntimeBuild,
    runtime_build,
)
from giscode.ai_module.providers import LlamaCppProvider

Progress = Callable[[str, str, Optional[float]], None]


class RuntimeInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeInstallation:
    path: str
    backend: str
    version: str
    reused: bool = False
    downloaded_bytes: int = 0


class ManagedRuntimeInstaller:
    def __init__(
        self,
        runtime_root: str | Path,
        *,
        provider: Optional[LlamaCppProvider] = None,
        download=download_component,
    ) -> None:
        self.root = Path(runtime_root).expanduser()
        self.provider = provider or LlamaCppProvider()
        self._download = download

    def installed(self) -> Optional[str]:
        return self.provider.find_installed(self.root)

    def marker_for(self, server_path: str) -> Optional[dict]:
        parent = Path(server_path).parent
        for marker in (parent / "install.json", parent.parent / "install.json"):
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
            except (OSError, ValueError):
                continue
        return None

    def ensure(
        self,
        build: RuntimeBuild | str,
        *,
        free_disk: int = 0,
        cancel=lambda: False,
        progress: Progress = lambda *_: None,
    ) -> RuntimeInstallation:
        if isinstance(build, str):
            build = runtime_build(build)

        existing = self.installed()
        if existing:
            marker = self.marker_for(existing)
            installed_backend = str((marker or {}).get("backend") or "")
            # A previously installed runtime is reused only when it is the
            # backend this machine selected and it still starts.  A CPU build
            # must not silently survive a later CUDA-capable launch, and a
            # damaged build must be replaced rather than trusted by its marker.
            if marker and marker.get("version") == RUNTIME_VERSION and installed_backend == build.key:
                ok, _detail = self.provider.verify(existing)
                if ok:
                    progress("runtime", f"Using the installed {build.label} runtime.", 1.0)
                    return RuntimeInstallation(
                        path=existing,
                        backend=installed_backend,
                        version=str(marker.get("version")),
                        reused=True,
                    )
                progress("runtime", "The installed runtime did not start; repairing it.", 0.0)
            elif marker is None and "runtime" in Path(existing).parts:
                # A bundled runtime has no marker.  It is already part of this
                # build, so verify it and use it instead of downloading a
                # second copy.
                ok, _detail = self.provider.verify(existing)
                if ok:
                    progress("runtime", "Using the runtime bundled with GisCode.", 1.0)
                    return RuntimeInstallation(
                        path=existing,
                        backend=build.key,
                        version=RUNTIME_VERSION,
                        reused=True,
                    )
            elif installed_backend and installed_backend != build.key:
                progress(
                    "runtime",
                    f"Installing the {build.label} runtime for this PC (an older "
                    f"{installed_backend} build is present).",
                    0.0,
                )

        required = build.download_bytes
        if free_disk and free_disk < int(required * 1.10):
            raise RuntimeInstallError(
                f"The AI runtime needs about {_gib(required)} of free space, "
                f"but only {_gib(free_disk)} is available."
            )
        if cancel():
            raise Cancelled("The AI module setup was cancelled.")

        target = self.root / f"{RUNTIME_VERSION}-{build.key}"
        downloads = self.root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        progress(
            "runtime",
            f"Downloading the {build.label} runtime ({_gib(required)})...",
            0.0,
        )
        components: list[Path] = []
        completed = 0
        for component in build.components:
            last_reported = [-1]

            def report(done: int, total: int, _completed=completed, _name=component.filename):
                if cancel():
                    raise Cancelled("The runtime download was cancelled.")
                overall = (_completed + done) / max(1, required)
                percent = int(min(0.95, overall) * 100)
                if percent == last_reported[0]:
                    return
                last_reported[0] = percent
                progress("runtime", f"Downloading {_name}...", min(0.95, overall))

            components.append(
                self._download(
                    component,
                    downloads,
                    cancel=cancel,
                    progress=report,
                )
            )
            completed += component.size_bytes

        if cancel():
            raise Cancelled("The runtime installation was cancelled.")
        progress("runtime", "Verifying the downloaded runtime...", 0.96)
        staging = Path(tempfile.mkdtemp(prefix="giscode-runtime-", dir=str(self.root)))
        try:
            for component, archive in zip(build.components, components):
                if not archive.is_file():
                    raise RuntimeInstallError("A runtime component is missing after download.")
                # download_component already verified the digest when it was
                # fetched.  Re-checking from disk also covers a file that was
                # replaced while the installer was running.
                from giscode.ai_module.artifacts import verify_file

                if not verify_file(archive, component.sha256, cancel=cancel):
                    archive.unlink(missing_ok=True)
                    raise RuntimeInstallError(
                        f"{component.filename} failed its SHA-256 check and was deleted."
                    )
                extract_zip(archive, staging)

            server = next(
                (path for path in staging.rglob("llama-server.exe") if path.is_file()),
                None,
            )
            if server is None:
                raise RuntimeInstallError(
                    "The downloaded runtime does not contain llama-server.exe."
                )
            if cancel():
                raise Cancelled("The runtime installation was cancelled.")
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(staging), str(target))
            staging = None
            installed_server = _server_in(target)
            if installed_server is None:
                raise RuntimeInstallError("The runtime was extracted but llama-server.exe is missing.")
            ok, detail = self.provider.verify(str(installed_server))
            if not ok:
                shutil.rmtree(target, ignore_errors=True)
                raise RuntimeInstallError(
                    f"The downloaded runtime could not start on this PC: {detail}"
                )
            marker = {
                "version": RUNTIME_VERSION,
                "backend": build.key,
                "license": RUNTIME_LICENSE,
                "source": RUNTIME_RELEASE_URL,
                "server": str(installed_server.relative_to(target)).replace("\\", "/"),
                "components": [
                    {"filename": item.filename, "sha256": item.sha256}
                    for item in build.components
                ],
            }
            (target / "install.json").write_text(
                json.dumps(marker, indent=2), encoding="utf-8"
            )
            notice = Path(__file__).with_name("THIRD_PARTY_NOTICES.txt")
            if notice.is_file():
                shutil.copy2(notice, target / "THIRD_PARTY_NOTICES.txt")
            progress("runtime", f"The {build.label} runtime is ready.", 1.0)
            return RuntimeInstallation(
                path=str(installed_server),
                backend=build.key,
                version=RUNTIME_VERSION,
                downloaded_bytes=completed,
            )
        except Exception:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            raise


def _server_in(root: Path) -> Optional[Path]:
    return next((path for path in root.rglob("llama-server.exe") if path.is_file()), None)



def _gib(value: int) -> str:
    return f"{value / (1024 ** 3):.1f} GB"


def remove_runtime(runtime_root: str | Path, version: str = RUNTIME_VERSION) -> None:
    root = Path(runtime_root)
    for directory in root.glob(f"{version}-*"):
        shutil.rmtree(directory, ignore_errors=True)
