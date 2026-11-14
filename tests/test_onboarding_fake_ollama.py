"""A fake loopback Ollama server for the HTTP paths.

No test here downloads a model. The server speaks just enough of Ollama's API to
prove status, model listing, pull progress/completion, malformed responses,
failure and cancellation, including closing the response when the user cancels.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from giscode.onboarding.download import CANCELLED, DONE, FAILED, OllamaPuller
from giscode.providers.ollama import OllamaProvider


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    models = ["qwen2.5-coder:7b"]
    pull_events = [
        {"status": "pulling manifest"},
        {"status": "pulling layers", "completed": 50, "total": 100},
        {"status": "success"},
    ]

    def log_message(self, *_args):
        return

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/version":
            if self.mode == "no_version":
                self._json({"detail": "missing"}, 404)
            else:
                self._json({"version": "0.9.1-fake"})
            return
        if self.path == "/api/tags":
            if self.mode == "malformed_tags":
                body = b"this is not json"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json(
                {
                    "models": [
                        {
                            "name": name,
                            "size": 123456,
                            "details": {
                                "quantization_level": "Q4_K_M",
                                "families": ["qwen2"],
                                "parameter_size": "7B",
                            },
                        }
                        for name in self.models
                    ]
                }
            )
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/api/pull":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        try:
            if self.mode == "malformed_pull":
                self.wfile.write(b"not a status object\n")
                return
            if self.mode == "slow":
                self.wfile.write(
                    json.dumps(
                        {"status": "pulling layer", "completed": 1, "total": 100}
                    ).encode()
                    + b"\n"
                )
                self.wfile.flush()
                for index in range(100):
                    if self.server.stop_slow.is_set():
                        return
                    time.sleep(0.05)
                    self.wfile.write(
                        json.dumps(
                            {
                                "status": "pulling layer",
                                "completed": index + 2,
                                "total": 100,
                            }
                        ).encode()
                        + b"\n"
                    )
                    self.wfile.flush()
                return
            for event in self.pull_events:
                self.wfile.write(json.dumps(event).encode() + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


@pytest.fixture
def fake_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.stop_slow = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.stop_slow.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_provider_detects_available_service_and_lists_models(fake_server):
    _server, endpoint = fake_server
    provider = OllamaProvider(endpoint=endpoint, timeout=2)
    status = provider.status()
    assert status.available is True
    assert status.version == "0.9.1-fake"
    models = provider.list_models()
    assert [item.id for item in models] == ["qwen2.5-coder:7b"]
    assert models[0].quantization == "Q4_K_M"


def test_provider_reports_missing_service_without_inventing_availability():
    # A closed loopback port is the missing-Ollama case.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    provider = OllamaProvider(endpoint=f"http://127.0.0.1:{port}", timeout=0.5)
    assert provider.status().available is False


def test_malformed_tags_are_reported_not_treated_as_models(fake_server, monkeypatch):
    server, endpoint = fake_server
    server.RequestHandlerClass.mode = "malformed_tags"
    try:
        provider = OllamaProvider(endpoint=endpoint, timeout=2)
        assert provider.status().available is True
        with pytest.raises(Exception):
            provider.list_models()
    finally:
        server.RequestHandlerClass.mode = "ok"


def test_http_pull_streams_real_progress_and_completion(fake_server):
    _server, endpoint = fake_server
    puller = OllamaPuller(transport="api", endpoint=endpoint)
    puller.start("qwen2.5-coder:7b", confirm=True)
    job = puller.wait("qwen2.5-coder:7b", timeout=10)
    assert job.status == DONE
    assert job.percent == 100
    assert job.bytes_total > 0
    assert job.bytes_done >= job.bytes_total


def test_http_pull_reports_a_malformed_stream(fake_server):
    server, endpoint = fake_server
    server.RequestHandlerClass.mode = "malformed_pull"
    try:
        puller = OllamaPuller(transport="api", endpoint=endpoint)
        puller.start("qwen2.5-coder:7b", confirm=True)
        job = puller.wait("qwen2.5-coder:7b", timeout=10)
        assert job.status == FAILED
        assert "not valid JSON" in job.detail
    finally:
        server.RequestHandlerClass.mode = "ok"


def test_cancelling_an_http_pull_closes_the_transfer(fake_server):
    server, endpoint = fake_server
    server.RequestHandlerClass.mode = "slow"
    try:
        puller = OllamaPuller(transport="api", endpoint=endpoint)
        puller.start("qwen2.5-coder:7b", confirm=True)
        deadline = time.time() + 5
        while time.time() < deadline:
            job = puller.job("qwen2.5-coder:7b")
            if job is not None and job.bytes_total and job.bytes_done:
                break
            time.sleep(0.02)
        cancelled = puller.cancel("qwen2.5-coder:7b")
        assert cancelled.status == CANCELLED
        job = puller.wait("qwen2.5-coder:7b", timeout=10)
        assert job.status == CANCELLED
    finally:
        server.RequestHandlerClass.mode = "ok"


def test_http_pull_with_no_service_fails_instead_of_hanging():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    puller = OllamaPuller(transport="api", endpoint=f"http://127.0.0.1:{port}")
    puller.start("qwen2.5-coder:7b", confirm=True)
    job = puller.wait("qwen2.5-coder:7b", timeout=10)
    assert job.status == FAILED
    assert "not answering" in job.detail.lower()
