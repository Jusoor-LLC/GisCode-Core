"""Streaming and cancellation for provider-backed inference.

Every test here talks to a real local HTTP server on loopback - no paid service,
no API key, no remote endpoint. The server streams from a script, so chunk
boundaries, malformed events, premature closes and stalls are deterministic
rather than timing luck.

What is being pinned down:

* text arrives incrementally, in order, exactly once, even when a protocol
  message is split across network chunks;
* each provider's own framing is decoded (Ollama NDJSON, OpenAI-shaped SSE);
* an error, an empty body and a premature close are failures, never an empty
  successful answer;
* a Stop closes the connection and keeps the partial text, labelled as partial;
* a Stop can only affect the request it names, and a new request is unaffected
  by a cancellation that belonged to the previous one.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from giscode.agentic import model_chat
from giscode.inference.errors import (
    InferenceTimeoutError,
    InferenceUnavailableError,
    RequestCancelledError,
)
from giscode.inference.provider_backend import ProviderChatBackend
from giscode.inference.runtime import ChatMessage, ChatRequest
from giscode.providers.lmstudio import LMStudioProvider
from giscode.providers.ollama import OllamaProvider
from giscode.providers.openai_compat import OpenAICompatibleProvider


class _ProviderServer:
    """A loopback provider whose streaming body comes from ``responder``."""

    def __init__(self, responder, models=("m",), *, header_delay: float = 0.0):
        self.responder = responder
        self.models = tuple(models)
        self.header_delay = float(header_delay)
        self.post_seen = threading.Event()
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):  # keep pytest output clean
                return

            def _json(self, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.endswith("/v1/models"):
                    self._json({"data": [{"id": name} for name in outer.models]})
                elif self.path.endswith("/api/tags"):
                    self._json({"models": [{"name": name} for name in outer.models]})
                elif self.path.endswith("/api/version"):
                    self._json({"version": "test"})
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = {}
                outer.requests.append({"path": self.path, "payload": payload})
                outer.post_seen.set()
                if outer.header_delay:
                    time.sleep(outer.header_delay)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    outer.responder(payload, self.wfile, len(outer.requests))
                except (BrokenPipeError, ConnectionResetError, OSError):
                    # The client closed the socket: exactly what cancellation
                    # is supposed to do.
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _sse(wfile, obj) -> None:
    wfile.write(f"data: {json.dumps(obj)}\n\n".encode("utf-8"))
    wfile.flush()


def _ndjson(wfile, obj) -> None:
    wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
    wfile.flush()


def _request(request_id: str = "r1") -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="user", content="hello")],
        max_tokens=16,
        temperature=0.0,
        request_id=request_id,
    )


def _collect(backend, request, **kwargs):
    """Drain a stream, returning ``(pieces, final_finish, final_tokens)``."""
    pieces: list[str] = []
    finish = None
    tokens = None
    for piece, reason, count in backend.chat_generate_stream(request, **kwargs):
        if piece:
            pieces.append(piece)
        if reason:
            finish = reason
        if isinstance(count, int):
            tokens = count
    return pieces, finish, tokens


# --------------------------------------------------------------------------- SSE


def test_sse_text_is_incremental_ordered_and_not_duplicated():
    def responder(payload, wfile, index):
        raw = (
            b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"lo "}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"world"},'
            b'"finish_reason":"stop"}],"usage":{"completion_tokens":3}}\n\n'
            b"data: [DONE]\n\n"
        )
        # Deliberately split inside a protocol line and pack two events
        # into one write: both are what a real socket does.
        for part in (raw[:17], raw[17:60], raw[60:200], raw[200:]):
            wfile.write(part)
            wfile.flush()
            time.sleep(0.005)

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        pieces, finish, tokens = _collect(backend, _request())

    assert pieces == ["Hel", "lo ", "world"]
    assert "".join(pieces) == "Hello world"
    assert finish == "stop"
    assert tokens == 3
    statuses = [step for step in server.requests if step["path"].endswith("/chat/completions")]
    assert statuses and statuses[0]["payload"]["stream"] is True


def test_sse_finish_reason_and_usage_are_reported_verbatim():
    def responder(payload, wfile, index):
        _sse(wfile, {"choices": [{"delta": {"content": "x"}, "finish_reason": "length"}]})
        _sse(wfile, {"choices": [{"delta": {}, "finish_reason": "length"}], "usage": {"completion_tokens": 7}})
        wfile.write(b"data: [DONE]\n\n")
        wfile.flush()

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        pieces, finish, tokens = _collect(backend, _request())

    assert pieces == ["x"]
    assert finish == "length"
    assert tokens == 7


def test_openai_compatible_provider_streams_the_same_protocol():
    def responder(payload, wfile, index):
        _sse(wfile, {"choices": [{"delta": {"content": "ok"}}]})
        _sse(wfile, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        wfile.write(b"data: [DONE]\n\n")
        wfile.flush()

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(OpenAICompatibleProvider(server.endpoint), "m")
        pieces, finish, _tokens = _collect(backend, _request())

    assert pieces == ["ok"]
    assert finish == "stop"


def test_malformed_event_is_skipped_when_valid_events_follow():
    def responder(payload, wfile, index):
        wfile.write(b"data: {this is not json}\n\n")
        _sse(wfile, {"choices": [{"delta": {"content": "kept"}}]})
        _sse(wfile, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        wfile.write(b"data: [DONE]\n\n")
        wfile.flush()

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        pieces, finish, _tokens = _collect(backend, _request())

    assert pieces == ["kept"]
    assert finish == "stop"


def test_a_stream_of_only_garbage_is_an_error_not_an_empty_answer():
    def responder(payload, wfile, index):
        wfile.write(b"data: not json at all\n\n")
        wfile.flush()

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        with pytest.raises(InferenceUnavailableError):
            _collect(backend, _request())


def test_premature_close_without_a_completion_signal_is_an_error():
    def responder(payload, wfile, index):
        _sse(wfile, {"choices": [{"delta": {"content": "half"}}]})
        # No finish_reason, no [DONE]: the server just goes away.

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        with pytest.raises(InferenceUnavailableError) as excinfo:
            _collect(backend, _request())

    assert "before the answer" in str(excinfo.value) or "complete" in str(excinfo.value)


def test_an_empty_stream_is_an_error():
    def responder(payload, wfile, index):
        return None

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        with pytest.raises(InferenceUnavailableError):
            _collect(backend, _request())


def test_provider_error_event_surfaces_as_a_failure():
    def responder(payload, wfile, index):
        _sse(wfile, {"error": {"message": "model crashed"}})

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        with pytest.raises(InferenceUnavailableError) as excinfo:
            _collect(backend, _request())

    assert "model crashed" in str(excinfo.value)


# ------------------------------------------------------------------------ Ollama


def test_ollama_ndjson_stream_decodes_content_and_counts():
    def responder(payload, wfile, index):
        raw = (
            b'{"message":{"content":"A"},"done":false}\n'
            b'{"message":{"content":"B"},"done":false}\n'
            b'{"done":true,"done_reason":"stop","eval_count":2,"prompt_eval_count":5}\n'
        )
        for part in (raw[:9], raw[9:44], raw[44:]):
            wfile.write(part)
            wfile.flush()
            time.sleep(0.005)

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(OllamaProvider(server.endpoint), "m")
        pieces, finish, tokens = _collect(backend, _request())

    assert "".join(pieces) == "AB"
    assert finish == "stop"
    assert tokens == 2


def test_ollama_error_object_is_a_failure():
    def responder(payload, wfile, index):
        _ndjson(wfile, {"error": "model not found"})

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(OllamaProvider(server.endpoint), "m")
        with pytest.raises(InferenceUnavailableError) as excinfo:
            _collect(backend, _request())

    assert "model not found" in str(excinfo.value)


def test_ollama_stream_without_done_is_an_error():
    def responder(payload, wfile, index):
        _ndjson(wfile, {"message": {"content": "partial"}, "done": False})

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(OllamaProvider(server.endpoint), "m")
        with pytest.raises(InferenceUnavailableError):
            _collect(backend, _request())


# ------------------------------------------------------------------ cancellation


def _endless_responder(payload, wfile, index):
    """Emit deltas forever until the client closes the socket."""
    for step in range(2000):
        _sse(wfile, {"choices": [{"delta": {"content": f"{step},"}}]})
        time.sleep(0.005)


def test_cancel_mid_stream_closes_the_connection_and_keeps_partial_text():
    with _ProviderServer(_endless_responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        received: list[str] = []
        failure: dict = {}

        def _consume():
            try:
                for piece, _reason, _tokens in backend.chat_generate_stream(_request("r1")):
                    if piece:
                        received.append(piece)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                failure["exc"] = exc

        thread = threading.Thread(target=_consume, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while not received and time.time() < deadline:
            time.sleep(0.01)
        assert received, "the stream never produced a delta"

        assert backend.cancel("r1") is True
        thread.join(5)
        assert not thread.is_alive(), "cancellation left the reader running"

    error = failure.get("exc")
    assert isinstance(error, RequestCancelledError)
    assert error.partial_text, "the partial answer was not preserved"


def test_cancel_for_another_request_is_ignored():
    def responder(payload, wfile, index):
        for word in ("a", "b", "c"):
            _sse(wfile, {"choices": [{"delta": {"content": word}}]})
            time.sleep(0.01)
        _sse(wfile, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        wfile.write(b"data: [DONE]\n\n")
        wfile.flush()

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        received: list[str] = []

        def _consume():
            for piece, _reason, _tokens in backend.chat_generate_stream(_request("mine")):
                if piece:
                    received.append(piece)

        thread = threading.Thread(target=_consume, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while not received and time.time() < deadline:
            time.sleep(0.01)
        assert backend.cancel("someone-else") is False
        thread.join(5)

    assert "".join(received) == "abc"


def test_cancel_before_the_request_starts_touches_no_connection():
    def responder(payload, wfile, index):  # pragma: no cover - must not run
        _sse(wfile, {"choices": [{"delta": {"content": "unexpected"}}]})

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        backend.cancel()
        with pytest.raises(RequestCancelledError):
            _collect(backend, _request())
        posts = [step for step in server.requests if step["path"].endswith("/chat/completions")]

    assert posts == []


def test_a_new_request_after_a_cancel_completes_normally():
    def responder(payload, wfile, index):
        if index == 1:
            _endless_responder(payload, wfile, index)
            return
        _sse(wfile, {"choices": [{"delta": {"content": "second"}}]})
        _sse(wfile, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        wfile.write(b"data: [DONE]\n\n")
        wfile.flush()

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        got: list[str] = []
        failure: dict = {}

        def _consume():
            try:
                for piece, _reason, _tokens in backend.chat_generate_stream(_request("first")):
                    if piece:
                        got.append(piece)
            except BaseException as exc:  # noqa: BLE001
                failure["exc"] = exc

        thread = threading.Thread(target=_consume, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while not got and time.time() < deadline:
            time.sleep(0.01)
        backend.cancel("first")
        thread.join(5)

        pieces, finish, _tokens = _collect(backend, _request("second"))

    assert isinstance(failure.get("exc"), RequestCancelledError)
    assert "".join(pieces) == "second"
    assert finish == "stop"


def test_repeated_cancel_is_safe():
    with _ProviderServer(_endless_responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        received: list[str] = []
        failure: dict = {}

        def _consume():
            try:
                for piece, _reason, _tokens in backend.chat_generate_stream(_request("r1")):
                    if piece:
                        received.append(piece)
            except BaseException as exc:  # noqa: BLE001
                failure["exc"] = exc

        thread = threading.Thread(target=_consume, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while not received and time.time() < deadline:
            time.sleep(0.01)
        for _ in range(3):
            backend.cancel("r1")
        thread.join(5)

    assert isinstance(failure.get("exc"), RequestCancelledError)


def test_idle_provider_becomes_a_timeout_with_partial_text():
    def responder(payload, wfile, index):
        _sse(wfile, {"choices": [{"delta": {"content": "partial"}}]})
        time.sleep(1.2)

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m", timeout=0.4)
        with pytest.raises(InferenceTimeoutError) as excinfo:
            # ``read_timeout`` is the caller's inactivity budget for the stream.
            _collect(backend, _request(), read_timeout=0.4)

    assert "partial" in excinfo.value.partial_text


# ------------------------------------------------------- privacy stays enforced


def test_remote_image_is_refused_before_the_stream_is_opened():
    with _ProviderServer(lambda payload, wfile, index: None) as server:
        provider = LMStudioProvider(server.endpoint, allow_remote=True)
        # Pretend the endpoint is off this machine: consent is a separate
        # decision from "allow remote", and an image must not leave the PC.
        provider.endpoint = "http://10.255.255.1:1234"
        backend = ProviderChatBackend(provider, "m", attachment_consent=False)
        request = ChatRequest(
            messages=[ChatMessage(role="user", content="look")],
            images=[__file__],
            request_id="img",
        )
        with pytest.raises(InferenceUnavailableError) as excinfo:
            _collect(backend, request)
        posts = [step for step in server.requests if step["path"].endswith("/chat/completions")]

    assert "Nothing left this computer" in str(excinfo.value)
    assert posts == []


# ------------------------------------------------------------- chat integration


class _FakeRouter:
    def __init__(self, pieces, *, streaming=True, cancel_at=None):
        self._pieces = pieces
        self._streaming = streaming
        self._cancel_at = cancel_at
        self.calls: list[str] = []

    def supports_streaming(self, purpose=None) -> bool:
        return self._streaming

    def chat_generate(self, request):
        raise AssertionError("the non-streaming path must not be used here")

    def chat_generate_stream(self, request, cancel_event=None, read_timeout=5.0):
        from giscode.inference.errors import RequestCancelledError

        for index, piece in enumerate(self._pieces):
            if self._cancel_at is not None and index == self._cancel_at:
                error = RequestCancelledError("stopped by the test")
                error.partial_text = "".join(self._pieces[:index])
                raise error
            self.calls.append(piece)
            yield (piece, None, None)
        yield ("", "stop", 0)


def test_chat_streams_deltas_and_keeps_the_final_text():
    router = _FakeRouter(["Hel", "lo"])
    seen: list[str] = []
    result = model_chat.answer_with_model(
        "hi", router=router, on_delta=seen.append, purpose="chat"
    )

    assert seen == ["Hel", "lo"]
    assert result["status"] == "completed"
    assert "Hello" in result["response"]


def test_chat_cancel_reports_a_partial_answer_as_partial():
    router = _FakeRouter(["half ", "an answer"], cancel_at=1)
    seen: list[str] = []
    result = model_chat.answer_with_model(
        "hi", router=router, on_delta=seen.append, purpose="chat"
    )

    assert seen == ["half "]
    assert result["status"] == "cancelled"
    assert "partial" in result["response"].lower()
    assert "half " in result["response"]


class _ErrorAfterDeltaRouter(_FakeRouter):
    def chat_generate_stream(self, request, cancel_event=None, read_timeout=5.0):
        yield ("part", None, None)
        error = InferenceUnavailableError("the provider failed after one delta")
        error.partial_text = "part"
        raise error


def test_chat_provider_error_keeps_partial_text_and_marks_it_partial():
    seen: list[str] = []
    result = model_chat.answer_with_model(
        "hi", router=_ErrorAfterDeltaRouter([]), on_delta=seen.append, purpose="chat"
    )

    assert seen == ["part"]
    assert result["status"] == "failed"
    assert "part" in result["response"]
    assert "partial" in result["response"].lower()


# ----------------------------------- completion, concurrency and lifecycle


def _sse_content(text: str):
    return {"choices": [{"delta": {"content": text}}]}


def test_sse_done_alone_completes_without_a_finish_reason():
    def responder(payload, wfile, index):
        wfile.write(b"data: [DONE]\n\n")
        wfile.flush()

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        pieces, finish, tokens = _collect(backend, _request())

    assert pieces == []
    assert finish == "stop"
    assert tokens == 0


def test_sse_done_after_content_completes_without_a_finish_reason():
    def responder(payload, wfile, index):
        _sse(wfile, _sse_content("done by sentinel"))
        wfile.write(b"data: [DONE]\n\n")
        wfile.flush()

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        pieces, finish, _tokens = _collect(backend, _request())

    assert pieces == ["done by sentinel"]
    assert finish == "stop"


def test_sse_done_split_across_network_chunks_is_recognised():
    def responder(payload, wfile, index):
        wfile.write(b"data: {\"choices\":[{\"delta\":{\"content\":\"split\"}}]}\n\n")
        wfile.write(b"data: [DO")
        wfile.flush()
        time.sleep(0.01)
        wfile.write(b"NE]\n\n")
        wfile.flush()

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        pieces, finish, _tokens = _collect(backend, _request())

    assert pieces == ["split"]
    assert finish == "stop"


def test_sse_done_after_only_malformed_data_is_a_failure():
    def responder(payload, wfile, index):
        wfile.write(b"data: {not json}\n\ndata: [DONE]\n\n")
        wfile.flush()

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        with pytest.raises(InferenceUnavailableError) as excinfo:
            _collect(backend, _request())

    assert "not valid JSON" in str(excinfo.value)


def test_provider_error_after_a_delta_keeps_the_partial_text():
    def responder(payload, wfile, index):
        _sse(wfile, _sse_content("part"))
        _sse(wfile, {"error": {"message": "crashed after one token"}})

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        with pytest.raises(InferenceUnavailableError) as excinfo:
            _collect(backend, _request())

    assert excinfo.value.partial_text == "part"
    assert "crashed after one token" in str(excinfo.value)


def test_timeout_message_names_the_actual_idle_limit():
    def responder(payload, wfile, index):
        _sse(wfile, _sse_content("stalled"))
        time.sleep(1.8)

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m", timeout=180)
        with pytest.raises(InferenceTimeoutError) as excinfo:
            _collect(backend, _request(), read_timeout=1.2)

    assert "1.2s" in str(excinfo.value)
    assert "180s" not in str(excinfo.value)


def _two_stream_server(*, first_completes: bool = False):
    started = [threading.Event(), threading.Event()]
    release = threading.Event()

    def responder(payload, wfile, index):
        number = index
        _sse(wfile, _sse_content(f"{number}a"))
        started[number - 1].set()
        if not (first_completes and number == 1):
            release.wait(5)
        _sse(wfile, _sse_content(f"{number}b"))
        _sse(wfile, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        wfile.write(b"data: [DONE]\n\n")
        wfile.flush()

    return _ProviderServer(responder), started, release


def _consume_two(backend, request_id, received, outcomes, *, barrier=None):
    def _consume():
        try:
            if barrier is not None:
                barrier.wait(2)
            for piece, _reason, _tokens in backend.chat_generate_stream(
                _request(request_id)
            ):
                if piece:
                    received[request_id].append(piece)
            outcomes[request_id] = "completed"
        except BaseException as exc:  # noqa: BLE001 - asserted by the test
            outcomes[request_id] = exc

    thread = threading.Thread(target=_consume, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("cancel_id", ["A", "B"])
def test_cancelling_one_concurrent_request_leaves_the_other_untouched(cancel_id):
    server, started, release = _two_stream_server()
    survivor = "B" if cancel_id == "A" else "A"
    received = {"A": [], "B": []}
    outcomes: dict = {}
    with server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        threads = {"A": _consume_two(backend, "A", received, outcomes)}
        assert started[0].wait(2)
        threads["B"] = _consume_two(backend, "B", received, outcomes)
        assert started[1].wait(2)
        assert backend.cancel(cancel_id) is True
        threads[cancel_id].join(2)
        assert not threads[cancel_id].is_alive()
        assert threads[survivor].is_alive(), "cancelling one request aborted another"
        release.set()
        threads[survivor].join(3)

    assert isinstance(outcomes[cancel_id], RequestCancelledError)
    assert outcomes[survivor] == "completed"
    assert received[survivor] == [f"{1 if survivor == 'A' else 2}a", f"{1 if survivor == 'A' else 2}b"]
    assert backend.cancel(survivor) is False


def test_late_cancel_for_a_completed_concurrent_request_is_ignored():
    server, started, release = _two_stream_server(first_completes=True)
    received = {"A": [], "B": []}
    outcomes: dict = {}
    with server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        thread_a = _consume_two(backend, "A", received, outcomes)
        assert started[0].wait(2)
        thread_a.join(2)
        assert outcomes.get("A") == "completed"
        thread_b = _consume_two(backend, "B", received, outcomes)
        assert started[1].wait(2)
        assert backend.cancel("A") is False
        assert thread_b.is_alive()
        release.set()
        thread_b.join(3)

    assert outcomes["B"] == "completed"
    assert "".join(received["B"]) == "2a2b"


def test_cancel_during_connection_setup_returns_before_headers_arrive():
    def responder(payload, wfile, index):  # pragma: no cover - headers are delayed
        _sse(wfile, _sse_content("too late"))

    with _ProviderServer(responder, header_delay=1.5) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        failure: dict = {}
        thread = threading.Thread(
            target=lambda: _record_cancel(backend, failure), daemon=True
        )
        thread.start()
        assert server.post_seen.wait(2)
        started = time.monotonic()
        assert backend.cancel("r1") is True
        thread.join(0.8)
        elapsed = time.monotonic() - started

    assert not thread.is_alive(), "cancellation waited for response headers"
    assert elapsed < 1.2
    assert isinstance(failure.get("exc"), RequestCancelledError)


def _record_cancel(backend, failure: dict) -> None:
    try:
        _collect(backend, _request())
    except BaseException as exc:  # noqa: BLE001 - asserted by the test
        failure["exc"] = exc


# ------------------------------------------------ registration / stop races


class _RegistrationGateBackend(ProviderChatBackend):
    """Pause registration at a deterministic barrier for a Stop race test."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.registration_entered = threading.Event()
        self.registration_release = threading.Event()

    def _register_request(self, request_id):
        self.registration_entered.set()
        if not self.registration_release.wait(5):
            raise AssertionError("test did not release registration")
        return super()._register_request(request_id)


