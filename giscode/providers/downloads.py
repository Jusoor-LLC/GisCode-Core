"""Resumable GGUF downloads.

Ollama and LM Studio manage their own downloads, so this manager is for the one
case GisCode can honestly own: a GGUF file fetched over plain HTTP(S) into the
models folder.

Pause/resume is real, not cosmetic. It works by closing the connection and
re-opening it with a ``Range: bytes=<done>-`` header, so bytes already on disk are
never fetched twice. If a server ignores Range and returns the whole body again,
the partial file is discarded and the download restarts from zero - the one thing
that will not happen is a file stitched together from overlapping data.

Deletion always requires ``confirm=True`` and refuses a file that is in use.
"""
from __future__ import annotations

import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

QUEUED = "queued"
RUNNING = "running"
PAUSED = "paused"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class DownloadError(RuntimeError):
    pass


@dataclass
class Download:
    id: str
    url: str
    filename: str
    status: str = QUEUED
    bytes_done: int = 0
    bytes_total: int = 0
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    @property
    def path(self) -> str:
        return str(Path(self.filename))

    @property
    def percent(self) -> int:
        if self.bytes_total <= 0:
            return 0
        return min(100, int(self.bytes_done * 100 / self.bytes_total))

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "filename": self.filename,
            "status": self.status,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "percent": self.percent,
            "error": self.error,
        }


