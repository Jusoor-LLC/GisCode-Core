"""Focused unit tests for the new /v1/chat/completions path.

These tests stub out the HTTP transport so they exercise:
- correct endpoint URL
- correct JSON payload (messages + max_tokens + temperature + stream + stop)
- response text extraction
- error handling
- backward compatibility of the existing /completion path
"""
import io
import json
import urllib.error

import pytest

from giscode.inference.api import InferenceAPI
from giscode.inference.errors import InferenceError, InferenceUnavailableError
from giscode.inference.runtime import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    FileSystemModelDiscovery,
    GenerationRequest,
    LlamaCppPythonRuntime,
    SubprocessLlamaCppRuntime,
)


class _FakeHTTPResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _CapturingURLLib:
    """Drop-in for ``urllib.request.urlopen`` that records the request and
    returns a configured response payload.
    """

    def __init__(self, response_payload: dict = None, error: Exception = None, status: int = 200):
        self.response_payload = response_payload or {}
        self.error = error
        self.status = status
        self.requests: list = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self.error is not None:
            raise self.error
        # Handle health check requests - return 200 OK
        url = req.get_full_url() if hasattr(req, 'get_full_url') else str(req)
        if "/health" in url:
            return _FakeHTTPResponse({"status": "ok"}, status=200)
        return _FakeHTTPResponse(self.response_payload, status=self.status)


def _make_subprocess_runtime_with_running_server(monkeypatch) -> SubprocessLlamaCppRuntime:
    runtime = SubprocessLlamaCppRuntime(server_path="C:/fake/llama-server.exe")
    fake_process = type("FakeProcess", (), {"poll": lambda self=None: None})()
    runtime._process = fake_process
    runtime._server_port = 8765
    runtime._model_path = "C:/fake/model.gguf"
    return runtime


CHAT_URL = "http://127.0.0.1:8765/v1/chat/completions"
COMPLETION_URL = "http://127.0.0.1:8765/completion"


def _request_to(fake, url: str):
    """Return the recorded request whose full URL equals *url*.

    Readiness gating precedes the first chat/completion with health polls and
    a completion probe, so the request under test is located by URL instead of
    by list position (the leading entries are health probes, not Request
    objects).
    """
    for req, _timeout in fake.requests:
        if hasattr(req, "get_full_url") and req.get_full_url() == url:
            return req
    raise AssertionError(f"no request to {url!r}; recorded: {fake.requests!r}")


def _count_requests_to(fake, url: str) -> int:
    return sum(
        1
        for req, _timeout in fake.requests
        if hasattr(req, "get_full_url") and req.get_full_url() == url
    )


def _temp_gguf() -> str:
    import tempfile
    from pathlib import Path

    f = tempfile.NamedTemporaryFile(suffix=".gguf", delete=False)
    f.write(b"fake model")
    f.close()
    return f.name


def test_chat_request_to_payload_includes_messages_and_sampling():
    req = ChatRequest(
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hi"),
            ChatMessage(role="assistant", content="Hello! How can I help?"),
            ChatMessage(role="user", content="Tell me a joke."),
        ],
        max_tokens=128,
        temperature=0.3,
        stop=["</s>"],
        request_id="abc-1",
    )
    payload = req.to_payload()
    assert payload["messages"] == [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello! How can I help?"},
        {"role": "user", "content": "Tell me a joke."},
    ]
    assert payload["max_tokens"] == 128
    assert payload["temperature"] == 0.3
    assert payload["stream"] is False
    assert payload["stop"] == ["</s>"]


def test_chat_request_payload_defaults():
    req = ChatRequest(messages=[ChatMessage(role="user", content="Hi")])
    payload = req.to_payload()
    assert payload["max_tokens"] == 512
    assert payload["temperature"] == 0.7
    assert payload["stream"] is False
    assert payload["stop"] == []


