"""Personality / tone enhancer.

Produces a short, optional "flavor" string (greeting, status quip,
closing remark) that callers can append to their UI presentation.
The engine is **never** allowed to wrap or alter:

* technical results (test counts, build status, inference output)
* error messages
* security / safety messages
* code, logs, or stack traces

It is purely a presentation layer. If anything goes wrong inside the
enhancer, the caller gets the original text back unchanged.
"""
from __future__ import annotations

import random
from typing import Optional

from giscode.experience.settings import (
    DEFAULT_SETTINGS,
    ExperienceSettings,
    PersonalityMode,
)


# Categories that must NEVER be wrapped in personality flavor.
_PROTECTED_CATEGORIES = frozenset({
    "error",
    "security",
    "safety",
    "code",
    "log",
    "stacktrace",
    "stack_trace",
    "test_output",
    "build_output",
    "diagnostic",
})


class PersonalityEngine:
    def __init__(self, settings: Optional[ExperienceSettings] = None, rng: Optional[random.Random] = None) -> None:
        self._settings = settings or DEFAULT_SETTINGS
        self._rng = rng or random.Random()

    @property
    def settings(self) -> ExperienceSettings:
        return self._settings

    def configure(self, settings: ExperienceSettings) -> None:
        self._settings = settings

    def can_use_humor(self) -> bool:
        return can_use_humor(self._settings)

    def can_use_personality(self) -> bool:
        if self._settings.silent_mode:
            return False
        return True

    def is_protected(self, category: Optional[str]) -> bool:
        if not category:
            return False
        return category.lower() in _PROTECTED_CATEGORIES

    def greeting(self) -> str:
        if not self.can_use_personality():
            return ""
        mode = self._settings.personality_mode
        if mode == PersonalityMode.SERIOUS.value:
            return "Ready."
        if mode == PersonalityMode.PROFESSIONAL.value:
            return "Ready to help."
        if mode == PersonalityMode.FRIENDLY.value:
            return self._rng.choice([
                "Ready when you are.",
                "Here to help.",
                "Let's get started.",
            ])
        # FRIENDLY_HUMOR
        return self._rng.choice([
            "Ready when you are. 🤝",
            "Here to help. Coffee is virtual, unfortunately.",
            "Let's build something cool. 🛠️",
        ])

    def status_flair(self, status_text: str, category: Optional[str] = None) -> str:
        """Return ``status_text`` unchanged unless personality is enabled
        and the category is not protected. The flavor is a small prefix
        that is *clearly* separated from the technical text."""
        if not self.can_use_personality():
            return status_text
        if self.is_protected(category):
            return status_text
        mode = self._settings.personality_mode
        if mode in (PersonalityMode.SERIOUS.value, PersonalityMode.PROFESSIONAL.value):
            return status_text
        if mode == PersonalityMode.FRIENDLY.value:
            return status_text
        # FRIENDLY_HUMOR with humor on
        if not self._settings.humor_enabled:
            return status_text
        prefix = self._rng.choice([
            "Heads up — ",
            "Quick note — ",
            "FYI — ",
            "By the way — ",
        ])
        return f"{prefix}{status_text}"

    def quip_for(self, event: str) -> str:
        """Optional short one-liner tied to a named event. Returns ``""``
        if the engine decides not to add a quip — callers must accept
        the empty string silently."""
        if not self.can_use_personality():
            return ""
        if not self._settings.humor_enabled:
            return ""
        mode = self._settings.personality_mode
        if mode != PersonalityMode.FRIENDLY_HUMOR.value:
            return ""
        if event == "tests_passed":
            return self._rng.choice([
                "The bugs have apparently decided to cooperate today. 😄",
                "All green. The compiler is in a good mood. ✅",
                "Clean run. Nothing exploded. 🎉",
            ])
        if event == "tests_failed":
            return self._rng.choice([
                "The compiler is feeling chatty today.",
                "Bugs found. The good news: they're findable.",
            ])
        if event == "build_complete":
            return self._rng.choice([
                "Build finished. The bytes are in their new home.",
            ])
        if event == "model_ready":
            return self._rng.choice([
                "Model is warmed up and ready to roll. ☕",
            ])
        if event == "project_completed":
            return self._rng.choice([
                "Project shipped. Time for a stretch. 🧘",
            ])
        return ""


def can_use_humor(settings: ExperienceSettings) -> bool:
    if settings.silent_mode:
        return False
    if not settings.humor_enabled:
        return False
    return settings.personality_mode == PersonalityMode.FRIENDLY_HUMOR.value


def enhance(
    text: str,
    settings: Optional[ExperienceSettings] = None,
    category: Optional[str] = None,
    event: Optional[str] = None,
    rng: Optional[random.Random] = None,
) -> str:
    """One-call helper: apply personality safely to ``text``.

    * Never raises.
    * Never wraps a protected category.
    * Never mutates empty/None input.
    * If the engine rejects, the original text is returned unchanged.
    """
    if not text:
        return text or ""
    try:
        engine = PersonalityEngine(settings=settings or DEFAULT_SETTINGS, rng=rng)
        if engine.is_protected(category):
            return text
        # status_flair returns the same text unless personality is on
        out = engine.status_flair(text, category=category)
        if event:
            quip = engine.quip_for(event)
            if quip:
                out = f"{out}\n{quip}"
        return out
    except Exception:
        return text
