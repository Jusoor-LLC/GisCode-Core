"""The window's router must actually see the profile saved in Model Manager.

This is the bug this test exists for: an adapter that offered ``get``/``set`` but
not ``save`` was rejected by the settings layer, so the router quietly became
``None`` and Chat fell back to the "cannot carry out" refusal while Model Manager
showed an active Ollama profile. Silently returning ``None`` is the failure mode
worth pinning down.
"""

from __future__ import annotations

import tkinter as tk

import pytest

pytest.importorskip("tkinter")

from giscode.gui.main_window import MainWindow  # noqa: E402
from giscode.providers.profiles import ProfileStore  # noqa: E402


class _WindowSettings:
    """A settings store shaped like MainWindow's, for the profile store."""

    def __init__(self, data=None):
        self.data = data if data is not None else {}

    def get(self, key, default=None):
        node = self.data
        for part in str(key).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def set(self, key, value):
        node = self.data
        parts = str(key).split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save(self):
        return None


@pytest.fixture()
def window():
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display in this environment
        pytest.skip(f"no Tk display: {exc}")
    root.withdraw()
    win = MainWindow(root)
    try:
        yield win
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _save_profile(win, purpose: str, model: str) -> None:
    class _Source:
        def get(self, key, default=None):
            return win._settings_get(key, default)

        def set(self, key, value):
            if not win._settings_set(key, value):
                raise RuntimeError("settings are not writable")

        def save(self):
            return None

    ProfileStore(_Source()).create(
        purpose=purpose,
        provider="ollama",
        model=model,
        label="GUI routing",
        make_active=True,
    )


def test_the_router_sees_the_active_profile(window):
    _save_profile(window, "chat", "qwen3:14b")
    window._model_router_cache = None

    router = window._selected_model_router("chat")

    assert router is not None, "the window router was silently dropped"
    target = router.active_target()
    assert target.kind == "ollama"
    assert target.model == "qwen3:14b"
    assert target.profile_id


def test_the_router_is_cached_per_purpose(window):
    _save_profile(window, "coding", "qwen3-coder:30b")
    window._model_router_cache = None

    first = window._selected_model_router("coding")
    second = window._selected_model_router("coding")

    assert first is second
    assert first.active_target().model == "qwen3-coder:30b"
