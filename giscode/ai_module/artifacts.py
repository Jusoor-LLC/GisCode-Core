"""Safe, resumable downloads and integrity checks for AI artifacts.

Runtime components are ZIP files and models are GGUF files, so the model
downloader's ``.gguf`` restriction is not enough.  This module implements the
same Range/resume rule for either suffix and verifies every completed artifact
against the pinned manifest before it is moved into place.
"""
from __future__ import annotations

import hashlib
import os
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from giscode.ai_module.manifest import RuntimeComponent

CHUNK = 256 * 1024


class DownloadError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


class HashMismatch(DownloadError):
    pass


def sha256_file(path: str | Path, *, cancel=lambda: False, progress=None) -> str:
    digest = hashlib.sha256()
    done = 0
    with open(path, "rb") as handle:
        while True:
            if cancel():
                raise Cancelled("The operation was cancelled.")
            chunk = handle.read(CHUNK * 4)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if progress is not None:
                progress(done)
    return digest.hexdigest()


def verify_file(
    path: str | Path,
    expected_sha256: str,
    *,
    cancel=lambda: False,
    progress=None,
) -> bool:
    return sha256_file(path, cancel=cancel, progress=progress).lower() == expected_sha256.lower()


def download_file(
    url: str,
    target: str | Path,
    *,
    expected_size: int = 0,
    expected_sha256: str = "",
    cancel=lambda: False,
    progress: Optional[Callable[[int, int], None]] = None,
    timeout: float = 30.0,
) -> Path:
    """Download *url* to *target*, resuming a ``.part`` file when possible.

    The caller owns cancellation.  A cancelled transfer keeps its ``.part`` so
    the next attempt resumes from the bytes already on disk; a hash mismatch
    deletes both the finished file and the partial file.
    """
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and expected_sha256:
        if verify_file(destination, expected_sha256, cancel=cancel):
            if progress:
                size = destination.stat().st_size
                progress(size, size)
            return destination
        destination.unlink(missing_ok=True)

    partial = destination.with_suffix(destination.suffix + ".part")
    start_at = partial.stat().st_size if partial.is_file() else 0
    request = urllib.request.Request(url, method="GET")
    if start_at:
        request.add_header("Range", f"bytes={start_at}-")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if start_at and status != 206:
                # The server ignored Range. Start over rather than stitch two
                # unrelated byte streams into a corrupt executable.
                start_at = 0
                partial.unlink(missing_ok=True)
            elif start_at and status == 206:
                content_range = response.headers.get("Content-Range") or ""
                match = re.search(r"/(\d+)\s*$", content_range)
                if match and not expected_size:
                    expected_size = int(match.group(1))
            content_length = response.headers.get("Content-Length")
            if not expected_size and content_length:
                expected_size = start_at + int(content_length)
            mode = "ab" if start_at else "wb"
            done = start_at
            # A 4 GB model produces thousands of 256 KiB reads.  Reporting each
            # one floods the Tk event queue without adding information.
            report_every = max(1024 * 1024, (expected_size or 0) // 100)
            last_report = done
            with open(partial, mode) as handle:
                while True:
                    if cancel():
                        raise Cancelled("The download was cancelled.")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if progress and (done - last_report >= report_every or done == expected_size):
                        last_report = done
                        progress(done, expected_size or done)
            if expected_size and done != expected_size:
                # Keep the partial for a resume, but never present it as done.
                raise DownloadError(
                    f"The download ended at {done} of {expected_size} bytes; retry to resume."
                )
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"The download server returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise DownloadError(f"The download could not reach the server: {exc.reason}") from exc
    except Cancelled:
        raise
    except OSError as exc:
        raise DownloadError(f"The download failed: {exc}") from exc

    if expected_sha256 and not verify_file(partial, expected_sha256, cancel=cancel):
        partial.unlink(missing_ok=True)
        raise HashMismatch(
            "The downloaded file did not match its pinned SHA-256 and was deleted."
        )
    try:
        os.replace(partial, destination)
    except OSError as exc:
        raise DownloadError(f"The finished file could not be moved into place: {exc}") from exc
    return destination


def download_component(
    component: RuntimeComponent,
    directory: str | Path,
    *,
    cancel=lambda: False,
    progress=None,
) -> Path:
    path = Path(directory) / component.filename
    return download_file(
        component.url,
        path,
        expected_size=component.size_bytes,
        expected_sha256=component.sha256,
        cancel=cancel,
        progress=progress,
    )


def _safe_members(archive: zipfile.ZipFile, destination: Path):
    root = destination.resolve()
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            raise DownloadError(f"The archive contains an unsafe path: {info.filename}")
        target = (destination / name).resolve()
        if root != target and root not in target.parents:
            raise DownloadError(f"The archive escapes its install folder: {info.filename}")
        yield info


def extract_zip(archive_path: str | Path, destination: str | Path) -> Path:
    """Extract a ZIP without path traversal, returning the folder containing
    ``llama-server.exe`` (the release archives wrap it in one directory)."""
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in _safe_members(archive, target):
                archive.extract(info, target)
    except zipfile.BadZipFile as exc:
        raise DownloadError("The runtime archive is damaged and cannot be opened.") from exc
    for candidate in target.rglob("llama-server.exe"):
        if candidate.is_file():
            return candidate.parent
    raise DownloadError("The runtime archive does not contain llama-server.exe.")
