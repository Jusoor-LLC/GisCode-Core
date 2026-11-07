"""Accept either a SettingsManager or a plain mapping.

The provider layer needs ``get``/``set``/``save``. Real runs hand it the
application's ``SettingsManager``; tests and a few GUI paths hand it a plain
``dict`` (which has ``get`` but no ``set``). Rather than teaching every provider
class about both shapes, this adapter normalises them once.

A dict source keeps dotted keys, mirroring SettingsManager, so ``providers`` and
``profiles`` land in the same nested structure either way.
"""
from __future__ import annotations

from typing import Any, Optional


class DictSettings:
    """Settings-shaped wrapper around an in-memory mapping."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in str(key).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def set(self, key: str, value: Any) -> None:
        parts = str(key).split(".")
        node = self._data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def save(self) -> None:
        """Nothing to flush: the mapping is the storage."""
        return None


def as_settings_source(source: Any) -> Optional[Any]:
    """Return an object with ``get``/``set``/``save``, or ``None``."""
    if source is None or source is False:
        return None
    if callable(getattr(source, "set", None)) and callable(getattr(source, "save", None)):
        return source
    if isinstance(source, dict):
        return DictSettings(source)
    return None
