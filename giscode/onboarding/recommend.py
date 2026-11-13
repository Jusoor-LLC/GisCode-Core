"""Which model should this machine run? One answer, with the reasoning shown.

The catalogue below is deliberately small and explicit. A recommendation is a
claim about *this* machine, so every entry carries the two numbers that make the
claim checkable - the download size and the free space it needs - and the memory
verdict comes from the same estimator the loader uses rather than from a table
of guesses.

Two rules the rest of the system relies on:

* **The recommendation names one tag.** ``qwen2.5-coder:7b`` is what gets pulled
  and validated; nothing silently swaps in a smaller model because a pull was
  slow or a test failed.
* **Nothing is recommended that cannot fit.** Entries whose memory verdict is
  ``likely_insufficient`` or whose disk requirement exceeds free space are
  returned in a separate ``rejected`` list with the reason, so the UI can
  explain the refusal instead of hiding the option.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from giscode.onboarding.hardware import (
    GIB,
    SystemProfile,
    assess_fit,
    fits_disk,
    required_disk_bytes,
)

#: Ollama tags with approximate download sizes. Sizes are the published blob
#: sizes rounded to 0.1 GB; they are shown as approximate because a re-tagged
#: upstream revision can move them.
CATALOG: tuple[dict, ...] = (
    {
        "model": "qwen2.5-coder:1.5b",
        "coding": True,
        "label": "Qwen2.5 Coder 1.5B",
        "size_gb": 1.0,
        "context_length": 8192,
        "note": "Smallest capable coding model; fastest on CPU-only machines.",
    },
    {
        "model": "qwen2.5-coder:3b",
        "coding": True,
        "label": "Qwen2.5 Coder 3B",
        "size_gb": 1.9,
        "context_length": 8192,
        "note": "Good balance for 8 GB machines without a GPU.",
    },
    {
        "model": "qwen2.5-coder:7b",
        "coding": True,
        "label": "Qwen2.5 Coder 7B",
        "size_gb": 4.7,
        "context_length": 8192,
        "note": "Recommended when 8 GB or more of VRAM is available.",
    },
    {
        "model": "qwen2.5-coder:14b",
        "coding": True,
        "label": "Qwen2.5 Coder 14B",
        "size_gb": 9.0,
        "context_length": 8192,
        "note": "Stronger coding; needs a 16 GB GPU or plenty of system RAM.",
    },
    {
        "model": "qwen2.5-coder:32b",
        "coding": True,
        "label": "Qwen2.5 Coder 32B",
        "size_gb": 20.0,
        "context_length": 8192,
        "note": "Large model for 24 GB+ GPUs; slow when offloaded to the CPU.",
    },
    {
        "model": "llama3.2:3b",
        "coding": False,
        "label": "Llama 3.2 3B",
        "size_gb": 2.0,
        "context_length": 8192,
        "note": "General-purpose fallback when a coding model will not fit.",
    },
    {
        "model": "gemma2:2b",
        "coding": False,
        "label": "Gemma 2 2B",
        "size_gb": 1.6,
        "context_length": 8192,
        "note": "Very small general model for low-memory machines.",
    },
)


@dataclass(frozen=True)
class Recommendation:
    model: str
    label: str
    size_bytes: int
    required_disk_bytes: int
    level: str
    headline: str
    detail: str
    note: str = ""
    coding: bool = False
    installed: bool = False
    rank: int = 0

    @property
    def ok(self) -> bool:
        return self.level in ("likely_suitable", "may_fit")

    @property
    def size_gb(self) -> float:
        return round(self.size_bytes / GIB, 1)

    @property
    def required_disk_gb(self) -> float:
        return round(self.required_disk_bytes / GIB, 1)

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "label": self.label,
            "size_bytes": self.size_bytes,
            "size_gb": self.size_gb,
            "required_disk_bytes": self.required_disk_bytes,
            "required_disk_gb": self.required_disk_gb,
            "level": self.level,
            "headline": self.headline,
            "detail": self.detail,
            "note": self.note,
            "coding": self.coding,
            "installed": self.installed,
            "rank": self.rank,
        }


@dataclass(frozen=True)
class Plan:
    """Recommendations plus the ones that were refused, and why."""

    recommended: tuple[Recommendation, ...] = field(default_factory=tuple)
    rejected: tuple[Recommendation, ...] = field(default_factory=tuple)
    hardware_summary: str = ""

    @property
    def best(self) -> Recommendation | None:
        return self.recommended[0] if self.recommended else None

    def as_dict(self) -> dict:
        return {
            "recommended": [item.as_dict() for item in self.recommended],
            "rejected": [item.as_dict() for item in self.rejected],
            "hardware_summary": self.hardware_summary,
        }


def _entry_size(entry: dict) -> int:
    return int(round(float(entry.get("size_gb", 0)) * GIB))


def _to_recommendation(entry: dict, profile: SystemProfile, installed: set[str], rank: int) -> Recommendation:
    size = _entry_size(entry)
    fit = assess_fit(profile, size, context_length=int(entry.get("context_length") or 8192))
    disk_ok = fits_disk(profile, size)
    level = fit.level
    detail = fit.detail
    if not disk_ok:
        level = "insufficient_disk"
        detail = (
            f"Needs about {required_disk_bytes(size) / GIB:.1f} GB free on "
            f"{profile.disk_path or 'the profile drive'}, but "
            f"{profile.disk_free_gb:.1f} GB is available."
        )
    return Recommendation(
        model=str(entry["model"]),
        label=str(entry.get("label") or entry["model"]),
        size_bytes=size,
        required_disk_bytes=required_disk_bytes(size),
        level=level,
        headline=str(getattr(fit, "headline", "")),
        detail=detail,
        note=str(entry.get("note") or ""),
        coding=bool(entry.get("coding")),
        installed=str(entry["model"]) in installed,
        rank=rank,
    )


def recommend(
    profile: SystemProfile,
    *,
    catalog: Sequence[dict] | None = None,
    installed: Iterable[str] = (),
    limit: int = 4,
) -> Plan:
    """Rank the catalogue for *profile*, keeping the refusals visible.

    Ranking prefers, in order: an already-installed model (no download needed),
    a fit with headroom, and a larger model among those. It never returns an
    entry whose level is a refusal as a recommendation.
    """
    entries = list(catalog if catalog is not None else CATALOG)
    installed_set = {str(name) for name in installed}

    scored: list[Recommendation] = []
    for index, entry in enumerate(entries):
        scored.append(_to_recommendation(entry, profile, installed_set, index))

    def sort_key(item: Recommendation) -> tuple:
        return (
            0 if item.installed else 1,
            0 if item.level == "likely_suitable" else (1 if item.level == "may_fit" else 2),
            # This is a coding product: a coding model that fits beats a general
            # model that happens to be a few hundred megabytes larger.
            0 if item.coding else 1,
            -item.size_bytes,
        )

    usable = [item for item in scored if item.ok]
    rejected = [item for item in scored if not item.ok]
    usable.sort(key=sort_key)
    rejected.sort(key=lambda item: item.size_bytes)

    return Plan(
        recommended=tuple(usable[: max(1, int(limit))]),
        rejected=tuple(rejected),
        hardware_summary=profile.summary(),
    )


def describe_insufficient(plan: Plan, profile: SystemProfile) -> str:
    """A plain explanation when nothing in the catalogue fits."""
    smallest = plan.rejected[0] if plan.rejected else None
    if smallest is None:
        return "No model in the catalogue could be evaluated on this machine."
    return (
        f"Nothing in the catalogue fits this machine as measured "
        f"({profile.summary()}). The smallest option, {smallest.label}, "
        f"needs about {smallest.required_disk_gb:.1f} GB free and "
        f"{smallest.detail.rstrip('.')}."
    )
