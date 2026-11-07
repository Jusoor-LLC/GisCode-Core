import pytest

from giscode.gui import main_window
from giscode.gui.main_window import MainWindow, project_path_in_text


class _FakeEngine:
    """Stands in for GisAiEngine: intent detection plus the poll/cancel hooks."""

    def __init__(self, capabilities):
        self._capabilities = list(capabilities)
        self.cancelled = False

    def _detect_intent(self, text, has_image=False, project_active=False):
        return {"capabilities": list(self._capabilities), "confidence": 0.4, "name": "test"}

    def get_active_task(self):
        return None

    def cancel(self):
        self.cancelled = True

    def set_project_context(self, context):
        return None


def _window(tk_root, capabilities=None):
    engine = None if capabilities is None else _FakeEngine(capabilities)
    window = MainWindow(tk_root, gisai_engine=engine, model_manager=None)
    window._active_project_context = None
    return window


@pytest.fixture
def dialogs(monkeypatch):
    """Record every dialog call so tests can assert on questions asked."""
    calls = {"yesno": [], "askstring": [], "showerror": []}
    monkeypatch.setattr(
        main_window.messagebox,
        "askyesnocancel",
        lambda *a, **k: calls["yesno"].append((a, k)) or True,
    )
    monkeypatch.setattr(
        main_window.messagebox,
        "askyesno",
        lambda *a, **k: calls["yesno"].append((a, k)) or True,
    )
    monkeypatch.setattr(
        main_window.simpledialog,
        "askstring",
        lambda *a, **k: calls["askstring"].append((a, k)) or None,
    )
    monkeypatch.setattr(
        main_window.messagebox,
        "showerror",
        lambda *a, **k: calls["showerror"].append((a, k)),
    )
    return calls


def test_plain_chat_asks_nothing(monkeypatch, tk_root, dialogs):
    """Ordinary conversation must not raise the project-location question."""
    window = _window(tk_root, capabilities=["general_reasoning"])
    monkeypatch.setattr(
        main_window.MainWindow,
        "_request_task_workspace",
        lambda self: pytest.fail("plain chat must not ask for a project"),
    )
    selected = []
    monkeypatch.setattr(
        window, "_select_project_path", lambda *a, **k: selected.append((a, k))
    )

    assert window._resolve_task_project("hello, how are you?") is True
    assert dialogs["yesno"] == []
    assert dialogs["askstring"] == []
    assert selected == []


def test_existing_project_already_selected_asks_nothing(monkeypatch, tk_root, dialogs):
    window = _window(tk_root, capabilities=["coding", "general_reasoning"])
    window._active_project_context = object()
    monkeypatch.setattr(
        main_window.MainWindow,
        "_request_task_workspace",
        lambda self: pytest.fail("a selected project must not be questioned"),
    )

    assert window._resolve_task_project("fix the failing test") is True
    assert dialogs["yesno"] == []
    assert dialogs["askstring"] == []


def test_coding_task_with_no_project_triggers_question(monkeypatch, tk_root, dialogs, tmp_path):
    """A coding task with no project and no path must ask existing-or-new."""
    window = _window(tk_root, capabilities=["coding", "general_reasoning"])
    monkeypatch.setattr(
        main_window.simpledialog,
        "askstring",
        lambda *a, **k: dialogs["askstring"].append((a, k)) or str(tmp_path),
    )
    asked = []
    monkeypatch.setattr(
        window, "_select_project_path", lambda p, project_kind="existing": asked.append(project_kind)
    )

    assert window._resolve_task_project("fix the failing test in the project") is True
    assert len(dialogs["yesno"]) == 1, "should ask existing-or-new first"
    assert len(dialogs["askstring"]) == 1
    assert asked == ["existing"]


def test_existing_branch_asks_for_path_and_checks_folder(monkeypatch, tk_root, dialogs, tmp_path):
    window = _window(tk_root, capabilities=["coding"])
    order = []

    def yesnocancel(*a, **k):
        order.append("type")
        return True

    def askstring(*a, **k):
        order.append("path")
        return str(tmp_path)

    monkeypatch.setattr(main_window.messagebox, "askyesnocancel", yesnocancel)
    monkeypatch.setattr(main_window.simpledialog, "askstring", askstring)
    selected = []
    monkeypatch.setattr(
        window,
        "_select_project_path",
        lambda p, project_kind="existing": selected.append((p, project_kind)),
    )

    assert window._request_task_workspace() is True
    assert order == ["type", "path"], "must ask type before path"
    assert selected == [(str(tmp_path), "existing")]


def test_existing_branch_rejects_missing_folder(monkeypatch, tk_root, dialogs, tmp_path):
    window = _window(tk_root, capabilities=["coding"])
    monkeypatch.setattr(main_window.messagebox, "askyesnocancel", lambda *a, **k: True)
    monkeypatch.setattr(
        main_window.simpledialog, "askstring", lambda *a, **k: str(tmp_path / "nope")
    )
    monkeypatch.setattr(
        window, "_select_project_path", lambda *a, **k: pytest.fail("must not select")
    )

    assert window._request_task_workspace() is False
    assert dialogs["showerror"], "a missing folder must be reported"


