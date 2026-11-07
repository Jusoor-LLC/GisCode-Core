"""One inference surface in front of every backend GisCode can use.

Chat and Projects must not each learn how to reach Ollama, LM Studio, an
OpenAI-compatible endpoint and the built-in ``llama.cpp`` runtime. They ask
:class:`ModelRouter`, and it resolves **the active profile** into exactly one
target:

* a ``llama.cpp`` profile (or the legacy ``model.selected_model`` setting) is
  served by the built-in :class:`~giscode.inference.api.InferenceAPI`, whose
  model GisCode loads itself;
* any other provider is served by
  :class:`~giscode.inference.provider_backend.ProviderChatBackend`, because that
  provider owns its own model lifecycle.

Because the router exposes the same four methods the planner and coding loop
already call, a profile switch changes the real backend without reloading the
app and without touching the approval gate, executor or verifier.

Two rules this class enforces:

* **No silent failover.** If the selected provider or model cannot answer, the
  error names it and offers the last selection that worked. GisCode does not
  slide to a different provider or to a cloud model on its own.
* **No false claims.** ``get_model_status`` returns ``None`` unless the provider
  itself lists the model, so nothing downstream can report a model as active
  that is not there.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

from giscode.inference.provider_backend import DEFAULT_CHAT_TIMEOUT, ProviderChatBackend
from giscode.providers.base import LLAMA_CPP, ProviderError
from giscode.providers.profiles import PURPOSES, ProfileStore
from giscode.providers.registry import ProviderRegistry
from giscode.providers.settings_adapter import as_settings_source


@dataclass(frozen=True)
class Target:
    """Where a request will actually go."""

    kind: str
    model: str = ""
    provider_id: str = ""
    profile_id: str = ""
    profile_label: str = ""
    purpose: str = "coding"

    @property
    def is_provider(self) -> bool:
        return self.kind != LLAMA_CPP

    @property
    def is_builtin(self) -> bool:
        return self.kind == LLAMA_CPP

    def describe(self) -> str:
        if self.is_builtin:
            if not self.model:
                return "the built-in llama.cpp runtime (no model loaded)"
            return f"built-in llama.cpp · {self.model.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}"
        return f"{self.kind} · {self.model}"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "model": self.model,
            "provider_id": self.provider_id,
            "profile_id": self.profile_id,
            "profile_label": self.profile_label,
            "purpose": self.purpose,
        }


class ModelRouter:
    """Resolve the selected profile, then delegate to the backend it names."""

    def __init__(
        self,
        builtin: Any = None,
        settings: Any = None,
        *,
        purpose: str = "coding",
        registry: Optional[ProviderRegistry] = None,
        profiles: Optional[ProfileStore] = None,
        timeout: float = DEFAULT_CHAT_TIMEOUT,
    ) -> None:
        self._builtin = builtin
        source = as_settings_source(settings) if settings is not None else {}
        self._source = source
        self._settings = settings
        self.purpose = purpose if purpose in PURPOSES else "coding"
        self._registry = registry or ProviderRegistry(source)
        self._profiles = profiles or ProfileStore(source)
        self._timeout = max(5.0, float(timeout))
        self._backends: dict[tuple[str, str], ProviderChatBackend] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- selection
    @property
    def profiles(self) -> ProfileStore:
        return self._profiles

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    @property
    def builtin(self) -> Any:
        return self._builtin

    def _setting(self, key: str, default=None):
        getter = getattr(self._source, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except Exception:
                return default
        return default

    def _attachment_consent(self) -> bool:
        """Opt-in for sending attachments to a remote endpoint. Default: no."""
        return bool(self._setting("providers.remote_attachments", False))

    def _legacy_model(self) -> str:
        value = ""
        getter = getattr(self._source, "get", None)
        if callable(getter):
            try:
                value = getter("model.selected_model", "") or ""
            except Exception:
                value = ""
        return str(value or "")

    def active_target(self, purpose: Optional[str] = None) -> Target:
        """The profile that will serve *purpose*, or the built-in fallback."""
        use = purpose or self.purpose
        if use not in PURPOSES:
            use = self.purpose
        profile = None
        try:
            profile = self._profiles.active_for(use)
        except Exception:
            profile = None
        if profile is not None:
            return Target(
                kind=profile.provider or LLAMA_CPP,
                model=profile.model or "",
                provider_id=profile.provider or LLAMA_CPP,
                profile_id=profile.id,
                profile_label=profile.label,
                purpose=use,
            )
        return Target(kind=LLAMA_CPP, model=self._legacy_model(), purpose=use)

    def fallback_target(self, purpose: Optional[str] = None) -> Optional[Target]:
        """The last selection that worked, for an error message - never auto-applied."""
        use = purpose or self.purpose
        try:
            profile = self._profiles.last_good_for(use)
        except Exception:
            return None
        if profile is None:
            return None
        return Target(
            kind=profile.provider or LLAMA_CPP,
            model=profile.model or "",
            provider_id=profile.provider or LLAMA_CPP,
            profile_id=profile.id,
            profile_label=profile.label,
            purpose=use,
        )

    def describe_active(self, purpose: Optional[str] = None) -> str:
        target = self.active_target(purpose)
        if target.profile_label:
            return f"{target.describe()} ({target.profile_label} profile)"
        return target.describe()

    def switch_to(self, profile_id: str) -> Target:
        """Make *profile_id* active and drop the cached backend for its purpose."""
        self._profiles.set_active(profile_id)
        profile = self._profiles.get(profile_id)
        if profile is not None:
            self.purpose = profile.purpose
        with self._lock:
            self._backends.clear()
        return self.active_target()

    # ------------------------------------------------------------- delegation
    def _provider_for(self, target: Target):
        provider = None
        try:
            provider = self._registry.provider_for_model(target.provider_id)
        except ProviderError:
            provider = None
        except Exception:
            provider = None
        if provider is None:
            provider = self._registry.get(target.provider_id) if target.provider_id else None
        return provider

    def _backend_for(self, target: Target) -> Any:
        if target.is_builtin:
            return self._builtin
        provider = self._provider_for(target)
        if provider is None:
            raise ProviderError(
                f"the {target.provider_id} provider is not configured",
                action="Open Model Manager → Providers and add or enable it.",
            )
        consent = self._attachment_consent()
        # Consent is part of the key: toggling it must not reuse a backend built
        # under the opposite decision.
        key = (target.provider_id, target.model, consent)
        with self._lock:
            backend = self._backends.get(key)
            if backend is None:
                backend = ProviderChatBackend(
                    provider,
                    target.model,
                    purpose=target.purpose,
                    timeout=self._timeout,
                    allow_remote=bool(getattr(provider, "allow_remote", False)),
                    attachment_consent=consent,
                )
                self._backends[key] = backend
        return backend

    def backend(self, purpose: Optional[str] = None) -> Any:
        """The live backend for *purpose* - built-in or provider."""
        return self._backend_for(self.active_target(purpose))

    def can_chat(self, purpose: Optional[str] = None) -> bool:
        """True only when a real backend exists and (for a provider) lists the model."""
        target = self.active_target(purpose)
        if target.is_builtin:
            probe = getattr(self._builtin, "available", None)
            return bool(probe) if not callable(probe) else bool(probe())
        try:
            return bool(self._backend_for(target).available)
        except Exception:
            return False

    # ------------------------------------------------------------- InferenceAPI surface
    @property
    def available(self) -> bool:
        return self.can_chat()

    def get_health(self) -> dict:
        target = self.active_target()
        if target.is_builtin:
            if self._builtin is None:
                return {
                    "available": False,
                    "model_loaded": False,
                    "model_state": None,
                    "load_state": "unavailable",
                }
            health = dict(self._builtin.get_health())
            health["target"] = target.to_dict()
            return health
        try:
            backend = self._backend_for(target)
        except ProviderError as exc:
            return {
                "available": False,
                "model_loaded": False,
                "model_state": None,
                "load_state": "unavailable",
                "load_error": str(exc),
                "target": target.to_dict(),
            }
        health = dict(backend.get_health())
        health["target"] = target.to_dict()
        return health

    def get_model_status(self):
        target = self.active_target()
        if target.is_builtin:
            getter = getattr(self._builtin, "get_model_status", None)
            return getter() if callable(getter) else None
        try:
            return self._backend_for(target).get_model_status()
        except Exception:
            return None

    def chat_generate(self, request):
        return self._backend_for(self.active_target()).chat_generate(request)

    def chat_generate_stream(self, request, cancel_event=None, read_timeout: float = 5.0):
        return self._backend_for(self.active_target()).chat_generate_stream(
            request, cancel_event=cancel_event, read_timeout=read_timeout
        )

    def generate(self, request):
        return self._backend_for(self.active_target()).generate(request)

    def load_model(self, model_path: str = "", **_kwargs):
        return self._backend_for(self.active_target()).load_model(model_path, **_kwargs)

    def unload_model(self) -> None:
        target = self.active_target()
        if target.is_builtin:
            if self._builtin is not None:
                self._builtin.unload_model()
            return
        # The provider owns the model; GisCode does not unload someone else's.
        return None

    def cancel(self) -> None:
        target = self.active_target()
        try:
            backend = self._backend_for(target)
        except Exception:
            backend = self._builtin
        cancel = getattr(backend, "cancel", None)
        if callable(cancel):
            cancel()

    # ------------------------------------------------------------- vision
    def supports_vision(self, target: Optional[Target] = None) -> bool:
        """True only when the provider itself reports this model reads images.

        A built-in GGUF target answers False here: its images go through
        :mod:`giscode.vision`, not through this router, and claiming otherwise
        would send a picture to a text model.
        """
        target = target or self.active_target()
        if target is None or not target.model or target.is_builtin:
            return False
        try:
            return bool(getattr(self._backend_for(target), "supports_vision", False))
        except Exception:
            return False

    def vision_target(self) -> tuple[Optional[Target], str]:
        """The target that should answer an image request, and why it was chosen.

        An explicit ``vision`` profile wins. Otherwise a ``chat`` profile whose
        model the provider reports as vision-capable is used and labelled as
        such. A text-only selection returns ``None`` - never a silent swap.
        """
        try:
            profile = self._profiles.active_for("vision")
        except Exception:
            profile = None
        if profile is not None:
            target = Target(
                kind=profile.provider or LLAMA_CPP,
                model=profile.model or "",
                provider_id=profile.provider or LLAMA_CPP,
                profile_id=profile.id,
                profile_label=profile.label,
                purpose="vision",
            )
            if target.is_builtin or self.supports_vision(target):
                return target, "vision profile"
            return None, (
                f"the vision profile {target.profile_label or target.profile_id!r} "
                f"names {target.model!r}, which "
                f"{target.provider_id or 'the provider'} does not report as "
                "vision-capable."
            )
        chat = self.active_target("chat")
        if self.supports_vision(chat):
            return chat, "chat profile"
        return None, (
            "no vision-capable model is selected: "
            f"{self.describe_active('chat') or 'no model'} is text-only."
        )

    def vision_candidates(self, limit: int = 5) -> list[str]:
        """Installed models the providers report as vision-capable, for a hint."""
        found: list[str] = []
        try:
            models = self._registry.all_models()
        except Exception:
            return found
        for model in models:
            if "vision" in {str(c).lower() for c in getattr(model, "capabilities", ())}:
                label = f"{getattr(model, 'provider', '')} · {model.id}".strip(" ·")
                if label not in found:
                    found.append(label)
            if len(found) >= limit:
                break
        return found

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ModelRouter {self.describe_active()}>"
