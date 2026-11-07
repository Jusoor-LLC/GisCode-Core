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
            capabilities = ["text", "chat"]
            for family in families:
                mapped = _FAMILY_CAPABILITIES.get(str(family).strip().lower())
                if mapped and mapped not in capabilities:
                    capabilities.append(mapped)
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
