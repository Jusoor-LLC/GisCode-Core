"""Ollama provider.

Ollama owns its own model store and its own downloader, so GisCode lists and
uses what Ollama has already pulled; it does not copy Ollama's blobs into
``~/.giscode/models`` and it does not pretend it can pause an Ollama pull.

Endpoints used (Ollama's documented local API):

* ``GET  /api/version`` - liveness and version
* ``GET  /api/tags``    - installed models
* ``POST /api/chat``    - chat completion (``stream: false``)

Ollama's default port is 11434 and it answers on loopback.
"""
from __future__ import annotations

from typing import Optional

from giscode.providers.base import (
    OLLAMA,
    Provider,
    ProviderError,
    ProviderModel,
    ProviderStatus,
    join_url,
)
from giscode.providers.http import request_json
from giscode.providers.streaming import (
    StreamDelta,
    StreamError,
    iter_ndjson_objects,
    stream_response,
)

#: Families Ollama reports that imply a capability GisCode can use. Anything not
#: listed is left unclassified rather than guessed.
_FAMILY_CAPABILITIES = {
    "clip": "vision",
    "mllama": "vision",
    "qwen2vl": "vision",
}


class OllamaProvider(Provider):
    kind = OLLAMA
    label = "Ollama"
    supports_streaming = True
    stream_format = "ndjson"
    supports_download = False
    supports_pause_resume = False
    supports_delete = False
    download_note = (
        "Ollama manages its own downloads. Run 'ollama pull <model>' in a "
        "terminal, then refresh this list."
    )

    def status(self) -> ProviderStatus:
        try:
            body = request_json(
                join_url(self.endpoint, "api/version"),
                timeout=self.timeout,
                headers=self.headers(),
            )
        except ProviderError as exc:
            return ProviderStatus(
                kind=self.kind,
                label=self.label,
                endpoint=self.secret_free_endpoint(),
                available=False,
                detail=str(exc),
            )
        version = ""
        if isinstance(body, dict):
            version = str(body.get("version") or "")
        count = 0
        try:
            count = len(self.list_models())
        except ProviderError:
            count = 0
        return ProviderStatus(
            kind=self.kind,
            label=self.label,
            endpoint=self.secret_free_endpoint(),
            available=True,
            detail="Ollama is running.",
            version=version,
            model_count=count,
        )

    def list_models(self) -> list[ProviderModel]:
        body = request_json(
            join_url(self.endpoint, "api/tags"),
            timeout=self.timeout,
            headers=self.headers(),
        )
        entries = (body or {}).get("models") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            return []
        models: list[ProviderModel] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("model") or "").strip()
            if not name:
                continue
            details = entry.get("details") or {}
            families = details.get("families") or []
            if isinstance(families, str):
                families = [families]
            # Ollama reports the real capability list (``completion``, ``vision``,
            # ``tools``, ``thinking``...). Trust it: guessing from the family name
            # made a model that can read images look text-only, so GisCode refused
            # every image request it could actually have answered.
            reported = entry.get("capabilities")
            capabilities: list[str] = []
            if isinstance(reported, list):
                capabilities = [str(item).strip().lower() for item in reported if str(item).strip()]
            if not capabilities:
                # Older Ollama builds omit the field; fall back to the family map.
                capabilities = ["text", "chat"]
                for family in families:
                    mapped = _FAMILY_CAPABILITIES.get(str(family).strip().lower())
                    if mapped and mapped not in capabilities:
                        capabilities.append(mapped)
            elif "completion" in capabilities and "chat" not in capabilities:
                capabilities.append("chat")
            models.append(
                ProviderModel(
                    id=name,
                    provider=self.kind,
                    name=name,
                    size_bytes=int(entry.get("size") or 0),
                    quantization=str(details.get("quantization_level") or ""),
                    families=tuple(str(f) for f in families),
                    capabilities=tuple(capabilities),
                    installed=True,
                    description=str(details.get("parameter_size") or ""),
                )
            )
        return sorted(models, key=lambda m: m.id.lower())

    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        max_tokens: int = 256,
        temperature: float = 0.2,
        timeout: Optional[float] = None,
    ) -> str:
        if not model:
            raise ProviderError(
                "No model was selected for this provider.",
                action="Choose an installed model, then test again.",
            )
        body = request_json(
            join_url(self.endpoint, "api/chat"),
            method="POST",
            payload={
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            },
            # Generating takes longer than a health check; the caller decides.
            timeout=timeout or max(self.timeout, 60.0),
            headers=self.headers(),
        )
        if isinstance(body, dict):
            message = body.get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content
        raise ProviderError(
            "Ollama answered without any message content.",
            action="Check 'ollama list' and the model name, then test again.",
        )

    def stream_chat(
        self,
        model: str,
        messages: list[dict],
        *,
        max_tokens: int = 256,
        temperature: float = 0.2,
        timeout: Optional[float] = None,
        idle_timeout: Optional[float] = None,
        cancel_event=None,
        should_stop=None,
    ):
        """Stream ``POST /api/chat`` as newline-delimited JSON objects.

        Ollama sends one JSON object per line: ``message.content`` deltas while
        generating, then a final object with ``done: true``, ``done_reason``
        and ``eval_count``. An object carrying ``error`` is a provider failure,
        not the end of a successful answer.
        """
        if not model:
            raise ProviderError(
                "No model was selected for this provider.",
                action="Choose an installed model, then test again.",
            )
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        completed = False
        with stream_response(
            join_url(self.endpoint, "api/chat"),
            payload=payload,
            headers=self.headers(),
            timeout=timeout or max(self.timeout, 60.0),
            idle_timeout=idle_timeout,
            cancel_event=cancel_event,
            should_stop=should_stop,
        ) as chunks:
            for obj in iter_ndjson_objects(chunks):
                error = obj.get("error")
                if error:
                    raise ProviderError(
                        f"Ollama reported an error while streaming: {error}",
                        action="Check 'ollama serve' and its log for the reason.",
                    )
                message = obj.get("message") or {}
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str) and content:
                    yield StreamDelta(text=content)
                if obj.get("done"):
                    completed = True
                    usage = {}
                    prompt_tokens = obj.get("prompt_eval_count")
                    completion_tokens = obj.get("eval_count")
                    if isinstance(prompt_tokens, int):
                        usage["prompt_tokens"] = prompt_tokens
                    if isinstance(completion_tokens, int):
                        usage["completion_tokens"] = completion_tokens
                    yield StreamDelta(
                        done=True,
                        finish_reason=str(obj.get("done_reason") or "stop"),
                        tokens=int(completion_tokens or 0),
                        usage=usage,
                    )
        if not completed:
            raise StreamError(
                "Ollama closed the stream before reporting the answer was done.",
                action="Try again; if it repeats, check that 'ollama serve' is healthy.",
            )