def test_new_branch_confirms_before_creating(monkeypatch, tk_root, dialogs, tmp_path):
    window = _window(tk_root, capabilities=["coding"])
    destination = tmp_path / "NewProject"
    order = []

    def yesnocancel(*a, **k):
        order.append("type")
        return False

    def askstring(*a, **k):
        order.append("path")
        return str(destination)

    def confirm(*a, **k):
        order.append("confirm")
        return True

    monkeypatch.setattr(main_window.messagebox, "askyesnocancel", yesnocancel)
    monkeypatch.setattr(main_window.simpledialog, "askstring", askstring)
    monkeypatch.setattr(main_window.messagebox, "askyesno", confirm)
    selected = []
    monkeypatch.setattr(
        window,
        "_select_project_path",
        lambda p, project_kind="existing": selected.append((p, project_kind)),
    )

    assert window._request_task_workspace() is True
    assert order == ["type", "path", "confirm"], "folder created only after confirming"
    assert destination.is_dir()
    assert selected == [(str(destination), "new")]


def test_new_branch_declined_creates_nothing(monkeypatch, tk_root, dialogs, tmp_path):
    window = _window(tk_root, capabilities=["coding"])
    destination = tmp_path / "Declined"
    monkeypatch.setattr(main_window.messagebox, "askyesnocancel", lambda *a, **k: False)
    monkeypatch.setattr(main_window.simpledialog, "askstring", lambda *a, **k: str(destination))
    monkeypatch.setattr(main_window.messagebox, "askyesno", lambda *a, **k: False)
    monkeypatch.setattr(
        window, "_select_project_path", lambda *a, **k: pytest.fail("must not select")
    )

    assert window._request_task_workspace() is False
    assert not destination.exists(), "declining must not create the folder"


def test_cancelling_type_question_stops_task(monkeypatch, tk_root, dialogs):
    window = _window(tk_root, capabilities=["coding"])
    monkeypatch.setattr(main_window.messagebox, "askyesnocancel", lambda *a, **k: None)
    monkeypatch.setattr(
        main_window.simpledialog, "askstring", lambda *a, **k: pytest.fail("no path asked")
    )
    monkeypatch.setattr(
        window, "_select_project_path", lambda *a, **k: pytest.fail("must not select")
    )

    assert window._request_task_workspace() is False


def test_cancel_produces_visible_chat_message(monkeypatch, tk_root):
    """Cancelling must say so in the chat area, not fail silently."""
    window = _window(tk_root, capabilities=["coding"])
    monkeypatch.setattr(main_window.messagebox, "askyesnocancel", lambda *a, **k: None)
    shown = []
    monkeypatch.setattr(window, "_append_response", lambda text, **k: shown.append(text))

    assert window._resolve_task_project("fix the failing test") is False
    assert shown, "cancelling must append a visible message"
    assert "cancel" in shown[0].lower()


def test_task_naming_a_path_asks_nothing(monkeypatch, tk_root, dialogs, tmp_path):
    """A path in the task text is used directly, with no question."""
    existing = tmp_path / "MyProject"
    existing.mkdir()
    window = _window(tk_root, capabilities=["coding"])
    monkeypatch.setattr(
        main_window.MainWindow,
        "_request_task_workspace",
        lambda self: pytest.fail("a task naming a path must not be questioned"),
    )
    selected = []
    monkeypatch.setattr(
        window,
        "_select_project_path",
        lambda p, project_kind="existing": selected.append((p, project_kind)),
    )

    message = f"Please fix the failing test in {existing}"
    assert window._resolve_task_project(message) is True
    assert dialogs["yesno"] == []
    assert dialogs["askstring"] == []
    assert selected == [(str(existing), "existing")]


def test_task_naming_a_new_path_creates_it(monkeypatch, tk_root, dialogs, tmp_path):
    destination = tmp_path / "BrandNew"
    window = _window(tk_root, capabilities=["coding"])
    selected = []
    monkeypatch.setattr(
        window,
        "_select_project_path",
        lambda p, project_kind="existing": selected.append((p, project_kind)),
    )

    message = f"Create a new project in {destination}"
    assert window._resolve_task_project(message) is True
    assert destination.is_dir()
    assert selected == [(str(destination), "new")]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("hello there", None),
        ("fix tests/test_routing.py", None),
        ("work in C:/Users/me/site", "C:/Users/me/site"),
        ("see D:\\projects\\demo", "D:\\projects\\demo"),
        ("put it in /home/me/app", "/home/me/app"),
        ("the repo at C:\\code\\x.", "C:\\code\\x"),
    ],
)
def test_project_path_in_text(message, expected):
    assert project_path_in_text(message) == expected