"""Serve a provider's model through GisCode's inference surface.

``InferenceAPI`` runs a model GisCode owns: it loads a GGUF through
``llama-server`` and reports the state of that process. A model that already
lives in Ollama, LM Studio or another local server cannot be loaded by us - the
provider owns its lifecycle - but GisCode's planner and coding agent do not care
*who* runs the model. They need four answers and one call:

* ``get_health()`` - is anything able to answer right now?
* ``get_model_status()`` - which model, in what state?
* ``chat_generate(request)`` - here are the messages, give me the reply.
* ``cancel()`` - abandon the in-flight request.

This class answers those from the provider's own API, so the same planner,
executor, approval gate and verifier run unchanged. It never claims a model is
loaded when the provider does not list it, never pretends to stream, and never
unloads a model: another application may be using it.

Cancellation is honest about its limit. An HTTP call that is already in flight
cannot be torn down from here, so ``cancel()`` marks the request abandoned and
the reply is discarded with :class:`RequestCancelledError` - the caller is told
the answer was dropped, not that generation stopped server-side.
"""

from __future__ import annotations

import base64
import inspect
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from giscode.inference.errors import (
    InferenceTimeoutError,
    InferenceUnavailableError,
    ModelNotFoundError,
    RequestCancelledError,
)
from giscode.inference.lifecycle import ModelState
from giscode.inference.runtime import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    GenerationRequest,
    GenerationResponse,
)
from giscode.providers.base import OLLAMA, Provider, ProviderError, is_loopback
from giscode.providers.redaction import redact_messages, redact_text
from giscode.providers.streaming import StreamCancelled, StreamTimeout

#: A local model can be slow to first token; the caller may override this.
DEFAULT_CHAT_TIMEOUT = 180.0

#: Extensions GisCode is willing to hand to a vision model, as base64.
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


@dataclass(frozen=True)
class ProviderModelStatus:
    """The model status shape the planner and loop read (``name``/``path``/``state``).

    A frozen stand-in for :class:`giscode.inference.lifecycle.ModelInfo`: the
    provider owns the file, so there is no local path to report and ``path`` is
    the provider's model id. ``state`` is a real :class:`ModelState` so any
    caller comparing against ``ModelState.READY`` keeps working.
    """

    name: str
    path: str
    state: object = ModelState.READY
    error_message: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "state": getattr(self.state, "name", str(self.state)),
            "error_message": self.error_message,
        }


@dataclass
class _RequestState:
    """Cancellation state owned by exactly one in-flight request."""

    request_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    completed: bool = False


