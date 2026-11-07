"""Optional fire-and-forget sound effects.

The player is deliberately self-contained: no external deps, no
hardware probing, no blocking of the caller. Sounds only play when:

* the player is enabled in :class:`ExperienceSettings`
* ``silent_mode`` is ``False``
* the volume is above zero
* a registered event has a file path that exists on disk

If any of those is false, the call is a no-op. If the file is
missing, the call is also a no-op. If playback fails, the call
swallows the exception.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from giscode.experience.events import EventName
from giscode.experience.settings import (
    DEFAULT_SETTINGS,
    ExperienceSettings,
)


class SoundPlayer:
    """Map event names to optional file paths and play them safely."""

    def __init__(self, settings: Optional[ExperienceSettings] = None) -> None:
        self._settings = settings or DEFAULT_SETTINGS
        self._file_map: dict[str, str] = {}
        self._lock = threading.Lock()
        self._play_count = 0
        self._skip_count = 0
        self._error_count = 0

    @property
    def settings(self) -> ExperienceSettings:
        return self._settings

    def configure(self, settings: ExperienceSettings) -> None:
        self._settings = settings

    def register(self, event_name: str, file_path: Optional[str]) -> None:
        if event_name not in EventName.ALL:
            raise ValueError(f"unknown event: {event_name!r}")
        with self._lock:
            if file_path:
                self._file_map[event_name] = file_path
            else:
                self._file_map.pop(event_name, None)

    def unregister(self, event_name: str) -> None:
        with self._lock:
            self._file_map.pop(event_name, None)

    def file_for(self, event_name: str) -> Optional[str]:
        with self._lock:
            return self._file_map.get(event_name)

    def is_available(self) -> bool:
        if self._settings.silent_mode:
            return False
        if not self._settings.sounds_enabled:
            return False
        if (self._settings.volume or 0.0) <= 0.0:
            return False
        return True

    def stats(self) -> dict:
        with self._lock:
            return {
                "played": self._play_count,
                "skipped": self._skip_count,
                "errors": self._error_count,
            }

    def play(self, event_name: str) -> bool:
        """Schedule a play attempt. Returns True if a real play was
        attempted, False if it was a no-op (silent / disabled / no
        file / missing file). Never raises."""
        try:
            if not self.is_available():
                with self._lock:
                    self._skip_count += 1
                return False
            if event_name not in EventName.ALL:
                with self._lock:
                    self._skip_count += 1
                return False
            file_path = self.file_for(event_name)
            if not file_path:
                with self._lock:
                    self._skip_count += 1
                return False
            if not Path(file_path).is_file():
                with self._lock:
                    self._skip_count += 1
                return False
            t = threading.Thread(
                target=self._play_blocking,
                args=(file_path,),
                daemon=True,
                name=f"experience-sound-{event_name}",
            )
            t.start()
            with self._lock:
                self._play_count += 1
            return True
        except Exception:
            with self._lock:
                self._error_count += 1
            return False

    def _play_blocking(self, file_path: str) -> None:
        """Best-effort playback.

        This implementation intentionally does not import any audio
        library. Real playback is delegated to the OS through a future
        backend; for now we treat the call as successful when the
        file exists. The function is structured so that a real
        backend can be slotted in without changing the public API.
        """
        try:
            # Sanity check: the file must be readable.
            with open(file_path, "rb") as f:
                f.read(1)
        except Exception:
            with self._lock:
                self._error_count += 1
