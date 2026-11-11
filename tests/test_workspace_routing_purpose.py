"""The workspace must resolve the coding profile, not the chat profile.

A coding-only installation (one profile, purpose ``coding``) used to be ignored
by the project workspace because the dispatcher asked the router for ``chat``.
The router then fell back to the built-in llama.cpp runtime with the legacy
``model.selected_model`` name, which the packaged build had never loaded, so
every workspace request failed with
``'NoneType' object has no attribute 'chat_generate'`` while the configured
Ollama provider sat idle.

These tests pin the rule at the level where the defect lived, without needing a
Tk window.
"""
from __future__ import annotations

import inspect

from giscode.agentic.core_tasks import execute_request
from giscode.inference.router import purpose_for_surface


def test_a_project_request_asks_for_the_coding_profile():
    assert purpose_for_surface(has_image=False) == "coding"


def test_an_image_request_still_asks_for_the_vision_profile():
    assert purpose_for_surface(has_image=True) == "vision"


def test_a_plain_chat_surface_can_still_ask_for_chat():
    assert purpose_for_surface(has_image=False, coding_surface=False) == "chat"


def test_the_coding_dispatcher_defaults_to_the_coding_purpose():
    """A caller that forgets the purpose must not silently mean "chat"."""
    default = inspect.signature(execute_request).parameters["model_purpose"].default
    assert default == "coding"


def test_a_coding_profile_is_used_when_the_workspace_asks(monkeypatch):
    """End-to-end at the router: the coding profile serves a coding surface."""
    from giscode.inference.router import ModelRouter
    from giscode.providers.profiles import ModelProfile

    seen: list[str] = []

    class _Profiles:
        def active_for(self, purpose):
            seen.append(purpose)
            if purpose != "coding":
                return None
            return ModelProfile("p1", "coding", "Coding", "ollama", "qwen3:14b")

        def last_good_for(self, purpose):
            return None

    router = ModelRouter(builtin=None, settings={}, profiles=_Profiles())
    router.purpose = purpose_for_surface(has_image=False)

    target = router.active_target()

    assert target.kind == "ollama"
    assert target.model == "qwen3:14b"
    assert seen == ["coding"]
