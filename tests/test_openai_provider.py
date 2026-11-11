"""Mock and local qualification for the official OpenAI provider.

These tests use a loopback HTTP server, never the real OpenAI service. They
therefore prove the wire contract, consent gate, redaction, cancellation and
router wiring, but they are **not** evidence of a live OpenAI account or a live
model response. The evidence report names that distinction explicitly.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import giscode.providers.openai as openai_module
from giscode.agentic.loop import CodingAgentLoop
from giscode.agentic.types import ExecutionPhase, TaskRequest, TaskResult
from giscode.inference.provider_backend import ProviderChatBackend
from giscode.inference.runtime import ChatMessage, ChatRequest
from giscode.providers.base import OPENAI, ProviderError
from giscode.providers.credentials import MemoryCredentialStore, WindowsDpapiStore
from giscode.providers.openai import OpenAIProvider, configure_openai_profile
from giscode.providers.profiles import ProfileStore
from giscode.providers.registry import ProviderRegistry
from giscode.providers.settings_adapter import DictSettings
from giscode.providers.streaming import StreamCancelled
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace

TEST_KEY = "sk-" + "testsecret1234567890"
MODEL = "gpt-4.1"


class _Server:
    """Small deterministic HTTP server for the documented OpenAI paths."""

    def __init__(
        self,
        *,
        content: str = "mock answer",
        # ``None`` means the streamed body carries the same answer as the
        # blocking body. Tests that need several chunks pass them explicitly.
        stream_parts: tuple[str, ...] | None = None,
        status: int = 200,
        delay: float = 0.0,
    ) -> None:
        self.content = content
        self.stream_parts = stream_parts
        self.status = status
        self.delay = delay
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                return

            def _json(self, payload: dict, status: int = 200) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                    # The client timed out or cancelled: expected in these tests.
                    return

            def do_GET(self):
                outer.requests.append(
                    {"method": "GET", "path": self.path, "headers": dict(self.headers)}
                )
                if self.path.endswith("/v1/models"):
                    if outer.status != 200:
                        self._json(
                            {"error": {"message": f"invalid key {TEST_KEY}"}},
                            outer.status,
                        )
                        return
                    self._json({"data": [{"id": MODEL}]})
                    return
                self._json({"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = {}
                outer.requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "headers": dict(self.headers),
                        "payload": payload,
                    }
                )
                if outer.delay:
                    time.sleep(outer.delay)
                if outer.status != 200:
                    self._json(
                        {"error": {"message": f"invalid key {TEST_KEY}"}},
                        outer.status,
                    )
                    return
                if payload.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    parts = (
                        outer.stream_parts
                        if outer.stream_parts is not None
                        else (outer.content,)
                    )
                    try:
                        for part in parts:
                            obj = {"choices": [{"delta": {"content": part}}]}
                            self.wfile.write(
                                f"data: {json.dumps(obj)}\n\n".encode("utf-8")
                            )
                            self.wfile.flush()
                        obj = {
                            "choices": [{"delta": {}, "finish_reason": "stop"}],
                            "usage": {
                                "prompt_tokens": 3,
                                "completion_tokens": 2,
                                "total_tokens": 5,
                            },
                        }
                        self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode("utf-8"))
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    return
                self._json(
                    {
                        "choices": [
                            {
                                "message": {"content": outer.content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 3,
                            "completion_tokens": 2,
                            "total_tokens": 5,
                        },
                    }
                )

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def posts(self) -> list[dict]:
        return [item for item in self.requests if item["method"] == "POST"]

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _provider(server: _Server, *, paid: bool = True, store=None, **kwargs) -> OpenAIProvider:
    timeout = kwargs.pop("timeout", 2.0)
    return OpenAIProvider(
        server.endpoint,
        credential_store=store or MemoryCredentialStore({"openai": TEST_KEY}),
        paid_calls_allowed=paid,
        timeout=timeout,
        **kwargs,
    )


def _allow_test_host(monkeypatch) -> None:
    # The production guard still enforces api.openai.com. This only lets the
    # deterministic local server stand in for the official host in this test.
    monkeypatch.setattr(openai_module, "OFFICIAL_HOST", "127.0.0.1")


def _chat_request(text: str = "hello", request_id: str = "req-1") -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="user", content=text)],
        max_tokens=32,
        temperature=0.0,
        request_id=request_id,
    )


def test_official_host_guard_rejects_other_remote_endpoints():
    with pytest.raises(ValueError, match="api.openai.com"):
        OpenAIProvider("https://example.com", paid_calls_allowed=True)


def test_missing_credential_is_refused_before_any_network(monkeypatch):
    _allow_test_host(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with _Server() as server:
        provider = _provider(server, store=MemoryCredentialStore())
        with pytest.raises(ProviderError, match="key is not configured"):
            provider.chat(MODEL, [{"role": "user", "content": "hello"}])
        assert server.requests == []


def test_billable_consent_is_required_before_listing_or_chat(monkeypatch):
    _allow_test_host(monkeypatch)
    with _Server() as server:
        provider = _provider(server, paid=False)
        assert provider.status().available is False
        assert server.requests == []
        with pytest.raises(ProviderError, match="billable"):
            provider.list_models()
        with pytest.raises(ProviderError, match="billable"):
            provider.chat(MODEL, [{"role": "user", "content": "hello"}])
        assert server.requests == []


def test_successful_mock_round_trip_uses_official_shape_and_records_usage(monkeypatch):
    _allow_test_host(monkeypatch)
    with _Server(content="qualified") as server:
        provider = _provider(
            server,
            pricing={"input_per_million": 1.0, "output_per_million": 2.0},
        )
        models = provider.list_models()
        assert [model.id for model in models] == [MODEL]
        text = provider.chat(
            MODEL,
            [{"role": "user", "content": "hello"}],
            max_tokens=32,
            request_id="r-42",
        )
        assert text == "qualified"
        post = server.posts[-1]
        assert post["path"].endswith("/v1/chat/completions")
        assert post["headers"].get("Authorization") == f"Bearer {TEST_KEY}"
        assert post["payload"]["model"] == MODEL
        assert post["payload"]["max_completion_tokens"] == 32
        usage = provider.usage_snapshot()
        assert usage["requests"] == 1
        assert usage["prompt_tokens"] == 3
        assert usage["completion_tokens"] == 2
        assert usage["estimated_cost_usd"] == pytest.approx(0.000007)


def test_invalid_credential_is_reported_without_echoing_the_key(monkeypatch):
    _allow_test_host(monkeypatch)
    with _Server(status=401) as server:
        provider = _provider(server)
        with pytest.raises(ProviderError) as excinfo:
            provider.list_models()
        message = str(excinfo.value)
        assert "rejected" in message.lower()
        assert TEST_KEY not in message
        assert "invalid key" not in message  # the server's echo was scrubbed too


def test_timeout_is_reported_without_a_silent_retry(monkeypatch):
    _allow_test_host(monkeypatch)
    with _Server(delay=0.4) as server:
        provider = _provider(server, timeout=0.05)
        with pytest.raises(ProviderError, match="did not answer"):
            provider.chat(
                MODEL,
                [{"role": "user", "content": "hello"}],
                timeout=0.05,
            )
        assert len(server.posts) == 1


def test_streaming_collects_text_and_usage(monkeypatch):
    _allow_test_host(monkeypatch)
    with _Server() as server:
        provider = _provider(server)
        backend = ProviderChatBackend(provider, MODEL)
        pieces: list[str] = []
        finish = ""
        tokens = 0
        for piece, reason, count in backend.chat_generate_stream(_chat_request()):
            if piece:
                pieces.append(piece)
            if reason:
                finish = reason
            if isinstance(count, int):
                tokens = count
        assert "".join(pieces) == "mock answer"
        assert finish == "stop"
        assert tokens == 2
        post = server.posts[-1]
        assert post["payload"]["stream"] is True
        assert post["payload"]["stream_options"] == {"include_usage": True}
        assert provider.usage_snapshot()["completion_tokens"] == 2


def test_cancel_before_stream_sends_no_post(monkeypatch):
    _allow_test_host(monkeypatch)
    cancel = threading.Event()
    cancel.set()
    with _Server() as server:
        provider = _provider(server)
        with pytest.raises(StreamCancelled):
            list(
                provider.stream_chat(
                    MODEL,
                    [{"role": "user", "content": "hello"}],
                    cancel_event=cancel,
                )
            )
        assert server.posts == []


def test_outbound_secrets_are_redacted_before_the_request(monkeypatch):
    _allow_test_host(monkeypatch)
    with _Server() as server:
        provider = _provider(server)
        provider.chat(
            MODEL,
            [
                {
                    "role": "user",
                    "content": (
                        f"use api_key={TEST_KEY} and password=hunter2 and "
                        "Authorization: Bearer abcdefghijklmnop"
                    ),
                }
            ],
        )
        wire = server.posts[-1]["payload"]
        dumped = json.dumps(wire)
        assert TEST_KEY not in dumped
        assert "hunter2" not in dumped
        assert "abcdefghijklmnop" not in dumped
        assert "[REDACTED]" in dumped


def test_response_attributes_provider_model_and_request(monkeypatch):
    _allow_test_host(monkeypatch)
    with _Server(content="attributed") as server:
        provider = _provider(server)
        backend = ProviderChatBackend(provider, MODEL)
        response = backend.chat_generate(_chat_request(request_id="request-77"))
        assert response.text == "attributed"
        assert response.provider == OPENAI
        assert response.model == MODEL
        assert response.request_id == "request-77"


def test_usage_ledger_file_never_contains_prompt_text(monkeypatch, tmp_path):
    _allow_test_host(monkeypatch)
    ledger_path = tmp_path / "openai-usage.jsonl"
    with _Server(content="ok") as server:
        provider = _provider(server, usage_path=ledger_path)
        provider.chat(MODEL, [{"role": "user", "content": "unique-prompt-marker"}])
    contents = ledger_path.read_text(encoding="utf-8")
    assert "unique-prompt-marker" not in contents
    assert TEST_KEY not in contents
    assert '"total_tokens": 5' in contents


def test_registry_defaults_keep_local_providers_and_openai_off():
    settings = DictSettings({})
    registry = ProviderRegistry(settings, credentials=MemoryCredentialStore())
    configs = registry.configs()
    assert configs["ollama"]["enabled"] is True
    assert configs["lmstudio"]["enabled"] is True
    assert configs[OPENAI]["enabled"] is False
    assert configs[OPENAI]["paid_calls_allowed"] is False
    # Constructing the provider must not make a network request.
    assert registry.get(OPENAI) is not None


def test_configure_profile_persists_consent_without_a_secret():
    settings = DictSettings({})
    profile = configure_openai_profile(
        settings,
        model=MODEL,
        active=True,
        paid_calls_allowed=True,
    )
    assert profile.provider == OPENAI
    stored = json.dumps(settings._data)
    assert TEST_KEY not in stored
    assert "paid_calls_allowed" in stored
    profiles = ProfileStore(settings)
    assert profiles.active_for("coding").provider == OPENAI
    assert profiles.active_for("coding").model == MODEL


def test_agent_planning_uses_provider_and_skips_legacy_routing(monkeypatch, tmp_path):
    _allow_test_host(monkeypatch)
    plan = json.dumps(
        {
            "objective": "Create a tiny file",
            "assumptions": [],
            "files_expected_to_change": ["src/example.py"],
            "steps": [
                {
                    "step_id": "step-1",
                    "description": "Create example",
                    "action_type": "create",
                    "target_path": "src/example.py",
                    "content": "# example\\n",
                    "expected_result": "file exists",
                }
            ],
        }
    )
    with _Server(content=plan) as server:
        provider = _provider(server)
        settings = DictSettings(
            {
                "providers": {
                    OPENAI: {
                        "kind": OPENAI,
                        "label": "OpenAI",
                        "endpoint": server.endpoint,
                        "enabled": True,
                        "allow_remote": True,
                        "api_key_env": "OPENAI_API_KEY",
                        "paid_calls_allowed": True,
                        "pricing": {},
                    }
                },
                "profiles": {
                    "items": [
                        {
                            "id": "profile-openai",
                            "purpose": "coding",
                            "label": "OpenAI coding",
                            "provider": OPENAI,
                            "model": MODEL,
                        }
                    ],
                    "active": {"coding": "profile-openai"},
                    "last_good": {},
                },
            }
        )
        registry = ProviderRegistry(
            settings, credentials=MemoryCredentialStore({"openai": TEST_KEY})
        )
        from giscode.inference.router import ModelRouter

        router = ModelRouter(
            builtin=None,
            settings=settings,
            registry=registry,
            profiles=ProfileStore(settings),
        )

        class _NeverCalled:
            def route_and_assemble(self, *_args, **_kwargs):
                raise AssertionError("legacy routing must not be used for a provider")

        manager = TaskManager()
        manager.start_task("task-openai", max_duration=30)
        try:
            loop = CodingAgentLoop(
                Workspace(tmp_path),
                router,
                manager,
                routing_service=_NeverCalled(),
            )
            result = loop._run_planning(
                TaskRequest("task-openai", "Create a tiny file"),
                TaskResult(task_id="task-openai", status=ExecutionPhase.INTAKE),
            )
        finally:
            manager.complete()
        assert result.plan is not None
        assert result.plan.objective == "Create a tiny file"
        assert server.posts, "the planner never contacted the provider"
        assert server.posts[-1]["payload"]["messages"]


def test_windows_dpapi_round_trip(tmp_path):
    store = WindowsDpapiStore(tmp_path)
    if not store.available:
        pytest.skip("Windows DPAPI is unavailable on this host")
    store.set("openai", TEST_KEY)
    assert store.get("openai") == TEST_KEY
    assert TEST_KEY not in (tmp_path / "openai.bin").read_bytes().decode(
        "utf-8", "ignore"
    )
    # A fresh store instance models a restart: the DPAPI-protected file is
    # readable only by this Windows user, and the value survives.
    assert WindowsDpapiStore(tmp_path).get("openai") == TEST_KEY
    store.delete("openai")
    assert store.has("openai") is False
