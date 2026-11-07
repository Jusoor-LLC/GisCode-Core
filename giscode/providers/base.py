"""Provider abstraction for local model backends.

GisCode has always run models through its own ``llama-server``. That stays the
default, and nothing here replaces it: this package adds *other* local runtimes
as selectable providers so a model that already lives in Ollama or LM Studio can
be used without copying GGUF files around.

Three rules shape the design:

* **A provider is only "available" when it answers.** ``status()`` performs a
  real request. A configured endpoint is not evidence that anything is running.
* **Loopback by default.** Endpoints must be ``http(s)`` and, unless the user
  explicitly opts in, must resolve to a loopback host. Local-only operation is
  the default, not a promise in a document.
* **Capabilities are per provider, not assumed.** Ollama and an OpenAI-compatible
  endpoint do not expose the same operations, so download/pause/delete are
  declared flags rather than methods every provider pretends to implement.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

#: Stable provider kinds. ``custom`` covers any OpenAI-compatible local server
#: the user adds by URL; the kind is stored with the model so routing never has
#: to guess which server a model came from.
OLLAMA = "ollama"
LMSTUDIO = "lmstudio"
OPENAI_COMPAT = "openai_compat"

PROVIDER_KINDS = (OLLAMA, LMSTUDIO, OPENAI_COMPAT)

#: The built-in runtime: GisCode's own llama-server process. It is not a network
#: provider, so it is absent from ``PROVIDER_KINDS``; it appears in profiles so a
#: profile can keep pointing at a local GGUF file.
LLAMA_CPP = "llama.cpp"

DEFAULT_ENDPOINTS = {
    OLLAMA: "http://127.0.0.1:11434",
    LMSTUDIO: "http://127.0.0.1:1234",
}

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


class ProviderError(RuntimeError):
    """A provider did not answer, or answered unusably.

    ``action`` is the next thing a person should try. Keeping it separate from
    the message lets the GUI show a short sentence and a concrete step instead of
    a stack trace.
    """

    def __init__(self, message: str, *, action: str = "") -> None:
        super().__init__(message)
        self.action = action


@dataclass(frozen=True)
class ProviderStatus:
    """The result of actually asking a provider whether it is there."""

    kind: str
    label: str
    endpoint: str
    available: bool
    detail: str = ""
    version: str = ""
    model_count: int = 0

    def describe(self) -> str:
        if self.available:
            extra = f", {self.model_count} model(s)" if self.model_count else ""
            version = f" v{self.version}" if self.version else ""
            return f"{self.label}{version} at {self.endpoint} - reachable{extra}"
        return f"{self.label} at {self.endpoint} - unavailable: {self.detail}"


@dataclass(frozen=True)
class ProviderModel:
    """One model as a provider reports it.

    Deliberately does not claim a quantization or a context window it was not
    told: empty means "the provider did not say", which the UI shows as unknown
    rather than filling in a plausible number.
    """

    id: str
    provider: str
    name: str = ""
    size_bytes: int = 0
    quantization: str = ""
    context_length: int = 0
    families: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    installed: bool = True
    description: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.id

    def supports(self, capability: str) -> bool:
        return capability.strip().lower() in {c.lower() for c in self.capabilities}

    @property
    def is_coding(self) -> bool:
        return self.supports("coding")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "name": self.display_name,
            "size_bytes": self.size_bytes,
            "quantization": self.quantization,
            "context_length": self.context_length,
            "families": list(self.families),
            "capabilities": list(self.capabilities),
            "installed": self.installed,
            "description": self.description,
        }


def normalize_endpoint(value: str, *, default: str = "") -> str:
    """Return a usable ``scheme://host:port`` base URL, or raise ValueError.

    Accepts a bare ``host:port`` (the form people paste from a provider's UI) and
    strips a trailing ``/v1`` so callers can append their own path exactly once.
    """
    raw = (value or "").strip() or default
    if not raw:
        raise ValueError("An endpoint is required.")
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// endpoints are supported.")
    if not parsed.hostname:
        raise ValueError("The endpoint needs a host, for example http://127.0.0.1:11434")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid port in endpoint {parsed.netloc!r}.") from exc
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    netloc = parsed.netloc
    if port is None and parsed.scheme == "http":
        netloc = parsed.netloc
    return f"{parsed.scheme}://{netloc}{path}"


def is_loopback(endpoint: str) -> bool:
    """True when the endpoint host is this machine."""
    try:
        host = (urlparse(endpoint).hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_endpoint(endpoint: str, *, allow_remote: bool = False) -> str:
    """Validate an endpoint, enforcing loopback unless the user opted out."""
    normalized = normalize_endpoint(endpoint)
    if not allow_remote and not is_loopback(normalized):
        raise ValueError(
            "Only loopback endpoints are used by default. Enable 'allow remote' "
            "for this provider if the server really is on another machine."
        )
    return normalized


def join_url(endpoint: str, path: str) -> str:
    return f"{endpoint.rstrip('/')}/{path.lstrip('/')}"


class Provider:
    """Minimal interface every local provider implements.

    Subclasses override the HTTP calls; this base class owns the endpoint rules
    and declares which optional operations are genuinely supported, so callers can
    check flags instead of try/except-ing a NotImplementedError.
    """

    kind: str = ""
    label: str = ""
    #: True only for providers that expose a real "download this model" API.
    supports_download: bool = False
    #: True only when the provider's own download can be paused and resumed.
    supports_pause_resume: bool = False
    #: True only when the provider's API can delete a model it manages.
    supports_delete: bool = False
    #: Why downloads are unavailable, shown verbatim in the UI.
    download_note: str = ""

    def __init__(
        self,
        endpoint: str = "",
        *,
        timeout: float = 5.0,
        api_key: str = "",
        allow_remote: bool = False,
        label: str = "",
    ) -> None:
        self.endpoint = validate_endpoint(
            endpoint or DEFAULT_ENDPOINTS.get(self.kind, ""),
            allow_remote=allow_remote,
        )
        self.timeout = timeout
        self._api_key = api_key
        self.allow_remote = allow_remote
        if label:
            self.label = label

    # -- to be implemented by subclasses -----------------------------------
    def status(self) -> ProviderStatus:  # pragma: no cover - interface
        raise NotImplementedError

    def list_models(self) -> list[ProviderModel]:  # pragma: no cover - interface
        raise NotImplementedError

    def chat(
        self,
        model: str,
        messages: list[dict],
        *,
        max_tokens: int = 256,
        temperature: float = 0.2,
        timeout: Optional[float] = None,
    ) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    # -- shared helpers ----------------------------------------------------
    def headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def secret_free_endpoint(self) -> str:
        """Endpoint safe to log: never carries userinfo."""
        parsed = urlparse(self.endpoint)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path}"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.secret_free_endpoint()}>"