def test_named_stop_before_registration_only_blocks_that_request():
    def responder(payload, wfile, index):
        _sse(wfile, {"choices": [{"delta": {"content": "next"}}]})
        _sse(wfile, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        wfile.write(b"data: [DONE]\n\n")
        wfile.flush()

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        assert backend.cancel("r1") is True
        with pytest.raises(RequestCancelledError):
            _collect(backend, _request("r1"))
        pieces, finish, _tokens = _collect(backend, _request("r2"))

    assert "".join(pieces) == "next"
    assert finish == "stop"
    assert not any(step["payload"].get("request_id") == "r1" for step in server.requests)


def test_stop_during_registration_is_not_lost():
    def responder(payload, wfile, index):  # pragma: no cover - no connection
        raise AssertionError("a stopped request must not connect")

    with _ProviderServer(responder) as server:
        backend = _RegistrationGateBackend(LMStudioProvider(server.endpoint), "m")
        failure = {}

        def consume():
            try:
                _collect(backend, _request("r1"))
            except BaseException as exc:  # noqa: BLE001
                failure["exc"] = exc

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        assert backend.registration_entered.wait(5)
        assert backend.cancel("r1") is True
        backend.registration_release.set()
        thread.join(5)

    assert isinstance(failure.get("exc"), RequestCancelledError)
    assert not any(step["path"].endswith("/chat/completions") for step in server.requests)


def test_stop_after_completion_is_stale_not_a_prestart_stop():
    def responder(payload, wfile, index):
        _sse(wfile, {"choices": [{"delta": {"content": "done"}}]})
        _sse(wfile, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        wfile.write(b"data: [DONE]\n\n")
        wfile.flush()

    with _ProviderServer(responder) as server:
        backend = ProviderChatBackend(LMStudioProvider(server.endpoint), "m")
        _collect(backend, _request("r1"))
        assert backend.cancel("r1") is False
        pieces, finish, _tokens = _collect(backend, _request("r2"))

    assert "".join(pieces) == "done"
    assert finish == "stop"
