"""Persistent model profiles.

A profile binds a *purpose* (coding, reasoning, chat, vision) to a provider and a
model, so switching what GisCode uses is a selection rather than a global edit.

The store is intentionally small and explicit:

* Items are a list with stable ids, so a rename never rewrites a selection.
* ``active`` maps purpose to profile id - exactly one active profile per purpose.
* ``last_good`` remembers the last profile that actually answered, so a failed
  switch can be undone instead of guessed at.

Migration is one-way and additive: an older install whose only model setting is
``model.selected_model`` gets one coding profile using the built-in
``llama.cpp`` runtime. Existing settings are never deleted.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from giscode.providers.base import LLAMA_CPP

PURPOSES = ("coding", "reasoning", "chat", "vision")

_PURPOSE_LABELS = {
    "coding": "Coding",
    "reasoning": "Reasoning",
    "chat": "General chat",
    "vision": "Vision",
}


@dataclass(frozen=True)
class ModelProfile:
    id: str
    purpose: str
    label: str
    provider: str
    model: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "purpose": self.purpose,
            "label": self.label,
            "provider": self.provider,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Optional["ModelProfile"]:
        try:
            purpose = str(data.get("purpose") or "").strip().lower()
            if purpose not in PURPOSES:
                return None
            profile_id = str(data.get("id") or "").strip()
            if not profile_id:
                return None
            provider = str(data.get("provider") or "").strip()
            model = str(data.get("model") or "").strip()
            if not provider or not model:
                return None
            return cls(
                id=profile_id,
                purpose=purpose,
                label=str(data.get("label") or _PURPOSE_LABELS.get(purpose, purpose)),
                provider=provider,
                model=model,
            )
        except (TypeError, ValueError):
            return None


def default_label(purpose: str) -> str:
    return _PURPOSE_LABELS.get(purpose, purpose.title())


class ProfileStore:
    """Reads and writes profiles through the existing settings manager."""

    KEY = "profiles"

    def __init__(self, settings) -> None:
        self._settings = settings

    # -- raw state ---------------------------------------------------------
    def _state(self) -> dict:
        stored = self._settings.get(self.KEY, None)
        state = stored if isinstance(stored, dict) else {}
        items = state.get("items")
        active = state.get("active")
        last_good = state.get("last_good")
        return {
            "items": [i for i in items if isinstance(i, dict)] if isinstance(items, list) else [],
            "active": active if isinstance(active, dict) else {},
            "last_good": last_good if isinstance(last_good, dict) else {},
        }

    def _save(self, state: dict) -> None:
        self._settings.set(self.KEY, state)
        self._settings.save()

    # -- reads -------------------------------------------------------------
    def all(self) -> list[ModelProfile]:
        profiles: list[ModelProfile] = []
        for raw in self._state()["items"]:
            profile = ModelProfile.from_dict(raw)
            if profile is not None:
                profiles.append(profile)
        return profiles

    def get(self, profile_id: str) -> Optional[ModelProfile]:
        for profile in self.all():
            if profile.id == profile_id:
                return profile
        return None

    def for_purpose(self, purpose: str) -> list[ModelProfile]:
        purpose = (purpose or "").strip().lower()
        return [p for p in self.all() if p.purpose == purpose]

    def active_id(self, purpose: str) -> str:
        value = self._state()["active"].get(purpose)
        return str(value) if value else ""

    def active_for(self, purpose: str) -> Optional[ModelProfile]:
        active = self.active_id(purpose)
        if active:
            profile = self.get(active)
            if profile is not None and profile.purpose == purpose:
                return profile
        # No explicit choice: fall back to the first profile for that purpose,
        # which is what a freshly migrated install has.
        candidates = self.for_purpose(purpose)
        return candidates[0] if candidates else None

    def last_good_for(self, purpose: str) -> Optional[ModelProfile]:
        value = self._state()["last_good"].get(purpose)
        return self.get(str(value)) if value else None

    # -- writes ------------------------------------------------------------
    def create(
        self,
        purpose: str,
        *,
        provider: str,
        model: str,
        label: str = "",
        make_active: bool = False,
    ) -> ModelProfile:
        purpose = (purpose or "").strip().lower()
        if purpose not in PURPOSES:
            raise ValueError(f"unknown purpose {purpose!r}; expected one of {PURPOSES}")
        if not provider or not model:
            raise ValueError("a profile needs both a provider and a model")
        profile = ModelProfile(
            id=f"profile-{uuid.uuid4().hex[:12]}",
            purpose=purpose,
            label=label.strip() or default_label(purpose),
            provider=provider,
            model=model,
        )
        state = self._state()
        state["items"].append(profile.as_dict())
        self._save(state)
        if make_active or not self.active_id(purpose):
            self.set_active(profile.id)
        return profile

    def update(
        self,
        profile_id: str,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        label: Optional[str] = None,
    ) -> ModelProfile:
        state = self._state()
        for raw in state["items"]:
            if str(raw.get("id")) != profile_id:
                continue
            if provider is not None:
                raw["provider"] = provider.strip()
            if model is not None:
                raw["model"] = model.strip()
            if label is not None:
                raw["label"] = label.strip() or raw.get("label")
            updated = ModelProfile.from_dict(raw)
            if updated is None:
                raise ValueError("the update would leave the profile unusable")
            self._save(state)
            return updated
        raise KeyError(f"unknown profile {profile_id!r}")

    def rename(self, profile_id: str, label: str) -> ModelProfile:
        return self.update(profile_id, label=label)

    def delete(self, profile_id: str) -> bool:
        state = self._state()
        before = len(state["items"])
        state["items"] = [i for i in state["items"] if str(i.get("id")) != profile_id]
        if len(state["items"]) == before:
            return False
        for purpose, active in list(state["active"].items()):
            if str(active) == profile_id:
                state["active"].pop(purpose, None)
        for purpose, good in list(state["last_good"].items()):
            if str(good) == profile_id:
                state["last_good"].pop(purpose, None)
        self._save(state)
        return True

    def set_active(self, profile_id: str) -> None:
        profile = self.get(profile_id)
        if profile is None:
            raise KeyError(f"unknown profile {profile_id!r}")
        state = self._state()
        state["active"][profile.purpose] = profile.id
        self._save(state)

    def mark_good(self, profile_id: str) -> None:
        """Record a profile that proved usable, for a later undo."""
        profile = self.get(profile_id)
        if profile is None:
            return
        state = self._state()
        state["last_good"][profile.purpose] = profile.id
        self._save(state)

    def restore_last_good(self, purpose: str) -> Optional[ModelProfile]:
        profile = self.last_good_for(purpose)
        if profile is None:
            return None
        self.set_active(profile.id)
        return profile

    # -- migration ---------------------------------------------------------
    def migrate(self) -> str:
        """Create a profile from legacy settings when none exist.

        Returns a short description of what happened (``""`` when nothing was
        needed) so startup can log it and a test can assert it.
        """
        if self.all():
            return ""
        selected = str(self._settings.get("model.selected_model", "") or "").strip()
        if not selected:
            return ""
        profile = self.create(
            "coding",
            provider=LLAMA_CPP,
            model=selected,
            label="Coding (migrated from the previous model setting)",
            make_active=True,
        )
        self.mark_good(profile.id)
        return f"created profile {profile.id} for the previously selected model {selected!r}"
