"""Visual-effect dispatcher.

The dispatcher is a registration system. The future GUI registers
callables (one per named effect) and the application can fire effects
without knowing what they do. Effects are fire-and-forget; if the
GUI hasn't registered anything for a given effect, the call is a
no-op. If a registered callable raises, the dispatcher swallows the
exception so the caller is never affected.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from giscode.experience.settings import (
    DEFAULT_SETTINGS,
    ExperienceSettings,
)


class EffectName:
    THINKING = "thinking"
    PROCESSING = "processing"
    BUTTON_HOVER = "button_hover"
    BUTTON_PRESS = "button_press"
    TASK_PROGRESS = "task_progress"
    MODEL_LOADING = "model_loading"
    MODEL_READY = "model_ready"
    COMPLETION_CHECK = "completion_check"
    WARNING = "warning"
    ERROR = "error"
    PROJECT_PROGRESS = "project_progress"
    RESEARCH_ACTIVE = "research_active"
    STARTUP = "startup"
    READY = "ready"

    ALL: tuple[str, ...] = (
        THINKING,
        PROCESSING,
        BUTTON_HOVER,
        BUTTON_PRESS,
        TASK_PROGRESS,
        MODEL_LOADING,
        MODEL_READY,
        COMPLETION_CHECK,
        WARNING,
        ERROR,
        PROJECT_PROGRESS,
        RESEARCH_ACTIVE,
        STARTUP,
        READY,
    )


EffectCallable = Callable[[str, dict], None]


class EffectDispatcher:
    def __init__(self, settings: Optional[ExperienceSettings] = None) -> None:
        self._settings = settings or DEFAULT_SETTINGS
        self._handlers: dict[str, list[EffectCallable]] = {}
        self._lock = threading.Lock()
        self._fire_count = 0
        self._error_count = 0

    @property
    def settings(self) -> ExperienceSettings:
        return self._settings

    def configure(self, settings: ExperienceSettings) -> None:
        self._settings = settings

    def is_enabled(self) -> bool:
        if self._settings.silent_mode:
            return False
        return bool(self._settings.effects_enabled)

    def register(self, effect_name: str, handler: EffectCallable) -> Callable[[], None]:
        if effect_name not in EffectName.ALL:
            raise ValueError(f"unknown effect: {effect_name!r}")
        if not callable(handler):
            raise TypeError("handler must be callable")
        with self._lock:
            self._handlers.setdefault(effect_name, []).append(handler)

        def _unregister() -> None:
            with self._lock:
                lst = self._handlers.get(effect_name, [])
                if handler in lst:
                    lst.remove(handler)

        return _unregister

    def unregister(self, effect_name: Optional[str] = None) -> None:
        with self._lock:
            if effect_name is None:
                self._handlers.clear()
            else:
                self._handlers.pop(effect_name, None)

    def handlers_for(self, effect_name: str) -> list[EffectCallable]:
        with self._lock:
            return list(self._handlers.get(effect_name, []))

    def fire(self, effect_name: str, payload: Optional[dict] = None) -> int:
        if effect_name not in EffectName.ALL:
            return 0
        if not self.is_enabled():
            return 0
        handlers = self.handlers_for(effect_name)
        if not handlers:
            return 0
        for handler in handlers:
            try:
                handler(effect_name, dict(payload or {}))
            except Exception:
                with self._lock:
                    self._error_count += 1
        with self._lock:
            self._fire_count += 1
        return len(handlers)

    def stats(self) -> dict:
        with self._lock:
            return {"fired": self._fire_count, "errors": self._error_count}
