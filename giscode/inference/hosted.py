"""Hosted writer provider: swap GisCode's local planner/writer for a hosted LLM.

The engine and its planner/writer talk to a duck-typed inference object with
``chat_generate`` / ``chat_generate_stream`` / ``cancel`` / ``get_health`` /
``get_model_status`` / ``unload_model``.  :class:`HostedWriterProvider`
implements exactly that surface against a hosted OpenAI-compatible endpoint,
so a local planner/writer can be swapped for a hosted model by handing the
engine this object instead of :class:`giscode.inference.api.InferenceAPI`.
:class:`RecordedResponseProvider` implements the same surface with recorded
responses (no network) for tests and the window drive.

Hard rules:

* The API key is read ONLY from the environment variable
  ``GISCODE_HOSTED_API_KEY``, at call time, and is never written to disk,
  logs, history or reports.  The only key exposure anywhere is
  :func:`masked_key_proof` (first 2 characters + length).
* Outgoing message text is scrubbed by the secret deny-list
  (:data:`SECRET_PATTERNS` + the live key value) before it is sent, and
  every recorded entry stores a SHA-256 of the payload — never the payload.
* Payloads above ``hosted.max_request_chars`` are refused, not truncated.
* Every call spends from a :class:`CallBudget`; the evidence cap (300) is a
  hard stop with a clear error, counted across processes via a counter file
  that holds only a number.
* Endpoint shapes: ``chat`` (OpenAI /v1/chat/completions), ``messages``
  (Anthropic /v1/messages), ``responses`` (OpenAI /v1/responses),
  auto-selected from the model id (``claude*`` -> messages,
  ``gpt*`` -> responses) unless ``hosted.api_style`` overrides it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from giscode.inference.runtime import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    GenerationRequest,
    GenerationResponse,
)

KEY_ENV = "GISCODE_HOSTED_API_KEY"
DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "minimax-m3"
DEFAULT_MAX_REQUEST_CHARS = 500_000
DEFAULT_TIMEOUT_S = 180.0
API_STYLES = ("chat", "messages", "responses")
ANTHROPIC_VERSION = "2023-06-01"
MAX_HISTORY_ENTRIES = 500

__all__ = [
    "API_STYLES",
    "CallBudget",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "HostedModelStatus",
    "HostedWriterError",
    "HostedWriterProvider",
    "KEY_ENV",
    "RecordedResponseProvider",
    "api_style_for",
    "build_writer_provider",
    "hosted_enabled",
    "masked_key_proof",
    "redact",
]


class HostedWriterError(RuntimeError):
    """Any hosted-writer failure: no key, size cap, budget, transport, HTTP."""


# --------------------------------------------------------------------------
# secret deny-list (redaction of outgoing / recorded text)
# --------------------------------------------------------------------------

SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("pem", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    )),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")),
    ("sk_token", re.compile(r"\b(?:sk|rk|pk|zk)-[A-Za-z0-9_\-]{16,}\b")),
    ("gh_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|apikey|access[_-]?token|client[_-]?secret"
        r"|password|passwd)\b\s*[:=]\s*[\"']?[A-Za-z0-9._\-]{8,}[\"']?"
    )),
)


def redact(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Scrub key material from ``text`` using the secret deny-list.

    ``extra_secrets`` are exact live values (the API key) that must never
    appear anywhere — they are replaced first, by exact match, before the
    pattern sweep.  Used for every outgoing message body and every string
    that could reach a log, history entry or report.
    """
    if not text:
        return text
    out = str(text)
    for secret in extra_secrets:
        if secret and len(str(secret)) >= 6:
            out = out.replace(str(secret), "[REDACTED:key]")
    for label, pattern in SECRET_PATTERNS:
        out = pattern.sub(f"[REDACTED:{label}]", out)
    return out


def masked_key_proof(key: Optional[str] = None) -> str:
    """Proof that a key exists without exposing it: first 2 chars + length."""
    if key is None:
        key = os.environ.get(KEY_ENV) or ""
    if not str(key).strip():
        return "key absent"
    return f"key present ({key[:2]}..., {len(key)} chars)"


