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
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from giscode.inference.errors import (
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
from giscode.providers.base import OLLAMA, Provider, ProviderError

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
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
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
            result = (False, str(exc))
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
    def available(self) -> bool:
        return self._refresh_probe()[0]

    @property
    def supports_streaming(self) -> bool:
        """False: every provider in this layer answers in one response."""
        return False

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

    def cancel(self) -> None:
        self._cancel_event.set()

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
        )

    def chat_generate_stream(
        self, request: ChatRequest, cancel_event=None, read_timeout: float = 5.0
    ):
        raise InferenceUnavailableError(
            f"{self.describe()} answers in a single response; this provider does "
            "not stream, so GisCode will not show a fake partial answer."
        )

    def chat_generate(self, request: ChatRequest) -> ChatResponse:
        ok, why = self._refresh_probe()
        if not ok:
            raise InferenceUnavailableError(
                f"{self.describe()} cannot answer: {why}. "
                f"Start {self.provider.label or self.provider.kind} or pick another "
                "model in Model Manager."
            )
        messages = self._messages_for(request)
        if self._cancel_event.is_set():
            # A cancel that arrived before the request did is a decision, not a
            # stale flag: consume it and do not spend the provider's time.
            self._cancel_event.clear()
            raise RequestCancelledError(
                f"the request for {self.describe()} was cancelled before it started"
            )
        self._cancel_event.clear()
        try:
            text = self.provider.chat(
                self.model,
                messages,
                max_tokens=int(request.max_tokens or 512),
                temperature=float(request.temperature),
                timeout=self.timeout,
            )
        except ProviderError as exc:
            action = getattr(exc, "action", "") or (
                "Check that the provider is running and the model is installed."
            )
            raise InferenceUnavailableError(f"{self.describe()}: {exc} {action}") from exc
        if self._cancel_event.is_set():
            # The reply arrived after the user stopped the task. Report the
            # abandonment, not a success the user never waited for.
            raise RequestCancelledError(
                f"{self.describe()} finished after the request was cancelled; "
                "its answer was discarded."
            )
        return ChatResponse(
            text=text,
            request_id=request.request_id,
            tokens_generated=0,
            finish_reason="stop",
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


