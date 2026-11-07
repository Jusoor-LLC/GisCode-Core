"""Experience Layer for GisCode.

Adds optional, fully isolated personality, sound and visual-effect
support on top of the existing coding/project workflow. Nothing in
this package ever blocks, delays, or alters the technical result of
any real subsystem — it only contributes cosmetic enhancements that
fail open.

Submodules:
    * :mod:`.settings`  – user-controlled configuration
    * :mod:`.events`    – lightweight synchronous event bus
    * :mod:`.personality` – tone / humor enhancer
    * :mod:`.sounds`    – fire-and-forget sound player
    * :mod:`.effects`   – visual-effect hook dispatcher

Any failure inside the experience layer is contained — callers
always see a successful return value.
"""

from giscode.experience.events import EventBus, EventName, safe_dispatch
from giscode.experience.settings import (
    DEFAULT_SETTINGS,
    ExperienceSettings,
    ExperienceSettingsStore,
    PersonalityMode,
)
from giscode.experience.personality import (
    PersonalityEngine,
    can_use_humor,
    enhance,
)
from giscode.experience.sounds import SoundPlayer
from giscode.experience.effects import EffectDispatcher, EffectName

__all__ = [
    "DEFAULT_SETTINGS",
    "EffectDispatcher",
    "EffectName",
    "EventBus",
    "EventName",
    "ExperienceSettings",
    "ExperienceSettingsStore",
    "PersonalityEngine",
    "PersonalityMode",
    "SoundPlayer",
    "can_use_humor",
    "enhance",
    "safe_dispatch",
]
