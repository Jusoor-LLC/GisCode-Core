"""Crash-safe first-run state.

One schema-versioned document lives under ``onboarding`` in the profile's
``config.json``, so it travels with the installation instead of sitting in a
second file that can go missing.

Three properties matter more than the shape of the document:

* **A write is atomic.** ``config.json`` is read, one key is replaced, and the
  result is written to a temporary file in the same directory and renamed over
  the original. A crash mid-save therefore cannot truncate the user's whole
  configuration - which is what a plain ``json.dump`` onto the live file does.
* **Every other key is preserved.** Only ``onboarding`` is touched, so pairing,
  providers, profiles, UI preferences and history are untouched.
* **The stage is the last one that *finished*.** Advancing is explicit, and the
  download stage records whether a pull was interrupted, so a restart resumes at
  a real checkpoint rather than pretending nothing happened or starting over.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from giscode.config.manager import ConfigManager

SCHEMA = 1
KEY = "onboarding"

#: Ordered stages. ``complete`` is the only one that may set ``completed``.
STAGES = (
    "welcome",
    "runtime",
    "hardware",
    "model",
    "download",
    "validate",
    "first_task",
    "complete",
)

#: A pull whose process is gone without a verdict is reported as interrupted
#: rather than silently retried.
DOWNLOAD_PENDING = "pending"
DOWNLOAD_RUNNING = "running"
DOWNLOAD_DONE = "done"
DOWNLOAD_FAILED = "failed"
DOWNLOAD_CANCELLED = "cancelled"
DOWNLOAD_INTERRUPTED = "interrupted"

_HISTORY_LIMIT = 60


def now() -> str:
    """UTC timestamp with second precision - enough to order user actions."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class OnboardingState:
    schema: int = SCHEMA
    stage: str = "welcome"
    completed: bool = False
    legacy: bool = False
    started_at: str = ""
    updated_at: str = ""
    chosen_model: str = ""
    runtime: dict = field(default_factory=dict)
    hardware: dict = field(default_factory=dict)
    download: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    first_task: dict = field(default_factory=dict)
    consents: dict = field(default_factory=dict)
    attempts: dict = field(default_factory=dict)
    last_error: str = ""
    history: list = field(default_factory=list)

    # -- serialisation ------------------------------------------------------
    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "OnboardingState":
        """Tolerant load: a damaged or older document must not break startup."""
        if not isinstance(data, dict):
            return cls()
        stage = str(data.get("stage") or "welcome").strip().lower()
        if stage not in STAGES:
            # An unknown stage is a newer or corrupted document. Keep the
            # completion flag if it is set, otherwise restart the sequence.
            stage = "complete" if data.get("completed") else "welcome"

        def _dict(name: str) -> dict:
            value = data.get(name)
            return dict(value) if isinstance(value, dict) else {}

        history = data.get("history")
        attempts = _dict("attempts")
        return cls(
            schema=int(data.get("schema") or SCHEMA),
            stage=stage,
            completed=bool(data.get("completed")) and stage == "complete",
            legacy=bool(data.get("legacy")),
            started_at=str(data.get("started_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            chosen_model=str(data.get("chosen_model") or ""),
            runtime=_dict("runtime"),
            hardware=_dict("hardware"),
            download=_dict("download"),
            validation=_dict("validation"),
            first_task=_dict("first_task"),
            consents=_dict("consents"),
            attempts={
                str(key): int(value)
                for key, value in attempts.items()
                if isinstance(value, (int, float))
            },
            last_error=str(data.get("last_error") or ""),
            history=[item for item in history if isinstance(item, dict)][-_HISTORY_LIMIT:]
            if isinstance(history, list)
            else [],
        )

    # -- transitions --------------------------------------------------------
    def stage_index(self) -> int:
        try:
            return STAGES.index(self.stage)
        except ValueError:
            return 0

    def note(self, event: str, detail: str = "") -> None:
        self.history.append({"at": now(), "event": event, "detail": str(detail)[:400]})
        del self.history[:-_HISTORY_LIMIT]

    def advance(self, stage: str, **fields: Any) -> "OnboardingState":
        """Move to *stage* and record why. Never moves backwards implicitly."""
        if stage not in STAGES:
            raise ValueError(f"unknown onboarding stage: {stage}")
        for name, value in fields.items():
            if not hasattr(self, name):
                raise AttributeError(f"unknown onboarding field: {name}")
            setattr(self, name, value)
        previous = self.stage
        if STAGES.index(stage) >= self.stage_index():
            self.stage = stage
        self.completed = stage == "complete" and bool(self.completed)
        self.updated_at = now()
        if previous != self.stage:
            self.note("stage", f"{previous} -> {self.stage}")
        return self

    def bump_attempt(self, key: str) -> int:
        self.attempts[key] = int(self.attempts.get(key, 0)) + 1
        return self.attempts[key]

    def fail(self, message: str) -> None:
        self.last_error = str(message)[:600]
        self.note("error", self.last_error)

    def clear_error(self) -> None:
        self.last_error = ""


class OnboardingStore:
    """Reads and writes :class:`OnboardingState` through the profile config."""

    def __init__(self, config_path: Path | None = None) -> None:
        self._config = ConfigManager(config_path)
        self.path = Path(self._config.config_path)

    # -- reads --------------------------------------------------------------
    def load(self) -> OnboardingState:
        state = OnboardingState.from_dict(self._config.get(KEY, None))
        self._reconcile_interrupted_download(state)
        return state

    def is_complete(self) -> bool:
        return self.load().completed

    def raw(self) -> dict:
        value = self._config.get(KEY, None)
        return dict(value) if isinstance(value, dict) else {}

    # -- writes -------------------------------------------------------------
    def save(self, state: OnboardingState) -> OnboardingState:
        state.schema = SCHEMA
        state.updated_at = now()
        data = self._config.load()
        data[KEY] = state.as_dict()
        self._atomic_write(data)
        return state

    def _atomic_write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".config-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    # -- recovery -----------------------------------------------------------
    @staticmethod
    def _reconcile_interrupted_download(state: OnboardingState) -> None:
        """A pull left ``running``/``pending`` by a dead process is interrupted.

        The process that owned the transfer cannot survive the restart, so the
        honest state is "interrupted, retry available" - not "downloading" and
        not "done". Retrying is a user action because it costs bandwidth.
        """
        status = str(state.download.get("status") or "").lower()
        if status in (DOWNLOAD_PENDING, DOWNLOAD_RUNNING):
            state.download = {
                **state.download,
                "status": DOWNLOAD_INTERRUPTED,
                "detail": "GisCode closed before the download finished. Retry to resume.",
                "percent": int(state.download.get("percent") or 0),
            }
            state.note("download_interrupted", state.download.get("model", ""))