def test_subprocess_chat_generate_uses_v1_chat_completions_endpoint(monkeypatch):
    runtime = _make_subprocess_runtime_with_running_server(monkeypatch)
    fake = _CapturingURLLib(response_payload={
        "id": "chatcmpl-1",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "Hello there!"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    })
    monkeypatch.setattr("urllib.request.urlopen", fake)

    req = ChatRequest(
        messages=[
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Hi"),
        ],
        max_tokens=64,
        temperature=0.2,
    )
    response = runtime.chat_generate(req)

    assert _count_requests_to(fake, CHAT_URL) == 1
    sent_req = _request_to(fake, CHAT_URL)
    assert sent_req.get_full_url() == CHAT_URL
    assert sent_req.get_method() == "POST"
    assert sent_req.get_header("Content-type") == "application/json"
    body = json.loads(sent_req.data.decode("utf-8"))
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"] == "You are helpful."
    assert body["messages"][1]["role"] == "user"
    assert body["messages"][1]["content"] == "Hi"
    assert body["max_tokens"] == 64
    assert body["temperature"] == 0.2
    assert body["stream"] is False

    assert isinstance(response, ChatResponse)
    assert response.text == "Hello there!"
    assert response.tokens_generated == 3
    assert response.finish_reason == "stop"


def test_subprocess_chat_generate_preserves_assistant_history(monkeypatch):
    runtime = _make_subprocess_runtime_with_running_server(monkeypatch)
    fake = _CapturingURLLib(response_payload={
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    })
    monkeypatch.setattr("urllib.request.urlopen", fake)

    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="u1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="user", content="u2"),
        ChatMessage(role="assistant", content="a2"),
        ChatMessage(role="user", content="u3"),
    ]
    runtime.chat_generate(ChatRequest(messages=messages))

    sent_req = _request_to(fake, CHAT_URL)
    body = json.loads(sent_req.data.decode("utf-8"))
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert [m["content"] for m in body["messages"]] == ["sys", "u1", "a1", "u2", "a2", "u3"]


def test_subprocess_chat_generate_extracts_text_from_message_content(monkeypatch):
    runtime = _make_subprocess_runtime_with_running_server(monkeypatch)
    fake = _CapturingURLLib(response_payload={
        "choices": [{"message": {"role": "assistant", "content": "How are you? I'm doing great."}}],
    })
    monkeypatch.setattr("urllib.request.urlopen", fake)
    resp = runtime.chat_generate(ChatRequest(messages=[ChatMessage(role="user", content="Hi")]))
    assert resp.text == "How are you? I'm doing great."


def test_subprocess_chat_generate_handles_empty_choices(monkeypatch):
    runtime = _make_subprocess_runtime_with_running_server(monkeypatch)
    fake = _CapturingURLLib(response_payload={"choices": []})
    monkeypatch.setattr("urllib.request.urlopen", fake)
    resp = runtime.chat_generate(ChatRequest(messages=[ChatMessage(role="user", content="Hi")]))
    assert resp.text == ""


def test_subprocess_chat_generate_falls_back_to_top_level_content(monkeypatch):
    runtime = _make_subprocess_runtime_with_running_server(monkeypatch)
    fake = _CapturingURLLib(response_payload={"content": "flat response"})
    monkeypatch.setattr("urllib.request.urlopen", fake)
    resp = runtime.chat_generate(ChatRequest(messages=[ChatMessage(role="user", content="Hi")]))
    assert resp.text == "flat response"


#: A port that only this test file uses. ``SubprocessLlamaCppRuntime`` defaults
#: to 8080, and ``_is_server_running()`` probes its port over HTTP before it
#: looks at the child process - so on a machine where a real llama-server is
#: listening on 8080 the runtime below is "running" and the two tests that
#: expect "not running" invert. That is exactly what happened on 2026-10-27,
#: with six orphaned servers up and one of them holding 8080. Never use the
#: runtime default here.
UNUSED_PORT = 8766


def _url_of(request) -> str:
    """The URL a captured urlopen argument points at (str or Request)."""
    return request.get_full_url() if hasattr(request, "get_full_url") else str(request)


def _runtime_with_no_reachable_server(monkeypatch, process):
    """A subprocess runtime whose server cannot be reached, on any machine.

    The transport is faked, so no socket is opened and nothing depends on what
    the machine happens to be listening on.
    """
    runtime = SubprocessLlamaCppRuntime(server_path="C:/fake/llama-server.exe")
    runtime._process = process
    runtime._server_port = UNUSED_PORT
    fake = _CapturingURLLib(error=urllib.error.URLError("connection refused"))
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return runtime, fake


