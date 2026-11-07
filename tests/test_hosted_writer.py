from __future__ import annotations

import io
import json
import threading
import urllib.error
from pathlib import Path

import pytest

from giscode.inference.hosted import (
    KEY_ENV,
    CallBudget,
    HostedWriterError,
    HostedWriterProvider,
    RecordedResponseProvider,
    api_style_for,
    build_writer_provider,
    hosted_enabled,
    masked_key_proof,
    redact,
)
from giscode.inference.runtime import ChatMessage, ChatRequest, GenerationRequest
from tests.conftest import requires_gisai

TEST_KEY = "sk-" + "a1b2c3d4e5f6ghij7890"


class _FakeSettings:
    def __init__(self, data: dict):
        self.data = dict(data)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class _FakeResponse:
    """Canned HTTP body.

    ``read()`` serves the full body (blocking chat path) until ``readline()``
    has been used (stream path, where ``read()`` is only the trailing rest and
    must stay empty). Body-only responses are also offered as a single line so
    the "server ignored stream: one JSON document" branch is reachable.
    """

    def __init__(self, body: bytes = b"", lines: list[bytes] | None = None):
        self._body = body
        if lines is None and body:
            lines = [body]
        self._lines = list(lines or [])
        self._line_consumed = False

    def read(self, n: int = -1) -> bytes:
        if self._line_consumed:
            return b""
        return self._body

    def readline(self) -> bytes:
        if self._lines:
            self._line_consumed = True
            return self._lines.pop(0)
        self._line_consumed = True
        return b""

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Injected urlopen replacement: captures requests, returns canned bodies."""

    def __init__(self, builder, error: Exception | None = None):
        self._builder = builder
        self._error = error
        self.requests: list = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self._error is not None:
            raise self._error
        return self._builder()


def _req(text: str = "hello", **kw) -> ChatRequest:
    return ChatRequest(
        messages=[
            ChatMessage(role="system", content="you are gisai"),
            ChatMessage(role="user", content=text),
        ],
        max_tokens=kw.get("max_tokens", 64),
        temperature=kw.get("temperature", 0.0),
        images=kw.get("images"),
    )


def _provider(opener, env=None, **kw) -> HostedWriterProvider:
    return HostedWriterProvider(
        None,
        opener=opener,
        env=env if env is not None else {KEY_ENV: TEST_KEY},
        **kw,
    )


# ---------------------------------------------------------------------------
# key rules
# ---------------------------------------------------------------------------


class TestKeyRules:
    def test_refuses_without_key_and_names_env_var(self):
        provider = _provider(_FakeOpener(lambda: _FakeResponse(b"{}")), env={})
        with pytest.raises(HostedWriterError) as exc:
            provider.chat_generate(_req())
        assert KEY_ENV in str(exc.value)

    def test_health_is_honest_without_key(self):
        provider = _provider(_FakeOpener(lambda: _FakeResponse(b"{}")), env={})
        health = provider.get_health()
        assert health["model_loaded"] is False
        assert health["model_state"] == "hosted_key_missing"

    def test_health_ready_with_key(self):
        provider = _provider(_FakeOpener(lambda: _FakeResponse(b"{}")))
        health = provider.get_health()
        assert health["model_loaded"] is True
        assert health["hosted"] is True

    def test_masked_key_proof_never_exposes_key(self):
        assert masked_key_proof("") == "key absent"
        proof = masked_key_proof(TEST_KEY)
        assert "key present" in proof
        assert "sk..." in proof
        assert str(len(TEST_KEY)) in proof
        assert TEST_KEY not in proof

    def test_redact_patterns_and_live_key(self):
        text = f"leak {TEST_KEY} bearer ABCDEFGHIJKLMNOP and api_key = hunter2secret"
        out = redact(text, (TEST_KEY,))
        assert TEST_KEY not in out
        assert "ABCDEFGHIJKLMNOP" not in out
        assert "hunter2secret" not in out
        assert "[REDACTED:key]" in out

    def test_prompt_containing_key_is_redacted_before_send(self):
        captured = {}

        def build():
            return _FakeResponse(
                b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'
            )

        opener = _FakeOpener(build)
        provider = _provider(opener, base_url="https://example.test/v1")
        provider.chat_generate(_req(f"secret is {TEST_KEY}"))
        req, _timeout = opener.requests[0]
        body = json.loads(req.data.decode("utf-8"))
        assert TEST_KEY not in json.dumps(body)
        assert "[REDACTED:key]" in json.dumps(body)


# ---------------------------------------------------------------------------
# payload shapes / endpoints / styles
# ---------------------------------------------------------------------------


class TestShapes:
    def test_api_style_auto_from_model_id(self):
        assert api_style_for("claude-opus-5-5") == "messages"
        assert api_style_for("gpt-6.1-sol") == "responses"
        assert api_style_for("minimax-m3") == "chat"

    def test_api_style_override_wins(self):
        assert api_style_for("minimax-m3", "messages") == "messages"
        assert api_style_for("claude-opus-5-5", "chat") == "chat"

    def test_endpoint_paths(self):
        assert HostedWriterProvider(None, base_url="https://e/v1", model="m", api_style="chat").endpoint == "https://e/v1/chat/completions"
        assert HostedWriterProvider(None, base_url="https://e/v1", model="m", api_style="messages").endpoint == "https://e/v1/messages"
        assert HostedWriterProvider(None, base_url="https://e/v1", model="m", api_style="responses").endpoint == "https://e/v1/responses"

    def _roundtrip(self, style: str, body: bytes, expected_text: str, opener_builder=None):
        opener = _FakeOpener(opener_builder or (lambda: _FakeResponse(body)))
        provider = _provider(opener, base_url="https://example.test/v1", api_style=style, model="m-1")
        response = provider.chat_generate(_req())
        assert response.text == expected_text
        return opener.requests[0][0]

    def test_chat_shape_request_and_parse(self):
        req = self._roundtrip(
            "chat",
            b'{"choices":[{"message":{"content":"hey"},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":7,"completion_tokens":3}}',
            "hey",
        )
        assert req.full_url == "https://example.test/v1/chat/completions"
        assert req.get_header("Authorization") == f"Bearer {TEST_KEY}"
        body = json.loads(req.data.decode("utf-8"))
        assert body["model"] == "m-1"
        assert body["stream"] is False
        assert [m["role"] for m in body["messages"]] == ["system", "user"]

    def test_messages_shape_moves_system(self):
        req = self._roundtrip(
            "messages",
            b'{"content":[{"type":"text","text":"bonjour"}],"stop_reason":"end_turn",'
            b'"usage":{"input_tokens":5,"output_tokens":2}}',
            "bonjour",
        )
        assert req.full_url == "https://example.test/v1/messages"
        assert req.get_header("X-api-key") == TEST_KEY
        assert req.get_header("Anthropic-version")
        body = json.loads(req.data.decode("utf-8"))
        assert "system" in body
        assert all(m["role"] != "system" for m in body["messages"])

    def test_responses_shape_input(self):
        req = self._roundtrip(
            "responses",
            b'{"output_text":"salut","status":"completed","usage":{"input_tokens":4,"output_tokens":6}}',
            "salut",
        )
        assert req.full_url == "https://example.test/v1/responses"
        body = json.loads(req.data.decode("utf-8"))
        assert body["max_output_tokens"] == 64
        assert [m["role"] for m in body["input"]] == ["system", "user"]

    def test_history_stores_hash_not_payload(self):
        opener = _FakeOpener(
            lambda: _FakeResponse(
                b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'
            )
        )
        provider = _provider(opener, base_url="https://example.test/v1")
        provider.chat_generate(_req("unique-prompt-marker-xyz"))
        dumped = json.dumps(provider.history)
        assert "unique-prompt-marker-xyz" not in dumped
        assert provider.history[0]["payload_sha256"]
        assert len(provider.history[0]["payload_sha256"]) == 64

    def test_images_refused(self):
        provider = _provider(_FakeOpener(lambda: _FakeResponse(b"{}")))
        with pytest.raises(HostedWriterError, match="does not send images"):
            provider.chat_generate(_req(images=["x.png"]))


# ---------------------------------------------------------------------------
# transport, budget, errors
# ---------------------------------------------------------------------------


class TestTransportAndBudget:
    def test_size_cap_refused_before_budget_spend(self):
        provider = _provider(
            _FakeOpener(lambda: _FakeResponse(b"{}")), max_request_chars=20
        )
        with pytest.raises(HostedWriterError, match="size cap exceeded"):
            provider.chat_generate(_req())
        assert provider.budget.used == 0
        assert provider.history == []

    def test_budget_cap_is_a_hard_stop(self):
        provider = _provider(
            _FakeOpener(
                lambda: _FakeResponse(
                    b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'
                )
            ),
            budget=CallBudget(1),
            base_url="https://example.test/v1",
        )
        provider.chat_generate(_req())
        with pytest.raises(HostedWriterError, match="budget exhausted"):
            provider.chat_generate(_req())
        assert provider.budget.used == 1

    def test_counter_file_shared_and_holds_only_a_number(self, tmp_path: Path):
        counter = tmp_path / "budget.json"
        first = CallBudget(300, counter_file=str(counter))
        first.spend(4)
        assert json.loads(counter.read_text(encoding="utf-8")) == {"used": 4}
        second = CallBudget(300, counter_file=str(counter))
        assert second.used == 4
        second.spend(1)
        assert second.used == 5
        # A fresh reader sees the shared total; the file never holds anything
        # but the number.
        third = CallBudget(300, counter_file=str(counter))
        assert third.used == 5
        assert json.loads(counter.read_text(encoding="utf-8")) == {"used": 5}

    def test_http_401_mentions_key_env_and_hides_key(self):
        opener = _FakeOpener(
            None,
            error=urllib.error.HTTPError(
                "https://example.test/v1/chat/completions",
                401,
                "Unauthorized",
                None,
                io.BytesIO(b'{"error":"invalid key ' + TEST_KEY.encode() + b'"}'),
            ),
        )
        provider = _provider(opener, base_url="https://example.test/v1")
        with pytest.raises(HostedWriterError) as exc:
            provider.chat_generate(_req())
        message = str(exc.value)
        assert "rejected the key" in message
        assert KEY_ENV in message
        assert TEST_KEY not in message

    def test_http_404_names_endpoint(self):
        opener = _FakeOpener(
            None,
            error=urllib.error.HTTPError(
                "https://example.test/v1/chat/completions",
                404,
                "Not Found",
                None,
                io.BytesIO(b"not here"),
            ),
        )
        provider = _provider(opener, base_url="https://example.test/v1", model="ghost")
        with pytest.raises(HostedWriterError, match="HTTP 404"):
            provider.chat_generate(_req())

    def test_unparseable_body_is_a_clear_error(self):
        provider = _provider(_FakeOpener(lambda: _FakeResponse(b"<html>oops</html>")))
        with pytest.raises(HostedWriterError, match="not parseable"):
            provider.chat_generate(_req())


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


def _sse_lines() -> list[bytes]:
    return [
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
        b"\n",
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n',
        b"\n",
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n',
        b"\n",
        b"data: [DONE]\n",
        b"\n",
    ]


class TestStreaming:
    def test_sse_stream_yields_pieces_then_final(self):
        provider = _provider(
            _FakeOpener(lambda: _FakeResponse(lines=_sse_lines())),
            base_url="https://example.test/v1",
        )
        chunks = list(provider.chat_generate_stream(_req()))
        pieces = [c[0] for c in chunks[:-1]]
        assert pieces == ["Hel", "lo"]
        assert chunks[-1] == ("", "stop", 0)
        assert provider.budget.used == 1

    def test_non_streaming_server_in_single_chunk_mode(self):
        body = (
            b'{"choices":[{"message":{"content":"one-shot"},"finish_reason":"stop"}],'
            b'"usage":{"completion_tokens":3}}'
        )
        provider = _provider(
            _FakeOpener(lambda: _FakeResponse(body)), base_url="https://example.test/v1"
        )
        chunks = list(provider.chat_generate_stream(_req()))
        assert chunks[0] == ("one-shot", None, 0)
        assert chunks[-1] == ("", "stop", 3)

    def test_cancel_event_stops_stream_and_records_status(self):
        provider = _provider(
            _FakeOpener(lambda: _FakeResponse(lines=_sse_lines())),
            base_url="https://example.test/v1",
        )
        cancel = threading.Event()
        cancel.set()
        chunks = list(provider.chat_generate_stream(_req(), cancel_event=cancel))
        assert chunks[-1] == ("", "stop", 0)
        assert provider.history[-1]["status"] == "cancelled"

    def test_generate_legacy_surface_maps_to_chat(self):
        provider = _provider(
            _FakeOpener(
                lambda: _FakeResponse(
                    b'{"choices":[{"message":{"content":"legacy"},"finish_reason":"stop"}]}'
                )
            ),
            base_url="https://example.test/v1",
        )
        response = provider.generate(GenerationRequest(prompt="do it"))
        assert response.text == "legacy"


# ---------------------------------------------------------------------------
# recorded provider (test-driven loop fake)
# ---------------------------------------------------------------------------


class TestRecordedProvider:
    def test_exhausted_entry_falls_through_to_next(self):
        provider = RecordedResponseProvider(
            [
                {"match": "STEP_ONE", "response": "first answer", "max_calls": 1},
                {"response": "second answer"},
            ]
        )
        first = provider.chat_generate(_req("please STEP_ONE now"))
        second = provider.chat_generate(_req("please STEP_ONE again"))
        assert first.text == "first answer"
        assert second.text == "second answer"

    def test_strict_mode_fails_loudly_on_no_match(self):
        provider = RecordedResponseProvider([{"match": "NEEDLE", "response": "r"}])
        with pytest.raises(HostedWriterError, match="no recorded response matches"):
            provider.chat_generate(_req("something else entirely"))

    def test_non_strict_returns_empty(self):
        provider = RecordedResponseProvider(
            [{"match": "NEEDLE", "response": "r"}], strict=False
        )
        assert provider.chat_generate(_req("no needle here")).text == ""

    def test_regex_matching(self):
        provider = RecordedResponseProvider(
            [{"match": r"repair attempt \d+", "regex": True, "response": "fixed"}]
        )
        assert provider.chat_generate(_req("please run repair attempt 2 now")).text == "fixed"

    def test_health_and_surface(self):
        provider = RecordedResponseProvider([{"response": "hi"}])
        assert provider.get_health()["model_loaded"] is True
        assert provider.is_writer_provider is True
        assert provider.chat_generate(_req()).text == "hi"
        assert provider.calls

    def test_budget_still_enforced(self):
        provider = RecordedResponseProvider(
            [{"response": "x"}], budget=CallBudget(2)
        )
        provider.chat_generate(_req())
        provider.chat_generate(_req())
        with pytest.raises(HostedWriterError, match="budget exhausted"):
            provider.chat_generate(_req())


# ---------------------------------------------------------------------------
# factory + engine seam
# ---------------------------------------------------------------------------


class TestFactoryAndSeam:
    def test_disabled_builds_nothing(self):
        assert build_writer_provider(_FakeSettings({})) is None
        assert hosted_enabled(None) is False

    def test_enabled_builds_provider_from_settings(self):
        settings = _FakeSettings(
            {"hosted.enabled": True, "hosted.model": "minimax-m3", "hosted.api_style": "auto"}
        )
        provider = build_writer_provider(settings, env={})
        assert isinstance(provider, HostedWriterProvider)
        assert provider.model == "minimax-m3"
        assert provider.is_writer_provider is True

    def test_project_override_wins_both_ways(self):
        on_for = _FakeSettings(
            {"hosted.enabled": True, "hosted.project_overrides": {"C:\\proj\\off": False}}
        )
        assert build_writer_provider(on_for, env={}, project_root="C:\\proj\\off") is None
        assert build_writer_provider(on_for, env={}, project_root="C:\\proj\\on") is not None

        off_for = _FakeSettings(
            {"hosted.enabled": False, "hosted.project_overrides": {"C:\\proj\\on": True}}
        )
        assert build_writer_provider(off_for, env={}, project_root="C:\\proj\\on") is not None
        assert build_writer_provider(off_for, env={}, project_root="C:\\proj\\other") is None

    @requires_gisai
    def test_engine_swap_covers_inference_and_planner_gateway(self):
        from giscode.gisai.engine import GisAiEngine

        engine = GisAiEngine()
        provider = RecordedResponseProvider([{"response": "ok"}])
        engine.set_writer_provider(provider)
        assert engine._inference is provider
        assert engine._llm_planner._inference is provider
        assert engine.is_hosted_writer is True

    @requires_gisai
    def test_model_executor_skips_local_load_and_unload(self):
        from unittest.mock import MagicMock

        from giscode.gisai.model_work import ModelDrivenRealWorkExecutor

        engine = MagicMock()
        engine._model_manager = None
        provider = RecordedResponseProvider([{"response": "ok"}])
        engine._inference = provider
        executor = ModelDrivenRealWorkExecutor(engine=engine, tool_registry=MagicMock())
        loaded = executor._select_and_load_model(MagicMock())
        assert loaded["success"] is True
        assert loaded["model_name"] == provider.model
        executor._cleanup_model()  # must not raise with no model manager

        class _Boom:
            is_writer_provider = True
            model = "boom"

            def unload_model(self):
                raise AssertionError("hosted provider must not be unloaded")

        engine._model_manager = MagicMock()
        engine._inference = _Boom()
        executor._cleanup_model()  # guarded: no unload attempted

    @requires_gisai
    def test_local_executor_still_loads_via_model_manager(self):
        from unittest.mock import MagicMock

        from giscode.gisai.model_work import ModelDrivenRealWorkExecutor

        class _FakeMeta:
            name = "local-model"
            path = "C:\\models\\local.gguf"

            def supports(self, capability):
                return True

        engine = MagicMock()
        inference = MagicMock()
        inference.is_writer_provider = False
        engine._inference = inference
        manager = MagicMock()
        manager.discover_model_metadata.return_value = [_FakeMeta()]
        engine._model_manager = manager
        executor = ModelDrivenRealWorkExecutor(engine=engine, tool_registry=MagicMock())
        loaded = executor._select_and_load_model(MagicMock())
        assert loaded["success"] is True
        manager.load_model.assert_called_once_with("local-model")
