"""Project-path extraction from the task text.

The unquoted pattern stops at whitespace, so a path that contains a space used
to be truncated and the truncated folder was then created. A quoted path is
unambiguous and must be used exactly; an unquoted match that is immediately
continued by another path segment is ambiguous and must not be turned into a
directory.
"""

from __future__ import annotations

import pytest

from giscode.gui.main_window import MainWindow, project_path_in_text


class _FakeEngine:
    def _detect_intent(self, text, has_image=False, project_active=False):
        return {"capabilities": ["coding"], "confidence": 0.4, "name": "test"}

    def get_active_task(self):
        return None

    def cancel(self):
        pass

    def set_project_context(self, context):
        return None


@pytest.fixture
def window(tk_root, monkeypatch):
    win = MainWindow(tk_root, gisai_engine=_FakeEngine(), model_manager=None)
    win._active_project_context = None
    return win


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def test_quoted_path_with_spaces_is_extracted_exactly(tmp_path):
    target = tmp_path / "Sample project" / "SampleTasks"
    message = f'Create a new app called Sample Tasks in "{target}"'
    assert project_path_in_text(message) == str(target)


def test_quoted_path_with_trailing_punctuation_is_extracted_exactly(tmp_path):
    target = tmp_path / "Sample project" / "SampleTasks"
    message = f'Create the app in "{target}".'
    assert project_path_in_text(message) == str(target)


def test_unquoted_path_with_spaces_is_ambiguous_not_truncated(tmp_path):
    target = tmp_path / "Sample project" / "SampleTasks"
    message = f"Create a new app in {target}"
    assert project_path_in_text(message) is None, (
        "a match that is continued by another path segment is not the path the "
        "user typed and must not be used"
    )


def test_unquoted_single_token_path_still_extracts(tmp_path):
    target = tmp_path / "BrandNew"
    message = f"Create a new project in {target}"
    assert project_path_in_text(message) == str(target)


# --------------------------------------------------------------------------
# No directory is created from an ambiguous match
# --------------------------------------------------------------------------


def test_ambiguous_path_asks_and_creates_nothing(window, tmp_path, monkeypatch):
    target = tmp_path / "Sample project" / "SampleTasks"
    truncated = tmp_path / "Sample"
    asked: list[bool] = []
    monkeypatch.setattr(
        MainWindow, "_request_task_workspace", lambda self: asked.append(True) or False
    )
    selected: list[str] = []
    monkeypatch.setattr(
        window, "_select_project_path", lambda *a, **k: selected.append(a)
    )

    assert window._resolve_task_project(f"Create a new app in {target}") is False

    assert asked, "an ambiguous path must be confirmed with the user"
    assert selected == []
    assert not truncated.exists(), "the truncated path must never be created"
    assert not target.exists(), "no folder is created before the user confirms"


def test_quoted_path_is_used_exactly_and_creates_that_folder(
    window, tmp_path, monkeypatch
):
    target = tmp_path / "Sample project" / "SampleTasks"
    selected: list[tuple] = []
    monkeypatch.setattr(
        window,
        "_select_project_path",
        lambda p, project_kind="existing": selected.append((p, project_kind)),
    )
    monkeypatch.setattr(
        MainWindow,
        "_request_task_workspace",
        lambda self: pytest.fail("a quoted path needs no question"),
    )

    message = f'Create a new app called Sample Tasks in "{target}"'
    assert window._resolve_task_project(message) is True

    assert selected == [(str(target), "new")]
    assert target.is_dir()
    assert not (tmp_path / "Tests").exists()