def test_subprocess_chat_generate_raises_when_server_not_running(monkeypatch):
    runtime, fake = _runtime_with_no_reachable_server(monkeypatch, process=None)
    with pytest.raises(InferenceError) as exc:
        runtime.chat_generate(ChatRequest(messages=[ChatMessage(role="user", content="Hi")]))
    assert "not running" in str(exc.value).lower()
    # Only this file's port was consulted: a real server on 8080 (the runtime
    # default) can neither make this pass nor make it fail.
    assert fake.requests, "the health probe was never attempted"
    assert all(_url_of(req).startswith(f"http://127.0.0.1:{UNUSED_PORT}/") for req, _ in fake.requests)
    assert not any("8080" in _url_of(req) for req, _ in fake.requests)


def test_subprocess_chat_generate_raises_when_process_died(monkeypatch):
    dead_process = type("DeadProcess", (), {"poll": lambda self=None: 1})()
    runtime, fake = _runtime_with_no_reachable_server(monkeypatch, process=dead_process)
    with pytest.raises(InferenceError) as exc:
        runtime.chat_generate(ChatRequest(messages=[ChatMessage(role="user", content="Hi")]))
    assert "not running" in str(exc.value).lower()
    assert not any("8080" in _url_of(req) for req, _ in fake.requests)


def test_subprocess_chat_generate_raises_on_empty_messages():
    runtime = _make_subprocess_runtime_with_running_server(None)
    with pytest.raises(InferenceError):
        runtime.chat_generate(ChatRequest(messages=[]))


def test_subprocess_chat_generate_wraps_url_errors(monkeypatch):
    """A refused connection means nothing is listening.

    The readiness gate must not wait out the whole timeout for a server that
    is not there: the request fails immediately, in the "chat request failed"
    wording, instead of reporting a 60 s readiness timeout.
    """
    runtime = _make_subprocess_runtime_with_running_server(monkeypatch)
    fake = _CapturingURLLib(error=urllib.error.URLError("connection refused"))
    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(InferenceError) as exc:
        runtime.chat_generate(ChatRequest(messages=[ChatMessage(role="user", content="Hi")]))
    assert "chat request failed" in str(exc.value).lower()
    assert "connection refused" in str(exc.value).lower()
    # Fail fast: no 60 s readiness wait was burned on a server that is not there.
    assert "not ready" not in str(exc.value).lower()


def test_subprocess_generate_completion_path_is_unchanged(monkeypatch):
    """The existing /completion endpoint must still be used by generate()."""
    runtime = _make_subprocess_runtime_with_running_server(monkeypatch)
    fake = _CapturingURLLib(response_payload={"content": "completion response"})
    monkeypatch.setattr("urllib.request.urlopen", fake)

    response = runtime.generate(GenerationRequest(prompt="Hello"))
    assert response.text == "completion response"
    assert _count_requests_to(fake, COMPLETION_URL) == 1
    sent_req = _request_to(fake, COMPLETION_URL)
    assert sent_req.get_full_url() == COMPLETION_URL
    body = json.loads(sent_req.data.decode("utf-8"))
    assert body["prompt"] == "Hello"
    assert "messages" not in body