def api_style_for(model: str, override: str = "auto") -> str:
    """Endpoint shape for ``model``: auto from the model id unless overridden."""
    if override in API_STYLES:
        return override
    name = (model or "").lower()
    if name.startswith("claude"):
        return "messages"
    if name.startswith("gpt"):
        return "responses"
    return "chat"


def hosted_enabled(settings: Any) -> bool:
    """True when the hosted writer is enabled (per project when one is set)."""
    if settings is None:
        return False
    try:
        return bool(settings.get("hosted.enabled", False))
    except Exception:  # noqa: BLE001 - a broken settings source means "off"
        return False


# --------------------------------------------------------------------------
# call budget (the 300-call evidence cap, shared across providers/processes)
# --------------------------------------------------------------------------


class CallBudget:
    """Hard cap on hosted calls. ``max_calls=0`` means unlimited.

    ``counter_file`` makes the budget shared across processes (preflight +
    two evidence passes): the file holds only ``{"used": <int>}`` — never a
    key, never prompt text.  Writes are atomic (tmp + replace).
    """

    def __init__(self, max_calls: int = 0, counter_file: Optional[str] = None):
        self.max_calls = int(max_calls or 0)
        self._counter_file = counter_file
        self._lock = threading.Lock()
        self._used = self._read_file() if counter_file else 0

    @property
    def used(self) -> int:
        if self._counter_file:
            with self._lock:
                return self._used
        return self._used

    @property
    def remaining(self) -> int:
        if self.max_calls <= 0:
            return -1
        return max(0, self.max_calls - self.used)

    def _read_file(self) -> int:
        try:
            with open(self._counter_file, "r", encoding="utf-8") as fh:
                return int(json.load(fh).get("used", 0) or 0)
        except (OSError, ValueError, TypeError):
            return 0

    def _write_file(self) -> None:
        if not self._counter_file:
            return
        tmp = f"{self._counter_file}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"used": self._used}, fh)
            os.replace(tmp, self._counter_file)
        except OSError:
            pass  # in-memory count stays authoritative for this process

    def spend(self, n: int = 1) -> int:
        """Consume ``n`` calls; raise when the cap would be exceeded."""
        with self._lock:
            new_used = self._used + int(n)
            if self.max_calls > 0 and new_used > self.max_calls:
                raise HostedWriterError(
                    f"hosted call budget exhausted: {self._used} used + "
                    f"{n} requested exceeds the cap of {self.max_calls} calls"
                )
            self._used = new_used
            self._write_file()
            return self._used


# --------------------------------------------------------------------------
# model status / provider
# --------------------------------------------------------------------------


@dataclass
class HostedModelStatus:
    """Status object shaped like the local runtime's (planner reads size_bytes)."""

    name: str
    path: str = ""
    size_bytes: int = 0
    state: str = "READY"
    hosted: bool = True
    backend: str = "hosted"
    error_message: str = ""


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content or "")


