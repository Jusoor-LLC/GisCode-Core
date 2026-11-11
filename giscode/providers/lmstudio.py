"""LM Studio provider.

LM Studio can serve an OpenAI-compatible API from its own process. When it does,
GisCode can talk to it directly - which is different from the *other* LM Studio
integration GisCode already has (reading its downloaded GGUF files and reusing
its llama.cpp backend).

Both can be true at once and they are not interchangeable:

* the file-based path loads a model inside GisCode's own ``llama-server``;
* this provider asks LM Studio's server to run the model it already has.

Endpoints used (OpenAI-compatible, LM Studio default port 1234):

* ``GET  /v1/models``
* ``POST /v1/chat/completions``
"""
from __future__ import annotations

from typing import Optional

from giscode.providers.base import (
    LMSTUDIO,
    Provider,
    ProviderError,
    ProviderModel,
    ProviderStatus,
    join_url,
)
from giscode.providers.http import request_json
from giscode.providers.streaming import (
    SSE_DONE_EVENT,
    StreamDelta,
    StreamError,
    iter_sse_objects,
    stream_response,
)


class LMStudioProvider(Provider):
    kind = LMSTUDIO
    label = "LM Studio"
    supports_streaming = True
    stream_format = "sse"
    supports_download = False
    supports_pause_resume = False
    supports_delete = False
    download_note = (
        "LM Studio manages its own model downloads. Download the model in LM "
        "Studio, then refresh this list."
    )

    def list_models(self) -> list[ProviderModel]:
        body = request_json(
            join_url(self.endpoint, "v1/models"),
            timeout=self.timeout,
            headers=self.headers(),
        )
        entries = (body or {}).get("data") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            return []
        models: list[ProviderModel] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identifier = str(entry.get("id") or "").strip()
            if not identifier:
                continue
            models.append(
                ProviderModel(
                    id=identifier,
                    provider=self.kind,
                    name=identifier,
                    capabilities=("text", "chat"),
                    installed=True,
                    description="Served by LM Studio",
                )
            )
        return sorted(models, key=lambda m: m.id.lower())

    def status(self) -> ProviderStatus:
        try:
            models = self.list_models()
        except ProviderError as exc:
            return ProviderStatus(
                kind=self.kind,
                label=self.label,
                endpoint=self.secret_free_endpoint(),
                available=False,
                detail=str(exc),
            )
        return ProviderStatus(
            kind=self.kind,
            label=self.label,
            endpoint=self.secret_free_endpoint(),
            available=True,
            detail=(
                "LM Studio's local server is running."
                if models
                else "LM Studio's server is running but serves no models."
            ),
            model_count=len(models),
        )

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
                "No model was selected for LM Studio.",
                action="Load a model in LM Studio and choose it here.",
            )
        body = request_json(
            join_url(self.endpoint, "v1/chat/completions"),
            method="POST",
            payload={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            },
            timeout=timeout or max(self.timeout, 60.0),
            headers=self.headers(),
        )
        return _first_message_content(body, provider="LM Studio")

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
        if not model:
            raise ProviderError(
                "No model was selected for LM Studio.",
                action="Load a model in LM Studio and choose it here.",
            )
        yield from _stream_openai_chat(
            self,
            model,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            idle_timeout=idle_timeout,
            cancel_event=cancel_event,
            should_stop=should_stop,
        )


def _stream_openai_chat(
    provider: Provider,
    model: str,
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float,
    timeout: Optional[float],
    idle_timeout: Optional[float],
    cancel_event,
    should_stop,
    include_usage: bool = False,
    token_field: str = "max_tokens",
):
    """Decode an OpenAI-shaped SSE chat stream into :class:`StreamDelta` events.

    Shared by LM Studio and generic OpenAI-compatible servers because they use
    the same protocol: ``data: {...}`` lines, content under
    ``choices[0].delta.content``, an optional ``finish_reason``, and a final
    ``data: [DONE]``. A server that closes without either signal is reported as
    an incomplete stream rather than a finished answer.
    """
    payload = {
        "model": model,
        "messages": messages,
        token_field: max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if include_usage:
        # llama.cpp and OpenAI honour this; the usage block then arrives in the
        # final chunk. Servers that do not know it still stream content fine.
        payload["stream_options"] = {"include_usage": True}
    finish = "stop"
    tokens = 0
    usage: dict = {}
    saw_done = False
    saw_finish = False
    with stream_response(
        join_url(provider.endpoint, "v1/chat/completions"),
        payload=payload,
        headers=provider.headers(),
        timeout=timeout or max(provider.timeout, 60.0),
        idle_timeout=idle_timeout,
        cancel_event=cancel_event,
        should_stop=should_stop,
    ) as chunks:
        for obj in iter_sse_objects(chunks):
            if obj is SSE_DONE_EVENT:
                saw_done = True
                break
            if not isinstance(obj, dict):
                continue
            error = obj.get("error")
            if error:
                message = error.get("message") if isinstance(error, dict) else error
                raise ProviderError(
                    f"{provider.label or provider.kind} reported an error while "
                    f"streaming: {message}",
                    action="Check the provider's log for the reason.",
                )
            choices = obj.get("choices") or []
            if choices and isinstance(choices[0], dict):
                choice = choices[0]
                delta = choice.get("delta") or {}
                content = delta.get("content") if isinstance(delta, dict) else None
                if isinstance(content, str) and content:
                    yield StreamDelta(text=content)
                elif isinstance(choice.get("text"), str) and choice.get("text"):
                    yield StreamDelta(text=choice["text"])
                reason = choice.get("finish_reason")
                if reason:
                    saw_finish = True
                    finish = str(reason)
            block = obj.get("usage") or {}
            if isinstance(block, dict) and block:
                usage = dict(block)
                number = block.get("completion_tokens")
                if isinstance(number, int):
                    tokens = number
    if not saw_done and not saw_finish:
        raise StreamError(
            f"{provider.label or provider.kind} closed the stream before the answer "
            "was complete.",
            action="Try again; if it repeats, check the provider's log.",
        )
    yield StreamDelta(done=True, finish_reason=finish, tokens=tokens, usage=usage)


def _first_message_content(body, *, provider: str) -> str:
    """Extract ``choices[0].message.content`` from an OpenAI-shaped response."""
    if isinstance(body, dict):
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message") or {}
                content = message.get("content")
                if isinstance(content, str):
                    return content
    raise ProviderError(
        f"{provider} answered without any message content.",
        action="Check that the selected model is loaded, then test again.",
    )
