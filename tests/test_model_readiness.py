"""Model-readiness tests.

llama-server answers ``/health`` with 200 while the weights are still
loading, so a request sent at that moment fails with HTTP 503
``{"error":{"message":"Loading model"}}``. Readiness therefore has to
wait for a real one-token completion, not just a healthy endpoint.

These tests drive ``SubprocessLlamaCppRuntime`` against an in-process
fake HTTP server that reproduces the real server's behaviour: health
ready early, completions returning 503 for a while, then 200.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
import time

import pytest

from giscode.inference.errors import InferenceError
from giscode.inference.runtime import (
    ChatMessage,
    ChatRequest,
    SubprocessLlamaCppRuntime,
)


class _FakeLlamaState:
    def __init__(
        self,
        health_failures: int = 0,
        completion_failures: int = 0,
        chat_failures: int = 0,
    ) -> None:
        self.health_failures = health_failures
        self.completion_failures = completion_failures
        self.chat_failures = chat_failures
        self.health_calls = 0
        self.completion_calls = 0
        self.chat_calls = 0
        self.lock = threading.Lock()


class _FakeLlamaHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence request logging
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        state = self.server.state  # type: ignore[attr-defined]
        with state.lock:
            state.health_calls += 1
            attempt = state.health_calls
        if self.path == "/health":
            if attempt <= state.health_failures:
                self._send(503, {"error": {"message": "Loading model"}})
            else:
                self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        state = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        if self.path == "/v1/completions":
            with state.lock:
                state.completion_calls += 1
                attempt = state.completion_calls
            if attempt <= state.completion_failures:
                self._send(503, {"error": {"message": "Loading model"}})
            else:
                self._send(200, {
                    "choices": [{"text": "x", "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 1},
                })
        elif self.path == "/v1/chat/completions":
            with state.lock:
                state.chat_calls += 1
                attempt = state.chat_calls
            if attempt <= state.chat_failures:
                self._send(503, {"error": {"message": "Loading model"}})
            else:
                self._send(200, {
                    "choices": [{
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }],
                    "usage": {"completion_tokens": 1},
                })
        else:
            self._send(404, {"error": {"message": "not found"}})


class _FakeLlamaServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextlib.contextmanager
def fake_llama_server(state: _FakeLlamaState):
    server = _FakeLlamaServer(("127.0.0.1", 0), _FakeLlamaHandler)
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _runtime(port: int, ready_timeout: float = 15.0) -> SubprocessLlamaCppRuntime:
    rt = SubprocessLlamaCppRuntime(
        server_path="C:/nonexistent/llama-server.exe",
        n_ctx=1024,
        n_threads=2,
        n_gpu_layers=0,
        ready_timeout=ready_timeout,
    )
    rt._server_port = port
    return rt


def test_wait_ready_waits_for_completion_probe_not_just_health():
    """Health is 200 immediately but completions 503 five times: readiness
    must not report success until a real completion has been served."""
    state = _FakeLlamaState(health_failures=0, completion_failures=5)
    with fake_llama_server(state) as port:
        rt = _runtime(port, ready_timeout=20.0)
        started = time.time()
        ready = rt._wait_ready(20.0)
        elapsed = time.time() - started
    assert ready is True
    assert state.completion_calls >= 6
    assert elapsed >= 1.0, "should have retried ~5 times at 250ms intervals"


def test_wait_ready_times_out_when_completion_always_503():
    """A server whose completions never succeed must time out, not hang."""
    state = _FakeLlamaState(health_failures=0, completion_failures=10**6)
    with fake_llama_server(state) as port:
        rt = _runtime(port, ready_timeout=1.5)
        started = time.time()
        ready = rt._wait_ready(1.5)
        elapsed = time.time() - started
    assert ready is False
    assert elapsed < 10.0, f"timeout took too long: {elapsed:.1f}s"


def test_first_chat_request_retries_503_until_ready():
    """The first real request must survive a run of 503s and land on 200."""
    state = _FakeLlamaState(chat_failures=3)
    with fake_llama_server(state) as port:
        rt = _runtime(port, ready_timeout=15.0)
        response = rt.chat_generate(ChatRequest(
            messages=[ChatMessage(role="user", content="hi")],
            max_tokens=1,
        ))
    assert response.text == "ok"
    assert state.chat_calls == 4


def test_first_chat_request_times_out_when_never_ready():
    """If the model never becomes ready the request fails with a clear
    timeout instead of returning the 503 body as an error string."""
    state = _FakeLlamaState(chat_failures=10**6)
    with fake_llama_server(state) as port:
        rt = _runtime(port, ready_timeout=1.5)
        with pytest.raises(InferenceError):
            rt.chat_generate(ChatRequest(
                messages=[ChatMessage(role="user", content="hi")],
                max_tokens=1,
            ))
    assert state.chat_calls >= 2, "should have retried before giving up"


def test_wait_ready_waits_through_unhealthy_health():
    """While the socket is not bound yet /health refuses connections."""
    state = _FakeLlamaState(health_failures=4, completion_failures=0)
    with fake_llama_server(state) as port:
        rt = _runtime(port, ready_timeout=20.0)
        ready = rt._wait_ready(20.0)
    assert ready is True
