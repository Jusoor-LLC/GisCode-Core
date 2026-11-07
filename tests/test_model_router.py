"""The router must pick the selected backend, or say why it cannot."""

from __future__ import annotations

import pytest

from giscode.inference.errors import InferenceUnavailableError, RequestCancelledError
from giscode.inference.provider_backend import ProviderChatBackend
from giscode.inference.router import ModelRouter
from giscode.inference.runtime import ChatMessage, ChatRequest, ChatResponse
from giscode.providers.base import LLAMA_CPP, OLLAMA, OPENAI_COMPAT


class _Model:
    def __init__(self, model_id: str, capabilities=("completion",)) -> None:
        self.id = model_id
        self.capabilities = tuple(capabilities)


class _Provider:
    """A provider that answers locally, without a socket."""

    def __init__(self, models=("m1",), reply="pong", vision=False, kind=OPENAI_COMPAT):
        self.kind = kind
        self.label = "Fake"
        self.allow_remote = False
        self.reply = reply
        self.calls = []
        self.down = False
        caps = ("completion", "vision") if vision else ("completion",)
        self._models = [_Model(name, caps) for name in models]

    def list_models(self):
        if self.down:
            raise RuntimeError("connection refused")
        return list(self._models)

    def chat(self, model, messages, *, max_tokens=256, temperature=0.2, timeout=None):
        self.calls.append((model, messages))
        return self.reply

    def secret_free_endpoint(self):
        return "http://127.0.0.1:9999/v1"


class _Registry:
    def __init__(self, providers):
        self._providers = dict(providers)

    def get(self, provider_id):
        return self._providers.get(provider_id)

    def provider_for_model(self, provider_id):
        return self._providers.get(provider_id)


class _Profile:
    def __init__(self, profile_id, purpose, provider, model, label="Coding"):
        self.id = profile_id
        self.purpose = purpose
        self.provider = provider
        self.model = model
        self.label = label


class _Profiles:
    def __init__(self, active=None, last_good=None):
        self._active = active
        self._last = last_good

    def active_for(self, purpose):
        if self._active is not None and self._active.purpose == purpose:
            return self._active
        return None

    def last_good_for(self, purpose):
        return self._last

    def get(self, profile_id):
        if self._active is not None and self._active.id == profile_id:
            return self._active
        return None

    def set_active(self, profile_id):
        if self._active is None or self._active.id != profile_id:
            raise KeyError(profile_id)


class _Builtin:
    """Stands in for InferenceAPI: same four calls, no runtime."""

    def __init__(self, reply="builtin answer"):
        self.available = True
        self.reply = reply
        self.calls = []
        self.cancelled = False

    def get_health(self):
        return {"available": True, "model_loaded": True, "model_state": "gguf", "load_state": "ready"}

    def get_model_status(self):
        return type("Status", (), {"name": "local.gguf", "path": "/models/local.gguf", "state": None})()

    def chat_generate(self, request):
        self.calls.append(request)
        return ChatResponse(text=self.reply)

    def generate(self, request):
        return ChatResponse(text=self.reply)

    def cancel(self):
        self.cancelled = True


def _request(text="hi"):
    return ChatRequest(messages=[ChatMessage(role="user", content=text)], max_tokens=64, temperature=0.0)


def test_no_profile_uses_the_builtin_runtime():
    builtin = _Builtin()
    router = ModelRouter(builtin=builtin, settings={}, profiles=_Profiles(), registry=_Registry({}))

    assert router.active_target().kind == LLAMA_CPP
    assert router.chat_generate(_request()).text == "builtin answer"
    assert len(builtin.calls) == 1


def test_selected_profile_sends_the_request_to_its_provider():
    provider = _Provider(models=("m1",), reply="from ollama")
    profile = _Profile("p1", "coding", "ollama", "m1")
    router = ModelRouter(
        builtin=_Builtin(),
        settings={},
        profiles=_Profiles(active=profile),
        registry=_Registry({"ollama": provider}),
    )

    response = router.chat_generate(_request())

    assert response.text == "from ollama"
    assert provider.calls[0][0] == "m1"
    assert router.get_health()["model_loaded"] is True
    assert router.get_health()["target"]["kind"] == "ollama"


def test_switching_profile_changes_the_real_backend():
    first = _Provider(models=("m1",), reply="first")
    second = _Provider(models=("m2",), reply="second")
    router = ModelRouter(
        builtin=_Builtin(),
        settings={},
        profiles=_Profiles(active=_Profile("p1", "coding", "ollama", "m1")),
        registry=_Registry({"ollama": first, "lmstudio": second}),
    )
    assert router.chat_generate(_request()).text == "first"

    router._profiles._active = _Profile("p2", "coding", "lmstudio", "m2")

    assert router.chat_generate(_request()).text == "second"
    assert second.calls and second.calls[0][0] == "m2"


def test_a_model_the_provider_does_not_list_is_never_reported_active():
    provider = _Provider(models=("other",))
    router = ModelRouter(
        builtin=_Builtin(),
        settings={},
        profiles=_Profiles(active=_Profile("p1", "coding", "ollama", "m1")),
        registry=_Registry({"ollama": provider}),
    )

    assert router.get_model_status() is None
    assert router.can_chat() is False
    with pytest.raises(InferenceUnavailableError) as excinfo:
        router.chat_generate(_request())
    assert "m1" in str(excinfo.value)
    assert provider.calls == []


def test_a_failed_provider_does_not_silently_switch():
    down = _Provider(models=("m1",))
    down.down = True
    fallback = _Profile("p0", "coding", "lmstudio", "m0", label="Previous")
    router = ModelRouter(
        builtin=_Builtin(),
        settings={},
        profiles=_Profiles(active=_Profile("p1", "coding", "ollama", "m1"), last_good=fallback),
        registry=_Registry({"ollama": down}),
    )

    with pytest.raises(InferenceUnavailableError):
        router.chat_generate(_request())

    # The failure is reported, and the selection is unchanged.
    assert router.active_target().profile_id == "p1"
    assert router.fallback_target().profile_id == "p0"


def test_a_text_model_is_never_sent_an_image(tmp_path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    provider = _Provider(models=("m1",))
    backend = ProviderChatBackend(provider, "m1")

    request = ChatRequest(messages=[ChatMessage(role="user", content="what is this?")], images=[str(image)])

    with pytest.raises(InferenceUnavailableError) as excinfo:
        backend.chat_generate(request)
    assert "text model" in str(excinfo.value)
    assert provider.calls == []


def test_a_vision_model_receives_the_image_as_base64(tmp_path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    provider = _Provider(models=("vl",), vision=True, kind=OLLAMA)
    backend = ProviderChatBackend(provider, "vl")

    request = ChatRequest(messages=[ChatMessage(role="user", content="what is this?")], images=[str(image)])
    backend.chat_generate(request)

    sent = provider.calls[0][1][0]
    assert sent["role"] == "user"
    assert sent["images"] == ["iVBORw0KGgo="]


def test_cancel_discards_a_late_reply():
    provider = _Provider(models=("m1",))
    backend = ProviderChatBackend(provider, "m1")
    backend.cancel()

    with pytest.raises(RequestCancelledError):
        backend.chat_generate(_request())


def test_streaming_is_refused_rather_than_faked():
    backend = ProviderChatBackend(_Provider(models=("m1",)), "m1")

    with pytest.raises(InferenceUnavailableError) as excinfo:
        backend.chat_generate_stream(_request())
    assert "does not stream" in str(info := str(excinfo.value)) or "single response" in info
