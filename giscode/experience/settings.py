"""User-controlled configuration for the Experience Layer.

Settings are stored as a small JSON file under
``~/.giscode/experience.json`` so the layer has zero coupling to
``giscode.config.settings``. The default values are conservative and
pleasant: friendly personality with subtle humor, sounds off, effects
on, volume at 60 %.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from giscode.config.paths import giscode_home


def default_settings_path() -> Path:
    """Where the experience settings JSON lives (profile aware)."""
    return giscode_home() / "experience.json"


class PersonalityMode(str, Enum):
    PROFESSIONAL = "professional"
    FRIENDLY = "friendly"
    FRIENDLY_HUMOR = "friendly_humor"
    SERIOUS = "serious"


@dataclass
class ExperienceSettings:
    personality_mode: str = PersonalityMode.FRIENDLY_HUMOR.value
    humor_enabled: bool = True
    sounds_enabled: bool = False
    effects_enabled: bool = True
    volume: float = 0.6
    silent_mode: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ExperienceSettings":
        valid_keys = {f for f in cls().__dataclass_fields__.keys()}
        clean = {k: v for k, v in (data or {}).items() if k in valid_keys}
        try:
            return cls(**clean)
        except Exception:
            return cls()

    def merged(self, overrides: Optional[dict]) -> "ExperienceSettings":
        if not overrides:
            return self
        base = self.to_dict()
        base.update({k: v for k, v in overrides.items() if k in base})
        return ExperienceSettings.from_dict(base)


DEFAULT_SETTINGS = ExperienceSettings()


class ExperienceSettingsStore:
    """Thread-safe JSON-backed settings store.

    The store is intentionally independent of
    :class:`giscode.config.settings.SettingsManager` so that the
    experience layer can be used in tests, CLI tools, or alternate
    front-ends without dragging in the full bootstrap.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = Path(path) if path else default_settings_path()
        self._lock = threading.Lock()
        self._settings = self._load()

    @property
    def path(self) -> str:
        return str(self._path)

    def _load(self) -> ExperienceSettings:
        if not self._path.exists():
            return ExperienceSettings()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return ExperienceSettings()
            return ExperienceSettings.from_dict(data)
        except Exception:
            return ExperienceSettings()

    def _save_unlocked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._settings.to_dict(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception:
            pass

    def get(self) -> ExperienceSettings:
        with self._lock:
            return ExperienceSettings.from_dict(self._settings.to_dict())

    def update(self, **changes: Any) -> ExperienceSettings:
        with self._lock:
            base = self._settings.to_dict()
            for k, v in changes.items():
                if k in base:
                    base[k] = v
            self._settings = ExperienceSettings.from_dict(base)
            snapshot = ExperienceSettings.from_dict(self._settings.to_dict())
        self._save_unlocked()
        return snapshot

    def reset(self) -> ExperienceSettings:
        with self._lock:
            self._settings = ExperienceSettings()
            snapshot = ExperienceSettings.from_dict(self._settings.to_dict())
        self._save_unlocked()
        return snapshot