class ProviderChatBackend:
    """An ``InferenceAPI``-shaped view of one model behind one provider."""

    #: The in-process llama.cpp runtime keeps this False; here it is genuinely
    #: true, because the provider serves chat itself.
    supports_chat_generate = True

    def __init__(
        self,
        provider: Provider,
        model: str,
        *,
        purpose: str = "coding",
        timeout: float = DEFAULT_CHAT_TIMEOUT,
        allow_remote: bool = False,
        attachment_consent: bool = False,
    ) -> None:
        if not str(model or "").strip():
            raise ModelNotFoundError(
                f"the {provider.label or provider.kind} profile has no model selected"
            )
        self.provider = provider
        self.model = str(model).strip()
        self.purpose = purpose
        self.timeout = max(5.0, float(timeout))
        self.allow_remote = bool(allow_remote)
        # Sending a picture off this PC is a separate decision from letting a
        # remote endpoint answer text. It defaults to no and is only ever turned
        # on by the user.
        self._attachment_consent = bool(attachment_consent)
        self._lock = threading.Lock()
        #: Every request owns its own event. Multiple requests can stream at
        #: once without one Stop changing another request's state.
        self._active_requests: dict[int, _RequestState] = {}
        self._next_request_token = 0
        #: True when an unnamed stop arrived while nothing was running, which
        #: means "do not start the next one".
        self._pre_start_cancel = False
        #: Named stops that arrived before their request registered. They are
        #: tied to that request id and are consumed by its registration.
        self._prestart_cancel_ids: set[str] = set()
        #: Ids that already finished. A late named Stop must not poison the next
        #: request that happens to reuse an old id.
        self._completed_ids: set[str] = set()
        # Probing the provider on every planner step would hammer a local
        # server; a short cache keeps "is it there" honest without the noise.
        self._probe: Optional[tuple[float, bool, str]] = None

    # ------------------------------------------------------------- identity
    def describe(self) -> str:
        label = self.provider.label or self.provider.kind
        return f"{label} · {self.model}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ProviderChatBackend {self.describe()} at {self.provider.secret_free_endpoint()}>"

    # ------------------------------------------------------------- probing
    def _refresh_probe(self) -> tuple[bool, str]:
        now = time.monotonic()
        if self._probe is not None and now - self._probe[0] < 2.0:
            return self._probe[1], self._probe[2]
        try:
            models = self.provider.list_models()
        except ProviderError as exc:
            result = (
                False,
                redact_text(
                    str(exc),
                    getattr(self.provider, "secret_values", lambda: ())(),
                ),
            )
        except Exception as exc:  # a broken provider must not crash a task
            result = (False, f"{type(exc).__name__}: {exc}")
        else:
            ids = {item.id for item in models}
            if self.model in ids:
                result = (True, "")
            else:
                result = (
                    False,
                    f"{self.model!r} is not installed in "
                    f"{self.provider.label or self.provider.kind}",
                )
        self._probe = (now, result[0], result[1])
        return result

    def _model_meta(self):
        try:
            for item in self.provider.list_models():
                if item.id == self.model:
                    return item
        except Exception:
            return None
        return None

    @property
    def supports_vision(self) -> bool:
        """Metadata first; an explicit per-provider declaration only as a fallback.

        The declaration exists for servers that publish no capability list. It is
        never inferred from a model's name, so an ambiguously named model does not
        get an image it cannot read.
        """
        if getattr(self.provider, "allow_vision", False):
            return True
        meta = self._model_meta()
        return bool(meta and "vision" in {c.lower() for c in meta.capabilities})

    @property
    def is_remote(self) -> bool:
        """True when this provider's endpoint is not on this machine."""
        return not is_loopback(getattr(self.provider, "endpoint", ""))

    @property
    def allows_attachments_off_pc(self) -> bool:
        return (not self.is_remote) or self._attachment_consent

    @property
    def available(self) -> bool:
        return self._refresh_probe()[0]

    @property
    def supports_streaming(self) -> bool:
        """True only when the provider really delivers incremental deltas."""
        return bool(getattr(self.provider, "supports_streaming", False))

    # ------------------------------------------------------------- API surface
    def get_health(self) -> dict:
        ok, why = self._refresh_probe()
        return {
            "available": ok,
            "model_loaded": ok,
            "model_state": self.model if ok else None,
            "load_state": "external" if ok else "unavailable",
            "load_error": "" if ok else why,
            "provider": self.provider.kind,
            "endpoint": self.provider.secret_free_endpoint(),
            "model": self.model,
        }

    def get_model_status(self) -> Optional[ProviderModelStatus]:
        ok, why = self._refresh_probe()
        if not ok:
            return None
        return ProviderModelStatus(name=self.model, path=self.model)

    def list_models(self):
        return self.provider.list_models()

    def load_model(self, model_path: str = "", **_kwargs):
        """Confirm the provider has the model; the provider does the loading."""
        target = str(model_path or self.model).strip()
        try:
            ids = {item.id for item in self.provider.list_models()}
        except ProviderError as exc:
            raise InferenceUnavailableError(str(exc)) from exc
        if target not in ids:
            raise ModelNotFoundError(
                f"{target!r} is not installed in "
                f"{self.provider.label or self.provider.kind}"
            )
        self.model = target
        self._probe = None
        return ProviderModelStatus(name=self.model, path=self.model)

    def unload_model(self) -> None:
        """Deliberately a no-op: the model belongs to the provider, not to us."""
        return None

    def cancel(self, request_id: Optional[str] = None) -> bool:
        """Ask the in-flight request to stop, without touching another one.

        A named cancel affects only requests with that id. An unnamed cancel is
        the legacy Stop path and abandons every request currently in flight;
        when nothing is running it applies to the next request.
        """
        with self._lock:
            active = [
                state
                for state in self._active_requests.values()
                if not state.completed
            ]
            if request_id not in (None, ""):
                request_id = str(request_id)
                matches = [
                    state for state in active if state.request_id == request_id
                ]
                if matches:
                    for state in matches:
                        state.cancel_event.set()
                    return True
                if active:
                    # A named Stop for another request must not touch this one.
                    return False
                if request_id in self._completed_ids:
                    # This answer already finished; a late Stop is stale.
                    return False
                # The request has not registered yet. Remember the exact id so
                # its registration is refused, while a different request is
                # still allowed to start.
                self._prestart_cancel_ids.add(request_id)
                return True
            if active:
                for state in active:
                    state.cancel_event.set()
                return True
            self._pre_start_cancel = True
        return True

    def _consume_pre_start_cancel(self) -> bool:
        """Consume an unnamed Stop that arrived before any request began."""
        with self._lock:
            if not self._pre_start_cancel:
                return False
            self._pre_start_cancel = False
            return True

    def _register_request(self, request_id: str) -> Optional[tuple[int, _RequestState]]:
        """Atomically register a request or consume its pre-registration Stop.

        The pre-start check and insertion share one lock acquisition. A Stop
        racing registration therefore either sees no active request (and is
        consumed here) or sees this registered request and sets its own event;
        it can never fall between two separate operations.
        """
        request_id = str(request_id or "")
        with self._lock:
            if self._pre_start_cancel:
                self._pre_start_cancel = False
                return None
            if request_id and request_id in self._prestart_cancel_ids:
                self._prestart_cancel_ids.discard(request_id)
                return None
            self._next_request_token += 1
            token = self._next_request_token
            state = _RequestState(request_id=request_id)
            self._active_requests[token] = state
            return token, state

    def _begin_request(self, request_id: str) -> tuple[int, _RequestState]:
        """Register one request before its transport starts."""
        registration = self._register_request(request_id)
        if registration is None:
            raise RequestCancelledError(
                f"the request for {self.describe()} was cancelled before it started"
            )
        return registration

    def _mark_complete(self, token: int) -> bool:
        """Publish completion atomically; a simultaneous Stop wins the race."""
        with self._lock:
            state = self._active_requests.get(token)
            if state is None or state.cancel_event.is_set():
                return False
            state.completed = True
            if state.request_id:
                self._completed_ids.add(state.request_id)
            self._active_requests.pop(token, None)
            return True

    def _finish_request(self, token: int) -> None:
        with self._lock:
            self._active_requests.pop(token, None)

    def generate(self, request: GenerationRequest) -> GenerationResponse:
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
            provider=response.provider,
            model=response.model,
        )

    def chat_generate_stream(
        self, request: ChatRequest, cancel_event=None, read_timeout: float = 5.0
    ):
        """Stream the provider's answer as ``(piece, finish, tokens)`` events.

        Same shape as :meth:`InferenceAPI.chat_generate_stream`, so Chat does
        not care whether the built-in runtime or a provider is answering. Text
        is yielded exactly as it arrived; the final event carries the finish
        reason and the provider's token count.

        Cancelling raises :class:`RequestCancelledError` with ``partial_text``
        set, and an idle deadline raises :class:`InferenceTimeoutError` the same
        way, so the caller can keep what was displayed and label it honestly.
        A stream that ends without a completion signal is an error, never an
        empty success.
        """
        if not self.supports_streaming:
            raise InferenceUnavailableError(
                f"{self.describe()} answers in a single response; this provider does "
                "not stream, so GisCode will not show a fake partial answer."
            )
        # Privacy is decided first: an image that must not leave this PC is
        # refused before the probe and before the stream opens.
        if getattr(request, "images", None) and not self.allows_attachments_off_pc:
            raise InferenceUnavailableError(
                "This image was not sent. "
                f"{self.describe()} is a remote endpoint "
                f"({self.provider.secret_free_endpoint()}), and sending an "
                "attachment off this PC needs your explicit consent. Enable "
                "'Allow images to remote providers' in Model Manager, or switch "
                "to a local provider. Nothing left this computer."
            )
        ok, why = self._refresh_probe()
        if not ok:
            raise InferenceUnavailableError(
                f"{self.describe()} cannot answer: {why}. "
                f"Start {self.provider.label or self.provider.kind} or pick another "
                "model in Model Manager."
            )
        # Refusals happen here, before a single byte is sent - and on the call
        # itself, not on the first ``next()``: a caller that never iterates must
        # still learn that this provider cannot stream or that consent is
        # missing. A generator function would defer every one of these checks.
        messages = self._messages_for(request)
        request_id = str(getattr(request, "request_id", "") or "")
        registration = self._register_request(request_id)
        if registration is None:
            raise RequestCancelledError(
                f"the request for {self.describe()} was cancelled before it started"
            )
        token, state = registration
        return self._stream_deltas(
            request, messages, request_id, cancel_event, read_timeout, token, state
        )

    def _stream_deltas(
        self,
        request: ChatRequest,
        messages: list[dict],
        request_id: str,
        cancel_event,
        read_timeout: float,
        token: int,
        state: _RequestState,
    ):
        """The generator half of :meth:`chat_generate_stream`."""
        pieces: list[str] = []
        finish = "stop"
        tokens = 0
        completed = False
        idle_limit = max(1.0, float(read_timeout or 5.0))

        def _stop() -> bool:
            if state.cancel_event.is_set():
                return True
            return bool(cancel_event is not None and cancel_event.is_set())

        try:
            kwargs = dict(
                max_tokens=int(request.max_tokens or 512),
                temperature=float(request.temperature),
                timeout=self.timeout,
                # ``read_timeout`` is the caller's inactivity budget, so a test
                # or a user-defined limit can be shorter than the chat timeout.
                idle_timeout=idle_limit,
                should_stop=_stop,
            )
            # Providers that can attribute usage to a request accept this; the
            # legacy local providers do not, so the capability is inspected
            # rather than relying on a broad except TypeError (which could
            # hide a real provider bug and duplicate a billable call).
            try:
                accepts_request_id = "request_id" in inspect.signature(
                    self.provider.stream_chat
                ).parameters
            except (TypeError, ValueError):
                accepts_request_id = False
            if accepts_request_id:
                kwargs["request_id"] = request_id
            for delta in self.provider.stream_chat(self.model, messages, **kwargs):
                if _stop():
                    raise self._cancelled_error(pieces)
                if delta.text:
                    pieces.append(delta.text)
                    yield (delta.text, None, None)
                if delta.done:
                    completed = True
                    finish = delta.finish_reason or "stop"
                    tokens = int(delta.tokens or 0)
            if not completed:
                if _stop():
                    raise self._cancelled_error(pieces)
                error = InferenceUnavailableError(
                    f"{self.describe()} stopped before the answer was complete. "
                    "The partial text is not a finished reply."
                )
                error.partial_text = "".join(pieces)
                raise error
            if _stop():
                raise self._cancelled_error(pieces)
            if not self._mark_complete(token):
                raise self._cancelled_error(pieces)
            yield ("", finish, tokens)
        except StreamCancelled:
            raise self._cancelled_error(pieces) from None
        except StreamTimeout as exc:
            limit = getattr(exc, "limit", None) or idle_limit
            if getattr(exc, "kind", "idle") == "request":
                detail = (
                    f"{self.describe()} did not answer within {limit:g}s; the "
                    "partial text was kept."
                )
            else:
                detail = (
                    f"{self.describe()} sent nothing for {limit:g}s before the "
                    "answer was complete; the partial text was kept."
                )
            error = InferenceTimeoutError(detail)
            error.partial_text = "".join(pieces)
            raise error from exc
        except RequestCancelledError:
            raise
        except ProviderError as exc:
            if _stop():
                # A Stop that raced a provider failure is still a Stop: report
                # cancellation, not a failure the user did not cause.
                raise self._cancelled_error(pieces) from None
            action = getattr(exc, "action", "") or (
                "Check that the provider is running and the model is installed."
            )
            secrets = getattr(self.provider, "secret_values", lambda: ())()
            error = InferenceUnavailableError(
                f"{self.describe()}: {redact_text(str(exc), secrets)} "
                f"{redact_text(str(action), secrets)}"
            )
            error.partial_text = "".join(pieces)
            raise error from exc
        finally:
            self._finish_request(token)

    def _cancelled_error(self, pieces: list[str]) -> RequestCancelledError:
        """A cancel that keeps what was already shown and says what it is."""
        text = "".join(pieces)
        error = RequestCancelledError(
            f"{self.describe()} was stopped; {len(text)} characters of the partial "
            "answer are kept and are not a completed reply."
        )
        error.partial_text = text
        return error

    def chat_generate(
        self, request: ChatRequest, cancel_event=None
    ) -> ChatResponse:
        # A caller with a stop signal gets the streaming transport, so a Stop
        # closes the connection and generation stops server-side instead of us
        # waiting for the complete blocking reply and discarding it afterwards.
        # A caller without one keeps the plain blocking request it asked for.
        # Text already received is kept on RequestCancelledError.partial_text.
        if self.supports_streaming and cancel_event is not None:
            pieces: list[str] = []
            finish = "stop"
            tokens = 0
            for piece, fin, toks in self.chat_generate_stream(
                request,
                cancel_event=cancel_event,
                # A one-shot caller has no display waiting for deltas: the
                # request timeout is the right idle budget, so a cold model
                # load is not mistaken for a dead stream. A Stop still closes
                # the connection within the cancel poll interval.
                read_timeout=self.timeout,
            ):
                if piece:
                    pieces.append(piece)
                if fin:
                    finish = str(fin)
                if toks:
                    tokens = int(toks)
            return ChatResponse(
                text="".join(pieces),
                request_id=request.request_id,
                tokens_generated=tokens,
                finish_reason=finish,
                provider=self.provider.kind,
                model=self.model,
            )
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError(
                f"the request for {self.describe()} was cancelled before it started"
            )
        if getattr(request, "images", None) and not self.allows_attachments_off_pc:
            # Refuse before the first byte leaves the machine - and before the
            # liveness probe, which is itself a network request. This is the one
            # boundary that must never depend on which model was selected.
            raise InferenceUnavailableError(
                "This image was not sent. "
                f"{self.describe()} is a remote endpoint "
                f"({self.provider.secret_free_endpoint()}), and sending an "
                "attachment off this PC needs your explicit consent. Enable "
                "'Allow images to remote providers' in Model Manager, or switch "
                "to a local provider. Nothing left this computer."
            )
        ok, why = self._refresh_probe()
        if not ok:
            raise InferenceUnavailableError(
                f"{self.describe()} cannot answer: {why}. "
                f"Start {self.provider.label or self.provider.kind} or pick another "
                "model in Model Manager."
            )
        messages = self._messages_for(request)
        request_id = str(getattr(request, "request_id", "") or "")
        registration = self._register_request(request_id)
        if registration is None:
            # A cancel that belongs to this request is a decision, not a stale
            # flag: consume it and do not spend the provider's time.
            raise RequestCancelledError(
                f"the request for {self.describe()} was cancelled before it started"
            )
        token, state = registration
        try:
            kwargs = dict(
                max_tokens=int(request.max_tokens or 512),
                temperature=float(request.temperature),
                timeout=self.timeout,
            )
            try:
                accepts_request_id = "request_id" in inspect.signature(
                    self.provider.chat
                ).parameters
            except (TypeError, ValueError):
                accepts_request_id = False
            if accepts_request_id:
                kwargs["request_id"] = request_id
            text = self.provider.chat(self.model, messages, **kwargs)
            if state.cancel_event.is_set():
                raise RequestCancelledError(
                    f"{self.describe()} finished after the request was cancelled; "
                    "its answer was discarded."
                )
            if not self._mark_complete(token):
                raise RequestCancelledError(
                    f"{self.describe()} finished after the request was cancelled; "
                    "its answer was discarded."
                )
        except ProviderError as exc:
            action = getattr(exc, "action", "") or (
                "Check that the provider is running and the model is installed."
            )
            secrets = getattr(self.provider, "secret_values", lambda: ())()
            raise InferenceUnavailableError(
                f"{self.describe()}: {redact_text(str(exc), secrets)} "
                f"{redact_text(str(action), secrets)}"
            ) from exc
        finally:
            self._finish_request(token)
        return ChatResponse(
            text=text,
            request_id=request.request_id,
            tokens_generated=0,
            finish_reason="stop",
            provider=self.provider.kind,
            model=self.model,
        )

    # ------------------------------------------------------------- messages
    def _messages_for(self, request: ChatRequest) -> list[dict]:
        images = list(getattr(request, "images", None) or [])
        if images and not self.supports_vision:
            raise InferenceUnavailableError(
                f"{self.describe()} is a text model; GisCode will not send an "
                "image to it and report an analysis it never did."
            )
        encoded = [self._encode_image(path) for path in images] if images else []
        # Multimodal content already shaped by the caller is passed through
        # untouched - we did not build it, so we do not rewrite it.
        converted: list[dict] = [
            {"role": message.role, "content": message.content}
            for message in request.messages
        ]
        if encoded:
            # Attach to the last user turn: that is the one carrying the question.
            for index in range(len(converted) - 1, -1, -1):
                if converted[index].get("role") == "user":
                    converted[index] = self._with_images(converted[index], encoded)
                    break
        if self.is_remote:
            # Last-line defence for every remote provider. The provider also
            # redacts its own requests; doing it here makes the boundary
            # independent of which provider implementation is selected.
            converted = redact_messages(
                converted, getattr(self.provider, "secret_values", lambda: ())()
            )
        return converted

    def _with_images(self, entry: dict, encoded: list[str]) -> dict:
        if self.provider.kind == OLLAMA:
            # Ollama takes base64 in the message itself.
            return {**entry, "images": encoded}
        # OpenAI-shaped servers take a content list.
        return {
            "role": entry["role"],
            "content": [{"type": "text", "text": entry["content"]}]
            + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}
                for data in encoded
            ],
        }

    @staticmethod
    def _encode_image(path: str) -> str:
        candidate = Path(str(path)).expanduser()
        if candidate.suffix.lower() not in _IMAGE_SUFFIXES:
            raise InferenceUnavailableError(
                f"{candidate.name} is not an image GisCode can attach."
            )
        try:
            return base64.b64encode(candidate.read_bytes()).decode("ascii")
        except OSError as exc:
            raise InferenceUnavailableError(
                f"the image could not be read: {exc}"
            ) from exc


