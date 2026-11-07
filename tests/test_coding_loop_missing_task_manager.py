"""CodingAgentLoop must never crash with a bare NoneType when it has no task
manager to report through.

Production wiring (``giscode/main.py``) always supplies a real TaskManager, so
this only bites harnesses and embedders that pass ``None``. Before the fix the
loop dereferenced ``self._task_manager`` inside its own exception handler, so
the real error was replaced by ``'NoneType' object has no attribute
'complete'`` - an opaque crash instead of a reportable failure.
"""

from pathlib import Path

from giscode.agentic.loop import (
    MISSING_TASK_MANAGER_MESSAGE,
    CodingAgentLoop,
)
from giscode.agentic.types import ExecutionPhase, TaskRequest
from giscode.workspace.workspace import Workspace


class _FakeInference:
    """Minimal model stand-in: the loop must fail before ever consulting it."""

    def __init__(self):
        self.calls = 0

    def chat_generate(self, request, *args, **kwargs):  # pragma: no cover
        self.calls += 1
        raise AssertionError("inference must not be consulted")


def _loop(tmp_path: Path, task_manager) -> CodingAgentLoop:
    workspace = Workspace(tmp_path)
    return CodingAgentLoop(workspace, _FakeInference(), task_manager)


def _request() -> TaskRequest:
    return TaskRequest(task_id="t-missing-tm", description="Add a helper to calc.py")


def test_start_without_task_manager_fails_clearly_instead_of_crashing(tmp_path):
    loop = _loop(tmp_path, task_manager=None)

    result = loop.start(_request())

    assert result.status == ExecutionPhase.FAILED
    assert result.error == MISSING_TASK_MANAGER_MESSAGE
    assert "task manager" in result.error
    assert "no changes made" in result.error
    assert result.failure_info["reason"] == "missing_task_manager"


def test_missing_task_manager_message_never_mentions_nonetype(tmp_path):
    loop = _loop(tmp_path, task_manager=None)

    result = loop.start(_request())

    assert "NoneType" not in (result.error or "")
    assert "attribute" not in (result.error or "")


def test_no_work_is_attempted_without_a_task_manager(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    loop = _loop(tmp_path, task_manager=None)

    loop.start(_request())

    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == (
        "def add(a, b):\n    return a - b\n"
    )
    assert loop._inference.calls == 0


class _ExplodingTaskManager:
    """A task manager whose reporting fails must not replace the real result."""

    def __init__(self):
        self.complete_calls = 0

    def get_active_task(self):
        return None

    def start_task(self, *args, **kwargs):
        return object()

    def update_phase(self, *args, **kwargs):
        return None

    def complete(self, *args, **kwargs):
        self.complete_calls += 1
        raise RuntimeError("task store unavailable")

    def cancel(self, *args, **kwargs):
        return None


def test_completion_bookkeeping_failure_does_not_mask_the_result(tmp_path):
    manager = _ExplodingTaskManager()
    loop = _loop(tmp_path, task_manager=manager)

    loop._complete_task(success=False, failure_info={"reason": "real_error"})

    assert manager.complete_calls == 1


def test_complete_task_is_a_noop_without_a_task_manager(tmp_path):
    loop = _loop(tmp_path, task_manager=None)

    loop._complete_task(success=True)
    loop._complete_task(success=False, failure_info={"reason": "x"})
