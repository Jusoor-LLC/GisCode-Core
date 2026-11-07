"""Any local OpenAI-compatible server (llama.cpp, vLLM, text-generation-webui...).

This is the escape hatch for runtimes GisCode does not name. It speaks only the
two endpoints every such server implements - ``/v1/models`` and
``/v1/chat/completions`` - and nothing else, so it never assumes a vendor
extension exists.

An API key is read by the caller from an environment variable and passed in; it
is never stored in settings and never logged.
"""
from __future__ import annotations

from typing import Optional

from giscode.providers.base import (
    OPENAI_COMPAT,
    Provider,
    ProviderError,
    ProviderModel,
    ProviderStatus,
    join_url,
)
from giscode.providers.http import request_json
from giscode.providers.lmstudio import _first_message_content


class OpenAICompatibleProvider(Provider):
    kind = OPENAI_COMPAT
    supports_download = False
    supports_pause_resume = False
    supports_delete = False
    download_note = (
        "This endpoint does not expose a download API. Put the model where the "
        "server expects it, then refresh this list."
    )

    def __init__(self, endpoint: str, *, label: str = "", **kwargs) -> None:
        kwargs.setdefault("label", label or "OpenAI-compatible")
        super().__init__(endpoint, **kwargs)

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
                    description=f"Served by {self.label}",
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
            detail="The endpoint is answering.",
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
                "No model was selected for this endpoint.",
                action="Pick one of the endpoint's models first.",
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
        return _first_message_content(body, provider=self.label)
