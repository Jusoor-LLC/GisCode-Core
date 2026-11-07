from types import SimpleNamespace

from giscode.agentic.loop import CodingAgentLoop
from giscode.agentic.planner import MockPlanner
from giscode.agentic.types import TaskRequest
from giscode.gui.main_window import MainWindow
from giscode.inference.api import InferenceAPI
from giscode.inference.mocks import MockLlamaCppRuntime
from giscode.task_control.manager import TaskManager
from giscode.workspace.workspace import Workspace


def _build_loop(tmp_path):
    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager, planner=MockPlanner())
    return loop, task_manager


def test_loop_refuses_to_adopt_an_already_active_task(tmp_path):
    """A GUI-initiated task (already active on the shared TaskManager) must not
    be adopted by the coding agent: that task's budget, cancellation and
    rollback records belong to the caller that started it."""
    loop, manager = _build_loop(tmp_path)
    gui_task = manager.start_task("gui-123", max_duration=60.0)

    request = TaskRequest(task_id="task-001", description="Fix the bug")
    result = loop.start(request)

    assert result.status.value == "failed"
    assert result.failure_info["reason"] == "task_manager_busy"
    assert "no changes were made" in (result.error or "")
    # The other task is untouched: still active, not completed, not cancelled.
    assert manager.get_active_task() is gui_task
    assert manager.get_last_task() is None
    # Nothing was planned or written in the workspace.
    assert not (tmp_path / "src" / "example.py").exists()


def test_loop_starts_own_task_when_none_active(tmp_path):
    """When nothing is active, the coding agent still owns its own task."""
    loop, manager = _build_loop(tmp_path)

    request = TaskRequest(task_id="task-002", description="Write a function")
    result = loop.start(request)

    assert "Only one active task" not in (getattr(result, "error", "") or "")
    assert manager.get_active_task() is None
    assert manager.get_last_task() is not None


class _RegisteredPathModelManager:
    def __init__(self):
        self.loaded = None
        self._settings = {"model.selected_model": "Model-A"}

    def get_current_model(self):
        return self.loaded

    def auto_register_discovered(self):
        pass

    def get_registry(self):
        return SimpleNamespace(
            list_all=lambda: [
                SimpleNamespace(name="Model-A", path=r"C:\models\Model-A\a.gguf"),
                SimpleNamespace(name="Model-B", path=r"C:\models\Model-B\b.gguf"),
                SimpleNamespace(
                    name="Qwen3-VL-4B-Instruct",
                    path=r"C:\models\Qwen3-VL-4B-Instruct-GGUF\Qwen3-VL-4B-Instruct-Q4_K_M.gguf",
                ),
                SimpleNamespace(name="mmproj-x", path=r"C:\models\x\mmproj-x.gguf"),
            ]
        )

    def get_health(self):
        return {"model_loaded": True}


def _make_window(tk_root, mm):
    mw = MainWindow(tk_root, model_manager=mm, gisai_engine=None)
    mw._refresh_model_list()
    return mw


def test_poll_maps_loaded_variant_name_to_registered_selector_value(tk_root):
    mm = _RegisteredPathModelManager()
    mw = _make_window(tk_root, mm)
    mm.loaded = SimpleNamespace(
        name="Qwen3-VL-4B-Instruct-Q4_K_M",
        path=r"C:\models\Qwen3-VL-4B-Instruct-GGUF\Qwen3-VL-4B-Instruct-Q4_K_M.gguf",
    )
    mw._poll_model_state()
    values = list(mw.chat_model_combo["values"] or [])
    assert mw.model_name_var.get() == "Qwen3-VL-4B-Instruct"
    assert mw.chat_model_combo.get() == "Qwen3-VL-4B-Instruct"
    assert mw.model_name_var.get() in values


def test_poll_keeps_loaded_name_when_no_registry_path_match(tk_root):
    mm = _RegisteredPathModelManager()
    mw = _make_window(tk_root, mm)
    mm.loaded = SimpleNamespace(name="Mystery-Model-Q8_0", path=r"C:\models\other\mystery.gguf")
    mw._poll_model_state()
    assert mw.model_name_var.get() == "Mystery-Model-Q8_0"


def _build_loaded_loop(tmp_path, model_name="Gemma"):
    from giscode.agentic.loop import CodingAgentLoop
    from giscode.agentic.planner import MockPlanner
    from giscode.inference.api import InferenceAPI
    from giscode.inference.lifecycle import ModelInfo, ModelState
    from giscode.inference.mocks import MockLlamaCppRuntime
    from giscode.task_control.manager import TaskManager
    from giscode.workspace.workspace import Workspace

    workspace = Workspace(tmp_path / "ws")
    inference = InferenceAPI(runtime=MockLlamaCppRuntime(available=True))
    inference._model = ModelInfo(
        name=model_name,
        path=r"C:\models\loaded\model.gguf",
        state=ModelState.READY,
        size_bytes=1,
        context_length=2048,
    )
    inference._runtime.start_server(r"C:\models\loaded\model.gguf")
    task_manager = TaskManager()
    loop = CodingAgentLoop(workspace, inference, task_manager, planner=MockPlanner())
    return loop, inference, task_manager


def test_routing_prefers_currently_loaded_model(tmp_path):
    from unittest.mock import MagicMock

    from giscode.routing.types import (
        ContextBundle,
        ModelAvailability,
        ModelRole,
        RoutingResult,
    )

    loop, inference, task_manager = _build_loaded_loop(tmp_path, model_name="Gemma")
    routing_service = MagicMock()
    routing_service._model_manager = SimpleNamespace(get_model_status=lambda n: SimpleNamespace(path=r"C:\models\loaded\model.gguf"))
    routing_service.route_and_assemble.side_effect = [
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.PLANNING,
                selected_model="Gemma",
                availability=ModelAvailability.LOADED,
                reason="preferred",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.PLANNING,
                selected_model="Gemma",
                namespace="default",
                project_id=None,
            ),
        ),
        (
            RoutingResult(
                task_id="t1",
                selected_role=ModelRole.CODING,
                selected_model="Gemma",
                availability=ModelAvailability.LOADED,
                reason="preferred",
            ),
            ContextBundle(
                task_id="t1",
                task_text="test",
                selected_role=ModelRole.CODING,
                selected_model="Gemma",
                namespace="default",
                project_id=None,
            ),
        ),
    ]
    loop._routing_service = routing_service

    request = TaskRequest(task_id="t1", description="Explain a concept")
    result = loop.start(request)

    assert "Only one active task" not in (getattr(result, "error", "") or "")
    planning_call = routing_service.route_and_assemble.call_args_list[0][0][0]
    assert planning_call.preferred_model == "Gemma"
    implementation_call = routing_service.route_and_assemble.call_args_list[1][0][0]
    assert implementation_call.preferred_model == "Gemma"