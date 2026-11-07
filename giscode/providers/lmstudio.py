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


class LMStudioProvider(Provider):
    kind = LMSTUDIO
    label = "LM Studio"
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