class HostedWriterProvider:
    """OpenAI-compatible hosted chat backend behind the local inference surface."""

    # Duck-type marker: executors use this instead of loading a local model.
    is_writer_provider = True

    def __init__(
        self,
        settings: Any = None,
        *,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_style: Optional[str] = None,
        timeout: Optional[float] = None,
        max_request_chars: Optional[int] = None,
        budget: Optional[CallBudget] = None,
        env: Optional[dict] = None,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        get = settings.get if settings is not None else (lambda _k, d=None: d)

        def _pick(value: Any, key: str, default: Any) -> Any:
            if value is not None:
                return value
            try:
                got = get(key, default)
            except Exception:  # noqa: BLE001
                got = default
            return default if got is None else got

        self.base_url = str(_pick(base_url, "hosted.base_url", DEFAULT_BASE_URL)).rstrip("/")
        self.model = str(_pick(model, "hosted.model", DEFAULT_MODEL))
        self.api_style = str(_pick(api_style, "hosted.api_style", "auto"))
        self.timeout = float(_pick(timeout, "hosted.timeout_seconds", DEFAULT_TIMEOUT_S))
        self.max_request_chars = int(
            _pick(max_request_chars, "hosted.max_request_chars", DEFAULT_MAX_REQUEST_CHARS)
        )
        if budget is not None:
            self.budget = budget
        else:
            try:
                cap = int(get("hosted.max_calls", 0) or 0)
            except Exception:  # noqa: BLE001
                cap = 0
            self.budget = CallBudget(cap)
        self._env = env if env is not None else os.environ
        self._opener = opener or urllib.request.urlopen
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.history: list[dict] = []

    # -------------------------------------------------------------- surface

    @property
    def style(self) -> str:
        return api_style_for(self.model, self.api_style)

    @property
    def endpoint(self) -> str:
        path = {"chat": "/chat/completions", "messages": "/messages", "responses": "/responses"}[
            self.style
        ]
        return f"{self.base_url}{path}"

    def api_key(self) -> str:
        """The key — read ONLY from the environment, at call time."""
        return str(self._env.get(KEY_ENV) or "")

    def _require_key(self) -> str:
        key = self.api_key()
        if not key.strip():
            raise HostedWriterError(
                f"{KEY_ENV} is not set - the hosted writer refuses to call without it"
            )
        return key

    def get_model_status(self) -> HostedModelStatus:
        return HostedModelStatus(name=self.model)

    def get_health(self) -> dict:
        """Health for planner/GUI gates. ``model_loaded`` is True only when
        the key exists: without it the hosted backend cannot answer, so the
        local gates keep their honest "not ready" behaviour."""
        key_present = bool(self.api_key().strip())
        return {
            "model_loaded": key_present,
            "model_state": "hosted" if key_present else "hosted_key_missing",
            "hosted": True,
            "hosted_model": self.model,
            "hosted_base_url": self.base_url,
            "hosted_api_style": self.style,
            "key_present": key_present,
            "calls_used": self.budget.used,
            "calls_cap": self.budget.max_calls,
            "backend": "hosted",
        }

    def load_model(self, *_args: Any, **_kwargs: Any) -> dict:
        """Nothing local to load: hosted mode is always 'loaded' (key permitting)."""
        return {"success": True, "model_name": self.model, "hosted": True}

    def unload_model(self) -> None:
        """No-op: the hosted endpoint holds no local process."""

    def cancel(self) -> None:
        """Mark the in-flight call abandoned; the next call starts clean."""
        self._cancel.set()

    def indicator(self) -> str:
        """Human-readable hosted-mode indicator (never contains the key)."""
        return f"hosted writer: {self.model} @ {self.base_url} ({self.style})"

    # ------------------------------------------------------------ history

    def _record(self, entry: dict) -> None:
        with self._lock:
            self.history.append(entry)
            if len(self.history) > MAX_HISTORY_ENTRIES:
                del self.history[: len(self.history) - MAX_HISTORY_ENTRIES]

    # ------------------------------------------------------------ payloads

    def _payload_for(self, request: ChatRequest, stream: bool) -> dict:
        secrets = (self.api_key(),)
        messages: list[dict] = []
        for message in request.messages:
            content = message.content
            if isinstance(content, str):
                content = redact(content, secrets)
            elif isinstance(content, list):
                content = [
                    {**part, "text": redact(str(part.get("text", "")), secrets)}
                    if isinstance(part, dict) and "text" in part
                    else part
                    for part in content
                ]
            messages.append({"role": message.role, "content": content})

        style = self.style
        if style == "chat":
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": int(request.max_tokens),
                "temperature": float(request.temperature),
                "stream": bool(stream),
            }
            if request.stop:
                payload["stop"] = list(request.stop)
            return payload

        if style == "messages":
            system_parts = [
                _content_to_text(m["content"]) for m in messages if m["role"] == "system"
            ]
            conversational = [m for m in messages if m["role"] != "system"]
            payload = {
                "model": self.model,
                "max_tokens": max(1, int(request.max_tokens)),
                "messages": conversational,
                "temperature": float(request.temperature),
                "stream": bool(stream),
            }
            if system_parts:
                payload["system"] = "\n\n".join(system_parts)
            if request.stop:
                payload["stop_sequences"] = list(request.stop)
            return payload

        # responses
        payload = {
            "model": self.model,
            "input": [
                {"role": m["role"], "content": _content_to_text(m["content"])}
                for m in messages
            ],
            "max_output_tokens": max(1, int(request.max_tokens)),
            "temperature": float(request.temperature),
            "stream": bool(stream),
        }
        if request.stop:
            payload["stop"] = list(request.stop)
        return payload

    # ------------------------------------------------------------ transport

    def _prepare(self, request: ChatRequest, stream: bool) -> tuple[dict, str, dict]:
        """Key check, payload build, size cap, budget spend, history meta."""
        if request.images:
            raise HostedWriterError(
                "hosted writer does not send images; vision runs on the local model"
            )
        key = self._require_key()
        payload = self._payload_for(request, stream=stream)
        blob = json.dumps(payload, ensure_ascii=False)
        if len(blob) > self.max_request_chars:
            raise HostedWriterError(
                f"hosted request size cap exceeded: {len(blob)} > "
                f"{self.max_request_chars} chars (hosted.max_request_chars)"
            )
        self.budget.spend(1)
        meta = {
            "t": time.time(),
            "style": self.style,
            "model": self.model,
            "url": self.endpoint,
            "payload_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
            "payload_chars": len(blob),
            "stream": bool(stream),
        }
        return payload, key, meta

    def _headers(self, key: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        }
        if self.style == "messages":
            headers["x-api-key"] = key
            headers["anthropic-version"] = ANTHROPIC_VERSION
        return headers

    def _raise_http(self, exc: urllib.error.HTTPError, meta: dict) -> None:
        try:
            body = exc.read(600) or b""
        except Exception:  # noqa: BLE001
            body = b""
        detail = redact(body.decode("utf-8", errors="replace"), (self.api_key(),))
        status = int(getattr(exc, "code", 0) or 0)
        if status in (401, 403):
            message = (
                f"hosted endpoint rejected the key (HTTP {status}) - check {KEY_ENV}"
            )
        elif status == 404:
            message = (
                f"hosted endpoint not found (HTTP 404): {meta['url']} "
                f"(model={self.model})"
            )
        elif status == 429:
            message = f"hosted endpoint rate-limited the call (HTTP 429): {detail[:200]}"
        else:
            message = f"hosted call failed (HTTP {status}): {detail[:300]}"
        self._record({**meta, "status": f"http_{status}"})
        raise HostedWriterError(message) from None

    @staticmethod
    def _extract_delta(style: str, data: dict) -> tuple[str, Optional[str], Optional[int]]:
        """One SSE data object -> (text_piece, finish_reason, completion_tokens)."""
        if style == "chat":
            choice = (data.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            piece = delta.get("content") or choice.get("text") or ""
            finish = choice.get("finish_reason")
            usage = data.get("usage") or {}
            toks = usage.get("completion_tokens")
            return piece, finish, toks
        if style == "messages":
            if data.get("type") == "content_block_delta":
                delta = data.get("delta") or {}
                return delta.get("text") or "", None, None
            if data.get("type") == "message_delta":
                usage = data.get("usage") or {}
                return "", data.get("delta", {}).get("stop_reason"), usage.get("output_tokens")
            return "", None, None
        # responses
        kind = data.get("type")
        if kind == "response.output_text.delta":
            return data.get("delta") or "", None, None
        if kind in ("response.completed", "response.done"):
            usage = data.get("response", {}).get("usage") or {}
            return "", "stop", usage.get("output_tokens")
        if kind == "response.output_text.done":
            return "", None, None
        return "", None, None

    @staticmethod
    def _parse_body(style: str, body: str) -> tuple[str, str, int, int]:
        """Full (non-stream) response body -> (text, finish, in_tokens, out_tokens)."""
        data = json.loads(body)
        if style == "chat":
            choice = (data.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content")) or choice.get("text") or ""
            finish = str(choice.get("finish_reason") or "stop")
            usage = data.get("usage") or {}
            return text, finish, int(usage.get("prompt_tokens") or 0), int(
                usage.get("completion_tokens") or 0
            )
        if style == "messages":
            blocks = data.get("content") or []
            text = "".join(
                b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
            )
            finish = str(data.get("stop_reason") or "stop")
            usage = data.get("usage") or {}
            return text, finish, int(usage.get("input_tokens") or 0), int(
                usage.get("output_tokens") or 0
            )
        # responses
        text = data.get("output_text")
        if not text:
            pieces = []
            for item in data.get("output") or []:
                for block in item.get("content") or []:
                    if block.get("type") in ("output_text", "text"):
                        pieces.append(block.get("text") or "")
            text = "".join(pieces)
        status = str(data.get("status") or "completed")
        finish = "stop" if status in ("completed", "completed") else status
        usage = data.get("usage") or {}
        return text or "", finish, int(usage.get("input_tokens") or 0), int(
            usage.get("output_tokens") or 0
        )

    def chat_generate(self, request: ChatRequest) -> ChatResponse:
        """Blocking hosted completion -> local :class:`ChatResponse`."""
        payload, key, meta = self._prepare(request, stream=False)
        self._cancel.clear()
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=blob, headers=self._headers(key), method="POST")
        started = time.time()
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            self._raise_http(exc, meta)
            raise  # pragma: no cover - _raise_http always raises
        except urllib.error.URLError as exc:
            reason = redact(str(getattr(exc, "reason", exc)), (key,))
            self._record({**meta, "status": "transport_error"})
            raise HostedWriterError(f"hosted call failed: {reason}") from None
        except TimeoutError as exc:
            self._record({**meta, "status": "timeout"})
            raise HostedWriterError(f"hosted call timed out after {self.timeout:g}s") from None
        try:
            text, finish, in_tok, out_tok = self._parse_body(self.style, body)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            self._record({**meta, "status": "bad_response"})
            raise HostedWriterError(
                f"hosted response was not parseable ({type(exc).__name__})"
            ) from None
        self._record({
            **meta,
            "status": "ok",
            "in_tokens": in_tok,
            "out_tokens": out_tok,
            "elapsed_ms": int((time.time() - started) * 1000),
        })
        return ChatResponse(
            text=text, tokens_generated=out_tok, finish_reason=finish, request_id=None
        )

    def chat_generate_stream(
        self,
        request: ChatRequest,
        cancel_event: Optional[threading.Event] = None,
        read_timeout: float = 5.0,
    ):
        """Streamed hosted completion yielding ``(piece, finish, tokens)``.

        SSE is consumed per style; a server that ignores ``stream`` and
        returns one JSON body is handled as a single chunk.  Cancellation
        (the caller's ``cancel_event`` or :meth:`cancel`) stops the loop.
        ``read_timeout`` paces reads for the inactivity watchdog.
        """
        payload, key, meta = self._prepare(request, stream=True)
        self._cancel.clear()
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=blob, headers=self._headers(key), method="POST")
        style = self.style
        pieces: list[str] = []
        finish = "stop"
        tokens = 0
        started = time.time()
        status = "ok"

        def _stop() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                return True
            return self._cancel.is_set()

        resp = None
        try:
            try:
                resp = self._opener(req, timeout=self.timeout)
                first = resp.readline()
            except urllib.error.HTTPError as exc:
                self._raise_http(exc, meta)
                raise  # pragma: no cover
            except urllib.error.URLError as exc:
                reason = redact(str(getattr(exc, "reason", exc)), (key,))
                self._record({**meta, "status": "transport_error"})
                raise HostedWriterError(f"hosted call failed: {reason}") from None
            except TimeoutError:
                self._record({**meta, "status": "timeout"})
                raise HostedWriterError(f"hosted call timed out after {self.timeout:g}s") from None

            if first and not first.lstrip().startswith((b"data:", b"event:", b"{")):
                # Headers/leading noise some gateways emit: skip until SSE/JSON.
                line = first
                while line and not line.lstrip().startswith((b"data:", b"event:", b"{")):
                    line = resp.readline()
                first = line

            if first and first.lstrip().startswith(b"{"):
                # Non-streaming server: the whole body is one JSON document.
                rest = resp.read() if resp is not None else b""
                body = (first + (rest or b"")).decode("utf-8", errors="replace")
                text, finish, _in_tok, out_tok = self._parse_body(style, body)
                pieces.append(text)
                tokens = int(out_tok or 0)
                if text:
                    yield (text, None, 0)
            else:
                data_lines: list[bytes] = []

                def _flush() -> tuple[str, Optional[str], Optional[int]]:
                    joined = b"\n".join(data_lines).decode("utf-8", errors="replace").strip()
                    data_lines.clear()
                    if not joined or joined == "[DONE]":
                        return "", None, None
                    try:
                        obj = json.loads(joined)
                    except ValueError:
                        return "", None, None
                    if not isinstance(obj, dict):
                        return "", None, None
                    return self._extract_delta(style, obj)

                line = first
                saw_done = False
                while line:
                    stripped = line.strip()
                    if stripped.startswith(b"data:"):
                        data_lines.append(stripped[len(b"data:"):].strip())
                        if stripped[len(b"data:"):].strip() == b"[DONE]":
                            saw_done = True
                    elif stripped.startswith(b"event:") and data_lines:
                        pass
                    elif not stripped:
                        piece, fin, toks = _flush()
                        if piece:
                            pieces.append(piece)
                            yield (piece, None, 0)
                        if fin:
                            finish = str(fin)
                        if toks is not None:
                            tokens = int(toks)
                        if saw_done:
                            break
                    if _stop():
                        status = "cancelled"
                        break
                    if saw_done:
                        piece, fin, toks = _flush()
                        if piece:
                            pieces.append(piece)
                            yield (piece, None, 0)
                        if fin:
                            finish = str(fin)
                        if toks is not None:
                            tokens = int(toks)
                        break
                    try:
                        line = resp.readline()
                    except (TimeoutError, OSError):
                        status = "read_timeout"
                        break
                else:
                    piece, fin, toks = _flush()
                    if piece:
                        pieces.append(piece)
                        yield (piece, None, 0)
                    if fin:
                        finish = str(fin)
                    if toks is not None:
                        tokens = int(toks)
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
            self._record({
                **meta,
                "status": status,
                "out_tokens": tokens,
                "elapsed_ms": int((time.time() - started) * 1000),
            })
        yield ("", finish, tokens)

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Legacy single-prompt surface, mapped onto hosted chat."""
        chat = ChatRequest(
            messages=[ChatMessage(role="user", content=request.prompt)],
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            stop=request.stop,
            request_id=request.request_id,
        )
        response = self.chat_generate(chat)
        return GenerationResponse(
            text=response.text,
            request_id=response.request_id,
            tokens_generated=response.tokens_generated,
            finish_reason=response.finish_reason,
        )


# --------------------------------------------------------------------------
# recorded-response fake (tests + window drive; never touches the network)
# --------------------------------------------------------------------------


@dataclass
class _ScriptEntry:
    match: Optional[str] = None
    response: str = ""
    max_calls: int = 0  # 0 = unlimited
    used: int = 0
    regex: bool = False


class RecordedResponseProvider:
    """Replays recorded responses behind the same inference surface.

    Entries are matched (first unused match, in order) against the combined
    text of the outgoing messages; ``match=None`` is the catch-all.  An entry
    with ``max_calls`` stops matching once exhausted, which is how attempt 1
    and attempt 2 can receive different recorded responses.  Strict mode
    (default) raises when nothing matches, so a drive fails loudly instead
    of silently answering with a guess.
    """

    is_writer_provider = True

    def __init__(
        self,
        entries: Optional[list] = None,
        *,
        script_path: Optional[str] = None,
        model: str = "recorded-fake",
        strict: bool = True,
        budget: Optional[CallBudget] = None,
    ) -> None:
        self.model = model
        self.strict = bool(strict)
        self.budget = budget or CallBudget(0)
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.history: list[dict] = []
        self.calls: list[str] = []
        self._entries: list[_ScriptEntry] = []
        raw = list(entries or [])
        if script_path:
            with open(script_path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            if isinstance(doc, dict):
                self.model = str(doc.get("model") or self.model)
                raw = list(doc.get("responses") or [])
            elif isinstance(doc, list):
                raw = doc
        for item in raw:
            if isinstance(item, _ScriptEntry):
                self._entries.append(item)
            elif isinstance(item, dict):
                self._entries.append(
                    _ScriptEntry(
                        match=item.get("match"),
                        response=str(item.get("response", "")),
                        max_calls=int(item.get("max_calls") or 0),
                        regex=bool(item.get("regex", False)),
                    )
                )
            elif isinstance(item, tuple):
                self._entries.append(_ScriptEntry(match=item[0], response=str(item[1])))
            else:
                self._entries.append(_ScriptEntry(response=str(item)))

    @classmethod
    def from_script(cls, path: str, **kwargs: Any) -> "RecordedResponseProvider":
        return cls(script_path=path, **kwargs)

    # -------------------------------------------------------------- surface

    def api_key(self) -> str:
        return ""

    def style(self) -> str:  # pragma: no cover - parity helper
        return "chat"

    def get_model_status(self) -> HostedModelStatus:
        return HostedModelStatus(name=self.model, backend="recorded")

    def get_health(self) -> dict:
        return {
            "model_loaded": True,
            "model_state": "recorded",
            "hosted": True,
            "recorded": True,
            "hosted_model": self.model,
            "key_present": False,
            "calls_used": self.budget.used,
            "calls_cap": self.budget.max_calls,
            "backend": "recorded",
        }

    def load_model(self, *_a: Any, **_k: Any) -> dict:
        return {"success": True, "model_name": self.model, "hosted": True}

    def unload_model(self) -> None:
        return None

    def cancel(self) -> None:
        self._cancel.set()

    def indicator(self) -> str:
        return f"recorded fake provider: {self.model} ({len(self._entries)} entries)"

    # ------------------------------------------------------------ matching

    @staticmethod
    def _message_text(request: ChatRequest) -> str:
        return "\n".join(_content_to_text(m.content) for m in request.messages)

    def _next_response(self, text: str) -> str:
        with self._lock:
            for entry in self._entries:
                if entry.max_calls and entry.used >= entry.max_calls:
                    continue
                if entry.match is None:
                    entry.used += 1
                    return entry.response
                if entry.regex:
                    matched = re.search(entry.match, text, re.DOTALL) is not None
                else:
                    matched = entry.match in text
                if matched:
                    entry.used += 1
                    return entry.response
        if not self.strict:
            return ""
        needle = None
        for entry in self._entries:
            if entry.match:
                needle = entry.match
                break
        raise HostedWriterError(
            "recorded provider: no recorded response matches this request"
            + (f" (expected marker {needle!r})" if needle else "")
        )

    def chat_generate(self, request: ChatRequest) -> ChatResponse:
        self._cancel.clear()
        text = self._message_text(request)
        self.budget.spend(1)
        response = self._next_response(text)
        self.calls.append(text[:4000])
        self.history.append({
            "t": time.time(),
            "model": self.model,
            "prompt_chars": len(text),
            "response_chars": len(response),
        })
        return ChatResponse(text=response, tokens_generated=0, finish_reason="stop")

    def chat_generate_stream(
        self,
        request: ChatRequest,
        cancel_event: Optional[threading.Event] = None,
        read_timeout: float = 5.0,
    ):
        response = self.chat_generate(request)
        if response.text:
            yield (response.text, None, 0)
        yield ("", "stop", 0)

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        chat = ChatRequest(
            messages=[ChatMessage(role="user", content=request.prompt)],
            max_tokens=request.max_tokens,
            temperature=request.temperature,
        )
        response = self.chat_generate(chat)
        return GenerationResponse(
            text=response.text,
            tokens_generated=response.tokens_generated,
            finish_reason=response.finish_reason,
        )


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------


def build_writer_provider(
    settings: Any = None,
    *,
    budget: Optional[CallBudget] = None,
    env: Optional[dict] = None,
    opener: Optional[Callable[..., Any]] = None,
    project_root: Optional[str] = None,
) -> Optional[Any]:
    """Hosted provider when enabled (per project), else ``None`` (stay local).

    Per-project: ``hosted.project_overrides`` maps project root -> bool and
    wins over the global ``hosted.enabled`` for that project.
    """
    if settings is None:
        return None
    enabled = hosted_enabled(settings)
    if project_root:
        try:
            overrides = settings.get("hosted.project_overrides", {}) or {}
        except Exception:  # noqa: BLE001
            overrides = {}
        norm = os.path.normcase(os.path.abspath(str(project_root)))
        for path, value in dict(overrides).items():
            if os.path.normcase(os.path.abspath(str(path))) == norm:
                enabled = bool(value)
                break
    if not enabled:
        return None
    return HostedWriterProvider(settings, budget=budget, env=env, opener=opener)
