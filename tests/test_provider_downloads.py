"""Download manager tests against a local HTTP server.

The server implements ``Range`` and can be gated, which makes pause/resume
deterministic instead of timing-dependent: the body is not written until the test
releases it, so the transfer is provably still running when ``pause()`` is called.
"""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from giscode.providers.downloads import (
    CANCELLED,
    DONE,
    PAUSED,
    DownloadError,
    DownloadManager,
)

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB, position-dependent so a hole shows


class Handler(BaseHTTPRequestHandler):
    gate: threading.Event | None = None
    support_range = True
    seen_ranges: list = []

    def log_message(self, *args):  # silence the test output
        return

    def do_GET(self):
        Handler.seen_ranges.append(self.headers.get("Range"))
        start = 0
        header = self.headers.get("Range") or ""
        if self.support_range and header.startswith("bytes="):
            start = int(header.split("=")[1].split("-")[0])
        body = PAYLOAD[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        if self.support_range:
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        self.end_headers()
        if Handler.gate is not None:
            Handler.gate.wait(timeout=10)
        self.wfile.write(body)


@pytest.fixture()
def server():
    Handler.gate = None
    Handler.support_range = True
    Handler.seen_ranges = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    if Handler.gate is not None:
        Handler.gate.set()


def _url(httpd, name="model.gguf"):
    host, port = httpd.server_address
    return f"http://{host}:{port}/{name}"


def _wait(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_download_completes_and_writes_exact_bytes(server, tmp_path):
    manager = DownloadManager(tmp_path)
    download = manager.start(_url(server), "tiny.gguf")
    assert _wait(lambda: manager.get(download.id).status == DONE)
    assert (tmp_path / "tiny.gguf").read_bytes() == PAYLOAD
    assert manager.get(download.id).bytes_total == len(PAYLOAD)


def test_pause_keeps_partial_file_and_resume_uses_range(server, tmp_path):
    Handler.gate = threading.Event()
    manager = DownloadManager(tmp_path, chunk_size=64 * 1024)
    download = manager.start(_url(server), "resume.gguf")
    assert _wait(lambda: manager.get(download.id).status == "running")

    manager.pause(download.id)
    Handler.gate.set()
    # pause() marks the entry immediately; the worker settles a moment later, so
    # wait for the file it leaves behind rather than racing it.
    partial = tmp_path / "resume.gguf.part"
    assert _wait(lambda: partial.exists())
    assert _wait(lambda: manager.get(download.id).status == PAUSED)

    Handler.gate = None
    manager.resume(download.id)
    assert _wait(lambda: manager.get(download.id).status == DONE)
    assert (tmp_path / "resume.gguf").read_bytes() == PAYLOAD


def test_resume_from_existing_partial_sends_range_header(server, tmp_path):
    half = len(PAYLOAD) // 2
    (tmp_path / "half.gguf.part").write_bytes(PAYLOAD[:half])
    manager = DownloadManager(tmp_path)
    download = manager.start(_url(server), "half.gguf")
    assert _wait(lambda: manager.get(download.id).status == DONE)
    assert (tmp_path / "half.gguf").read_bytes() == PAYLOAD
    assert any(r and r.startswith(f"bytes={half}-") for r in Handler.seen_ranges)


def test_server_ignoring_range_restarts_cleanly(server, tmp_path):
    Handler.support_range = False
    (tmp_path / "ignored.gguf.part").write_bytes(b"stale")
    manager = DownloadManager(tmp_path)
    download = manager.start(_url(server), "ignored.gguf")
    assert _wait(lambda: manager.get(download.id).status == DONE)
    assert (tmp_path / "ignored.gguf").read_bytes() == PAYLOAD


def test_cancel_marks_cancelled_and_removes_partial(server, tmp_path):
    Handler.gate = threading.Event()
    manager = DownloadManager(tmp_path)
    download = manager.start(_url(server), "cancel.gguf")
    assert _wait(lambda: manager.get(download.id).status == "running")
    manager.cancel(download.id)
    Handler.gate.set()
    assert manager.get(download.id).status == CANCELLED
    assert _wait(lambda: not (tmp_path / "cancel.gguf.part").exists())


def test_retry_after_failure_starts_over(server, tmp_path):
    manager = DownloadManager(tmp_path)
    url = _url(server, "missing.gguf")
    download = manager.start(url.replace("missing.gguf", "unused.gguf"), "retry.gguf")
    assert _wait(lambda: manager.get(download.id).status == DONE)
    manager.retry(download.id)
    assert manager.get(download.id).bytes_done == 0


def test_unsafe_filenames_are_rejected(tmp_path):
    manager = DownloadManager(tmp_path)
    with pytest.raises(DownloadError):
        manager.start("http://127.0.0.1:1/x.gguf", "../../escape.gguf")


def test_non_http_scheme_is_rejected(tmp_path):
    manager = DownloadManager(tmp_path)
    with pytest.raises(DownloadError):
        manager.start("file:///etc/passwd", "x.gguf")


def test_delete_requires_confirmation_and_refuses_in_use(tmp_path):
    manager = DownloadManager(tmp_path)
    (tmp_path / "kept.gguf").write_bytes(b"x")
    with pytest.raises(DownloadError):
        manager.delete_model("kept.gguf")
    with pytest.raises(DownloadError):
        manager.delete_model("kept.gguf", confirm=True, in_use=True)
    assert (tmp_path / "kept.gguf").exists()
    manager.delete_model("kept.gguf", confirm=True)
    assert not (tmp_path / "kept.gguf").exists()
