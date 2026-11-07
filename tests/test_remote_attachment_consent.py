"""An attachment must not leave this machine without a separate, explicit yes.

The endpoint opt-in (``allow_remote``) lets a remote provider answer text. It is
not consent to upload a picture: the tests here pin that boundary, including the
case where the provider is remote *and* vision-capable, which is exactly when a
silent upload would be easiest to miss.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PIL")

from PIL import Image

from giscode.inference.errors import InferenceUnavailableError
from giscode.inference.provider_backend import ProviderChatBackend
from giscode.inference.router import ModelRouter, Target
from giscode.inference.runtime import ChatMessage, ChatRequest, ChatResponse
from giscode.providers.base import OLLAMA, OPENAI_COMPAT

REMOTE = "https://vision.example.com/v1"
LOCAL = "http://127.0.0.1:11434"
SECRET = "sk-should-never-appear"


def _png(path):
    Image.new("RGB", (8, 8), (10, 10, 10)).save(path)
    return str(path)


class _Model:
    def __init__(self, model_id, caps=("completion", "vision")):
        self.id = model_id
        self.capabilities = tuple(caps)


class _Provider:
    def __init__(self, endpoint, models=("vl",), kind=OPENAI_COMPAT):
        self.kind = kind
        self.label = "Remote"
        self.endpoint = endpoint
        self.allow_remote = True
        self.allow_vision = True
        self.calls = []
        self._models = [_Model(name) for name in models]

    def list_models(self):
        return list(self._models)

    def chat(self, model, messages, *, max_tokens=256, temperature=0.2, timeout=None):
        self.calls.append((model, messages))
        return "described"

    def secret_free_endpoint(self):
        return self.endpoint


class _Profile:
    def __init__(self, pid, purpose, provider, model):
        self.id = pid
        self.purpose = purpose
        self.provider = provider
        self.model = model
        self.label = "Vision"


class _Profiles:
    def __init__(self, profiles):
        self._profiles = list(profiles)

    def active_for(self, purpose):
        for profile in self._profiles:
            if profile.purpose == purpose:
                return profile
        return None

    def last_good_for(self, purpose):
        return None


class _Registry:
    def __init__(self, providers):
        self._providers = dict(providers)

    def get(self, pid):
        return self._providers.get(pid)

    def provider_for_model(self, pid):
        return self._providers.get(pid)

    def all_models(self):
        return []


def _request(image):
    return ChatRequest(
        messages=[ChatMessage(role="user", content="what is in this image?")],
        images=[image],
    )


def test_remote_image_is_refused_without_consent(tmp_path):
    image = _png(tmp_path / "shot.png")
    provider = _Provider(REMOTE)
    backend = ProviderChatBackend(provider, "vl", allow_remote=True, attachment_consent=False)

    with pytest.raises(InferenceUnavailableError) as excinfo:
        backend.chat_generate(_request(image))

    message = str(excinfo.value)
    assert "was not sent" in message
    assert "explicit consent" in message
    assert "Nothing left this computer" in message
    assert provider.calls == [], "the provider must not be contacted at all"


def test_remote_image_is_sent_only_after_consent(tmp_path):
    image = _png(tmp_path / "shot.png")
    provider = _Provider(REMOTE)
    backend = ProviderChatBackend(provider, "vl", allow_remote=True, attachment_consent=True)

    backend.chat_generate(_request(image))

    assert provider.calls, "an explicitly consented remote request must be sent"


def test_a_local_provider_needs_no_extra_consent(tmp_path):
    image = _png(tmp_path / "shot.png")
    provider = _Provider(LOCAL, kind=OLLAMA)
    backend = ProviderChatBackend(provider, "vl", attachment_consent=False)

    backend.chat_generate(_request(image))

    assert provider.calls


def test_text_to_a_remote_provider_is_not_gated(tmp_path):
    provider = _Provider(REMOTE)
    backend = ProviderChatBackend(provider, "vl", allow_remote=True, attachment_consent=False)

    backend.chat_generate(ChatRequest(messages=[ChatMessage(role="user", content="hello")]))

    assert provider.calls, "the consent rule is about attachments, not text"


def test_the_router_reads_consent_from_settings(tmp_path):
    image = _png(tmp_path / "shot.png")
    provider = _Provider(REMOTE)
    profiles = _Profiles([_Profile("v", "vision", "openai_compat", "vl")])

    blocked = ModelRouter(
        builtin=None,
        settings={},
        purpose="vision",
        profiles=profiles,
        registry=_Registry({"openai_compat": provider}),
    )
    with pytest.raises(InferenceUnavailableError):
        blocked.chat_generate(_request(image))
    assert provider.calls == []

    allowed = ModelRouter(
        builtin=None,
        settings={"providers": {"remote_attachments": True}},
        purpose="vision",
        profiles=profiles,
        registry=_Registry({"openai_compat": provider}),
    )
    allowed.chat_generate(_request(image))
    assert provider.calls, "consent from settings must reach the backend"


def test_switching_provider_cannot_inherit_consent(tmp_path):
    """A local profile works; switching to a remote one asks again."""
    image = _png(tmp_path / "shot.png")
    local = _Provider(LOCAL, kind=OLLAMA)
    remote = _Provider(REMOTE)
    profiles = _Profiles([_Profile("v", "vision", "ollama", "vl")])
    router = ModelRouter(
        builtin=None,
        settings={},
        purpose="vision",
        profiles=profiles,
        registry=_Registry({"ollama": local, "openai_compat": remote}),
    )

    router.chat_generate(_request(image))
    assert local.calls

    profiles._profiles = [_Profile("v2", "vision", "openai_compat", "vl")]

    with pytest.raises(InferenceUnavailableError):
        router.chat_generate(_request(image))
    assert remote.calls == []


def test_no_key_or_full_path_reaches_the_prompt(tmp_path):
    image = _png(tmp_path / "shot.png")
    provider = _Provider(REMOTE)
    provider._api_key = SECRET  # what Provider.__init__ would store
    backend = ProviderChatBackend(provider, "vl", attachment_consent=True)

    backend.chat_generate(_request(image))

    _, messages = provider.calls[0]
    flattened = repr(messages)
    assert SECRET not in flattened
    assert str(tmp_path) not in flattened, "the full path must never be sent"
    # Pixels travel only after consent; the file name lives in the Chat prompt,
    # not in the provider payload, so the path has nothing to leak here.
    assert "base64" in flattened


def test_the_refusal_does_not_leak_the_key(tmp_path):
    image = _png(tmp_path / "shot.png")
    provider = _Provider(REMOTE)
    backend = ProviderChatBackend(provider, "vl", attachment_consent=False)

    with pytest.raises(InferenceUnavailableError) as excinfo:
        backend.chat_generate(_request(image))

    assert SECRET not in str(excinfo.value)
    assert "https://vision.example.com" in str(excinfo.value)


def test_health_still_reports_a_remote_target_without_sending(tmp_path):
    provider = _Provider(REMOTE)
    backend = ProviderChatBackend(provider, "vl")

    health = backend.get_health()

    assert health["available"] is True
    assert health["endpoint"] == REMOTE
    assert provider.calls == []