def test_subprocess_chat_generate_uses_default_port_when_arg_matches_8080(monkeypatch):
    runtime = _make_subprocess_runtime_with_running_server(monkeypatch)
    fake = _CapturingURLLib(response_payload={"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr("urllib.request.urlopen", fake)
    runtime.chat_generate(ChatRequest(messages=[ChatMessage(role="user", content="Hi")]), port=8080)
    sent_req = _request_to(fake, CHAT_URL)
    assert sent_req.get_full_url() == CHAT_URL


def test_inference_api_chat_generate_requires_loaded_model():
    api = InferenceAPI(runtime=_StubRuntime(available=True, supports_chat=True))
    with pytest.raises(InferenceUnavailableError):
        api.chat_generate(ChatRequest(messages=[ChatMessage(role="user", content="Hi")]))


def test_inference_api_chat_generate_rejects_unsupported_runtime():
    api = InferenceAPI(runtime=_StubRuntime(available=True, supports_chat=False))
    import tempfile
    from pathlib import Path
    f = tempfile.NamedTemporaryFile(suffix=".gguf", delete=False)
    f.write(b"fake")
    f.close()
    try:
        api.load_model(f.name)
        with pytest.raises(InferenceError):
            api.chat_generate(ChatRequest(messages=[ChatMessage(role="user", content="Hi")]))
    finally:
        try:
            api.unload_model()
        except Exception:
            pass
        Path(f.name).unlink()


def test_inference_api_chat_generate_with_real_runtime(monkeypatch):
    """InferenceAPI.chat_generate wires through to the runtime."""
    import tempfile
    from pathlib import Path

    f = tempfile.NamedTemporaryFile(suffix=".gguf", delete=False)
    f.write(b"fake model")
    f.close()
    model_path = f.name

    runtime = _RecordingChatRuntime(available=True)
    api = InferenceAPI(runtime=runtime, discovery=FileSystemModelDiscovery([]))
    try:
        api.load_model(model_path)
        resp = api.chat_generate(ChatRequest(
            messages=[
                ChatMessage(role="system", content="be concise"),
                ChatMessage(role="user", content="Hi"),
            ],
            max_tokens=32,
        ))
        assert resp.text == "ok"
        assert runtime.last_messages == [
            {"role": "system", "content": "be concise"},
            {"role": "user", "content": "Hi"},
        ]
        assert api.get_model_status().state.name == "READY"
    finally:
        try:
            api.unload_model()
        except Exception:
            pass
        Path(model_path).unlink()


def test_chat_generate_does_not_break_generate(monkeypatch):
    """The new chat_generate must not affect the existing generate() path."""
    import tempfile
    from pathlib import Path

    f = tempfile.NamedTemporaryFile(suffix=".gguf", delete=False)
    f.write(b"fake model")
    f.close()
    model_path = f.name

    runtime = _RecordingChatRuntime(available=True)
    api = InferenceAPI(runtime=runtime, discovery=FileSystemModelDiscovery([]))
    try:
        api.load_model(model_path)
        resp = api.generate(GenerationRequest(prompt="legacy prompt"))
        assert resp.text == "ok-legacy"
        assert runtime.last_legacy_prompt == "legacy prompt"
    finally:
        try:
            api.unload_model()
        except Exception:
            pass
        Path(model_path).unlink()


def test_llama_cpp_python_runtime_does_not_implement_chat_generate_by_default():
    """Backward compatibility: the default base-class implementation raises."""
    runtime = LlamaCppPythonRuntime()
    with pytest.raises(InferenceError):
        runtime.chat_generate(ChatRequest(messages=[ChatMessage(role="user", content="Hi")]))


class _StubRuntime:
    """Minimal runtime stub that reports availability and (optionally) chat support."""

    def __init__(self, available: bool = True, supports_chat: bool = True):
        from giscode.inference.runtime import GenerationRequest, GenerationResponse
        self._available = available
        self._supports_chat = supports_chat
        self._running = False
        self._GenerationRequest = GenerationRequest
        self._GenerationResponse = GenerationResponse

    def is_available(self) -> bool:
        return self._available

    def start_server(self, model_path: str, port: int = 8080, n_ctx: int = None, n_threads: int = None, n_gpu_layers: int = None, mmproj_path: str = None) -> bool:
        self._running = True
        return True

    def stop_server(self) -> None:
        self._running = False

    def generate(self, request, port: int = 8080):
        return self._GenerationResponse(text="ok-legacy")

    def is_running(self, port: int = 8080) -> bool:
        return self._running

    def chat_generate(self, request, port: int = 8080):
        if not self._supports_chat:
            raise InferenceError("chat not supported")
        from giscode.inference.runtime import ChatResponse
        return ChatResponse(text="ok")


class _RecordingChatRuntime(_StubRuntime):
    def __init__(self, available: bool = True):
        super().__init__(available=available, supports_chat=True)
        from giscode.inference.runtime import GenerationRequest, GenerationResponse, ChatResponse
        self._GenerationRequest = GenerationRequest
        self._GenerationResponse = GenerationResponse
        self._ChatResponse = ChatResponse
        self.last_messages = None
        self.last_legacy_prompt = None

    def generate(self, request, port: int = 8080):
        self.last_legacy_prompt = request.prompt
        return self._GenerationResponse(text="ok-legacy")

    def chat_generate(self, request, port: int = 8080):
        self.last_messages = [m.to_dict() for m in request.messages]
        return self._ChatResponse(text="ok")
