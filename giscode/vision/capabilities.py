from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from giscode.models.types import ModelRegistry

CAP_TEXT = "text"
CAP_CHAT = "chat"
CAP_CODING = "coding"
CAP_VISION = "vision"

_VISION_NAME_HINTS = (
    "vl",
    "vision",
    "vlm",
    "multimodal",
    "llava",
    "minicpm-v",
    "gemma-3",
    "gemma-4",
    "internvl",
)


def is_mmproj_path(path: str | Path) -> bool:
    return "mmproj" in Path(str(path)).name.lower()


def _has_mmproj_sibling(path: str | Path) -> bool:
    try:
        parent = Path(str(path)).parent
        return any("mmproj" in p.name.lower() for p in parent.iterdir() if p.is_file())
    except OSError:
        return False


def is_vision_capable(metadata) -> bool:
    """Heuristic capability check without requiring a live model load."""
    name = (metadata.name or "").lower()
    tags = [t.lower() for t in getattr(metadata, "tags", []) or []]
    caps = [c.lower() for c in getattr(metadata, "capabilities", []) or []]
    if "vision" in caps:
        return True
    if any("vision" in t or "vl" == t or "vlm" == t for t in tags):
        return True
    if any(hint in name for hint in _VISION_NAME_HINTS):
        return True
    path = getattr(metadata, "path", "") or ""
    if path and _has_mmproj_sibling(path):
        return True
    return False


def compute_capabilities(metadata) -> list[str]:
    """Return the capability list for a model metadata object.

    Text/chat are universal for discovered GGUF chat models. Coding is
    inferred from name/tags. Vision is inferred from name hints or a
    sibling *.mmproj projector file (the signal that a VLM is usable).
    """
    name = (metadata.name or "").lower()
    tags = [t.lower() for t in getattr(metadata, "tags", []) or []]
    path = getattr(metadata, "path", "") or ""

    caps = [CAP_TEXT, CAP_CHAT]
    if "coding" in tags or "coder" in name or "code" in tags:
        caps.append(CAP_CODING)
    if is_vision_capable(metadata):
        caps.append(CAP_VISION)
    return caps


def find_vision_model(registry: ModelRegistry, exclude_names: Optional[set[str]] = None) -> object | None:
    """Return the most suitable vision-capable model in the registry."""
    exclude = exclude_names or set()
    candidates: list = []
    for meta in registry.list_all():
        if meta.name in exclude:
            continue
        if is_mmproj_path(meta.path):
            continue
        if is_vision_capable(meta):
            candidates.append(meta)
    if not candidates:
        return None
    candidates.sort(
        key=lambda m: (not getattr(m, "path", "") or not Path(m.path).exists(), m.size_bytes or 2 ** 63)
    )
    return candidates[0]