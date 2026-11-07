"""Focused tests for server-reported token counts on the chat path.

llama-server only reports usage in the final stream chunk when the request
carries ``stream_options.include_usage``; without it ``tokens_generated``
stayed 0 on the (always streaming) API path. These tests stub the HTTP
transport and cover:
- the streaming payload carries stream_options.include_usage
- usage.completion_tokens from the final SSE chunk becomes tokens_generated
- the sync (non-streaming) path still parses usage.completion_tokens
- streamed text is assembled from content deltas
"""
import json
import threading

from giscode.inference.runtime import (
    ChatMessage,
    ChatRequest,
    SubprocessLlamaCppRuntime,
)


class _FakeStreamResponse:
    def __init__(self, sse_lines: list[bytes], status: int = 200):
        self._lines = sse_lines
        self.status = status

    def read(self) -> bytes:
        return b"".join(self._lines)

    def read1(self, _n: int = -1) -> bytes:
        # The production reader drains a blocking HTTPResponse with read1();
        # hand back one buffered SSE line per call and b"" at the end.
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeSyncResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _sse_chunk(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n"


def _runtime_with_running_server() -> SubprocessLlamaCppRuntime:
    runtime = SubprocessLlamaCppRuntime(server_path="C:/fake/llama-server.exe")
    fake_process = type("FakeProcess", (), {"poll": lambda self=None: None})()
    runtime._process = fake_process
    runtime._server_port = 8765
    runtime._model_path = "C:/fake/model.gguf"
    runtime._model_ready = True
    return runtime


class _StreamingURLLib:
    """urllib.request.urlopen stand-in returning an SSE stream and recording
    the request body."""

    def __init__(self, sse_lines: list[bytes]):
        self._sse_lines = sse_lines
        self.requests: list = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
        if "/health" in url:
            return _FakeSyncResponse({"status": "ok"})
        return _FakeStreamResponse(list(self._sse_lines))


class _SyncURLLib:
    def __init__(self, payload: dict):
        self._payload = payload
        self.requests: list = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
        if "/health" in url:
            return _FakeSyncResponse({"status": "ok"})
        return _FakeSyncResponse(self._payload)


def test_streaming_chat_sends_include_usage_and_parses_tokens(monkeypatch):
    sse = [
        _sse_chunk({"choices": [{"delta": {"content": "Hello"}}]}),
        _sse_chunk({"choices": [{"delta": {"content": " world"}}]}),
        _sse_chunk({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        _sse_chunk({"choices": [], "usage": {"completion_tokens": 7,
                                             "prompt_tokens": 12}}),
        b"data: [DONE]\n\n",
    ]
    fake = _StreamingURLLib(sse)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    runtime = _runtime_with_running_server()
    request = ChatRequest(messages=[ChatMessage(role="user", content="Hi")], max_tokens=16)
    response = runtime.chat_generate(request, cancel_event=threading.Event())

    assert response.text == "Hello world"
    assert response.tokens_generated == 7
    assert response.finish_reason == "stop"

    chat_posts = [r for r in fake.requests
                  if hasattr(r, "get_full_url") and "/v1/chat/completions" in r.get_full_url()]
    assert len(chat_posts) == 1
    sent = json.loads(chat_posts[0].data.decode("utf-8"))
    assert sent["stream"] is True
    assert sent.get("stream_options") == {"include_usage": True}


def test_streaming_tokens_zero_when_server_omits_usage(monkeypatch):
    sse = [
        _sse_chunk({"choices": [{"delta": {"content": "ok"}}]}),
        _sse_chunk({"choices": [{"delta": {}, "finish_reason": "length"}]}),
        b"data: [DONE]\n\n",
    ]
    fake = _StreamingURLLib(sse)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    runtime = _runtime_with_running_server()
    request = ChatRequest(messages=[ChatMessage(role="user", content="Hi")], max_tokens=16)
    response = runtime.chat_generate(request, cancel_event=threading.Event())
    assert response.text == "ok"
    assert response.tokens_generated == 0
    assert response.finish_reason == "length"


def test_sync_chat_parses_usage_completion_tokens(monkeypatch):
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "Hi there"},
                     "finish_reason": "stop"}],
        "usage": {"completion_tokens": 5, "prompt_tokens": 10},
    }
    fake = _SyncURLLib(payload)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    runtime = _runtime_with_running_server()
    request = ChatRequest(messages=[ChatMessage(role="user", content="Hi")], max_tokens=16)
    response = runtime.chat_generate(request)
    assert response.text == "Hi there"
    assert response.tokens_generated == 5
    chat_posts = [r for r in fake.requests
                  if hasattr(r, "get_full_url") and "/v1/chat/completions" in r.get_full_url()]
    sent = json.loads(chat_posts[0].data.decode("utf-8"))
    assert sent["stream"] is False