class DownloadManager:
    """Downloads GGUF files into one folder, with pause/resume/cancel/retry."""

    def __init__(
        self,
        models_dir,
        *,
        chunk_size: int = 256 * 1024,
        timeout: float = 30.0,
        on_change: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._dir = Path(models_dir).expanduser()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._chunk = chunk_size
        self._timeout = timeout
        self._on_change = on_change
        self._items: dict[str, Download] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._pause_flags: dict[str, threading.Event] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._pending_resume: dict[str, bool] = {}
        self._lock = threading.RLock()

    # -- public API --------------------------------------------------------
    def start(self, url: str, filename: str = "") -> Download:
        """Queue a download and start it in the background."""
        scheme = urllib.parse.urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise DownloadError("Only http:// and https:// downloads are supported.")
        name = self._safe_filename(filename or os.path.basename(urllib.parse.urlparse(url).path))
        target = self._dir / name
        with self._lock:
            for item in self._items.values():
                if item.filename == name and item.status in (QUEUED, RUNNING, PAUSED):
                    raise DownloadError(f"{name} is already being downloaded.")
            download = Download(id=uuid.uuid4().hex[:12], url=url, filename=name)
            # Resuming from an existing partial file is the point of the format.
            partial = target.with_suffix(target.suffix + ".part")
            if partial.exists():
                download.bytes_done = partial.stat().st_size
                download.status = PAUSED
            self._items[download.id] = download
            self._pause_flags[download.id] = threading.Event()
            self._cancel_flags[download.id] = threading.Event()
        self._notify(download)
        self._launch(download.id)
        return download

    def get(self, download_id: str) -> Optional[Download]:
        with self._lock:
            return self._items.get(download_id)

    def all(self) -> list[Download]:
        with self._lock:
            return list(self._items.values())

    def pause(self, download_id: str) -> None:
        with self._lock:
            download = self._items.get(download_id)
            if download is None:
                raise KeyError(download_id)
            if download.status != RUNNING:
                return
            self._pause_flags[download_id].set()
            download.status = PAUSED
        self._notify(download)

    def resume(self, download_id: str) -> None:
        launch = False
        with self._lock:
            download = self._items.get(download_id)
            if download is None:
                raise KeyError(download_id)
            if download.status not in (PAUSED, FAILED, CANCELLED):
                return
            download.status = QUEUED
            download.error = ""
            thread = self._threads.get(download_id)
            if thread is not None and thread.is_alive():
                # The paused worker is still inside read(): it has not seen the
                # pause yet. Clearing the flag and launching a second worker
                # would put two writers on the same .part file, so mark the
                # resume as pending and let that worker restart the transfer
                # itself once its file handle is closed.
                self._pending_resume[download_id] = True
                self._pause_flags[download_id].set()
            else:
                self._pause_flags[download_id].clear()
                self._cancel_flags[download_id].clear()
                launch = True
        self._notify(download)
        if launch:
            self._launch(download_id)

    def cancel(self, download_id: str) -> None:
        with self._lock:
            download = self._items.get(download_id)
            if download is None:
                raise KeyError(download_id)
            self._cancel_flags[download_id].set()
            self._pause_flags[download_id].clear()
            self._pending_resume.pop(download_id, None)
            download.status = CANCELLED
            download.finished_at = time.time()
        self._notify(download)
        self._discard_partial(download)

    def retry(self, download_id: str) -> None:
        with self._lock:
            download = self._items.get(download_id)
            if download is None:
                raise KeyError(download_id)
            download.bytes_done = 0
            download.bytes_total = 0
            download.error = ""
        self._discard_partial(download)
        self.resume(download_id)

    def remove(self, download_id: str) -> None:
        """Forget a finished/failed entry. Never deletes the model file."""
        with self._lock:
            download = self._items.pop(download_id, None)
            self._pause_flags.pop(download_id, None)
            self._cancel_flags.pop(download_id, None)
            self._pending_resume.pop(download_id, None)
        if download is not None:
            self._notify(download)

    # -- filesystem --------------------------------------------------------
    def delete_model(self, filename: str, *, confirm: bool = False, in_use: bool = False) -> Path:
        """Delete a downloaded model, but only with confirmation."""
        if not confirm:
            raise DownloadError("Deleting a model requires explicit confirmation.")
        if in_use:
            raise DownloadError("That model is loaded. Unload it before deleting the file.")
        path = self._dir / self._safe_filename(filename)
        if not path.exists():
            raise DownloadError(f"{path.name} is not in the models folder.")
        path.unlink()
        return path

    def installed(self) -> list[str]:
        return sorted(p.name for p in self._dir.glob("*.gguf"))

    # -- internals ---------------------------------------------------------
    def _safe_filename(self, name: str) -> str:
        raw = (name or "").strip()
        # Reject rather than reinterpret: silently turning "../../x.gguf" into
        # "x.gguf" would hide a path attempt instead of refusing it.
        if not raw or "/" in raw or "\\" in raw or ".." in raw:
            raise DownloadError(
                "That filename is not usable. Use a plain file name ending in .gguf."
            )
        cleaned = _SAFE_NAME.sub("-", raw).strip("-.")
        if not cleaned:
            raise DownloadError("That filename is not usable.")
        if not cleaned.lower().endswith(".gguf"):
            cleaned = f"{cleaned}.gguf"
        return cleaned

    def _notify(self, download: Download) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(download.as_dict())
        except Exception:
            # A progress hook must never break the transfer.
            pass

    def _launch(self, download_id: str) -> None:
        thread = threading.Thread(
            target=self._worker,
            args=(download_id,),
            name=f"giscode-download-{download_id}",
            daemon=True,
        )
        with self._lock:
            previous = self._threads.get(download_id)
            if (
                previous is not None
                and previous is not threading.current_thread()
                and previous.is_alive()
            ):
                # One worker per download, always: resume() hands the transfer to
                # the running worker rather than starting a second writer.
                return
            self._threads[download_id] = thread
        thread.start()

    def _discard_partial(self, download: Download) -> None:
        partial = Path(str(self._target(download)) + ".part")
        try:
            if partial.exists():
                partial.unlink()
        except OSError:
            pass

    def _target(self, download: Download) -> Path:
        return self._dir / download.filename

    def _stop_for_pause(self, download_id: str, download: Download) -> None:
        """Park this worker, or hand the transfer to a fresh one if resume() waited.

        Called after the file handle is closed, so a handoff never overlaps two
        writers. The status is written under the lock and only while it still
        belongs to this worker, so a resume that already moved the entry on
        cannot be overwritten with a stale PAUSED.
        """
        with self._lock:
            handoff = self._pending_resume.pop(download_id, False)
            if handoff:
                self._pause_flags[download_id].clear()
                self._cancel_flags[download_id].clear()
                download.status = QUEUED
            elif download.status == RUNNING:
                download.status = PAUSED
        self._notify(download)
        if handoff:
            self._launch(download_id)

    def _worker(self, download_id: str) -> None:
        with self._lock:
            download = self._items[download_id]
            download.status = RUNNING
        self._notify(download)

        target = self._target(download)
        partial = Path(str(target) + ".part")
        # The file on disk is the truth: bytes_done is updated just before the
        # buffered write lands, and a Range header that disagrees with the bytes
        # actually present would skip or duplicate data.
        start_at = partial.stat().st_size if partial.exists() else 0
        download.bytes_done = start_at
        # Create the partial file before the body arrives so a pause leaves a
        # real file on disk (and resume has something to continue from).
        if not partial.exists():
            partial.touch()
        if not partial.exists():
            download.bytes_done = 0

        request = urllib.request.Request(download.url, method="GET")
        if start_at:
            request.add_header("Range", f"bytes={start_at}-")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = getattr(response, "status", 200)
                total_header = response.headers.get("Content-Length")
                if start_at and status == 200:
                    # The server ignored Range: start over rather than corrupt.
                    start_at = 0
                    download.bytes_done = 0
                    partial.unlink(missing_ok=True)
                elif start_at and status == 206:
                    content_range = response.headers.get("Content-Range") or ""
                    match = re.search(r"/(\d+)\s*$", content_range)
                    if match:
                        download.bytes_total = int(match.group(1))
                if not download.bytes_total and total_header:
                    download.bytes_total = start_at + int(total_header)
                mode = "ab" if start_at else "wb"
                stopped = ""
                with open(partial, mode) as handle:
                    while True:
                        if self._cancel_flags[download_id].is_set():
                            stopped = CANCELLED
                            break
                        if self._pause_flags[download_id].is_set():
                            stopped = PAUSED
                            break
                        chunk = response.read(self._chunk)
                        if not chunk:
                            break
                        handle.write(chunk)
                        download.bytes_done += len(chunk)
                        self._notify(download)
                if stopped == CANCELLED:
                    download.status = CANCELLED
                    self._notify(download)
                    return
                if stopped == PAUSED:
                    self._stop_for_pause(download_id, download)
                    return
        except urllib.error.HTTPError as exc:
            download.status = FAILED
            download.error = f"HTTP {exc.code} while downloading."
            self._notify(download)
            return
        except Exception as exc:
            download.status = FAILED
            download.error = f"{type(exc).__name__}: {exc}"
            self._notify(download)
            return

        if self._cancel_flags[download_id].is_set():
            download.status = CANCELLED
            self._notify(download)
            return
        try:
            os.replace(partial, target)
        except OSError as exc:
            download.status = FAILED
            download.error = f"Could not move the finished file: {exc}"
            self._notify(download)
            return
        download.status = DONE
        download.finished_at = time.time()
        if not download.bytes_total:
            download.bytes_total = download.bytes_done
        self._notify(download)
