"""Deterministic cancellation tests for the task manager, loop and provider path.

These cover the timing scenarios required for requalification: before
generation, during planning, during provider streaming, during tool execution,
immediately before completion, racing a timeout or provider failure, and
recovery afterwards.
"""
from __future__ import annotations

import threading
import time

from giscode.agentic.loop import CodingAgentLoop
from giscode.agentic.planner import Planner, PlanningError
from giscode.agentic.types import (
    ActionType,
    ActionResult,
    ExecutionPhase,
    ExecutionPlan,
    ExecutionStep,
    TaskRequest,
    VerificationResult,
)
from giscode.inference.errors import RequestCancelledError
from giscode.inference.provider_backend import ProviderChatBackend
from giscode.inference.runtime import ChatMessage, ChatRequest
from giscode.providers.base import Provider, ProviderModel
from giscode.providers.streaming import StreamCancelled, StreamDelta
from giscode.task_control.manager import TaskManager
from giscode.task_control.types import TaskStatus
from giscode.workspace.workspace import Workspace


def _loop(tmp_path, manager, planner):
    (tmp_path / "src").mkdir(exist_ok=True)
    return CodingAgentLoop(
        Workspace(tmp_path, ensure_structure=False),
        inference=None,
        task_manager=manager,
        planner=planner,
    )


def _start(loop, request):
    holder: dict = {}

    def work():
        holder["result"] = loop.start(request)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, holder


class _BlockingPlanner(Planner):
    """Waits for the stop signal, like a real planner waiting on a model."""

    def __init__(self, started: threading.Event):
        self.started = started

    def create_plan(self, request, cancel_event=None):
        self.started.set()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise PlanningError("planning was cancelled")
            time.sleep(0.005)
        raise PlanningError("test timed out waiting for cancellation")


class _StaticPlanner(Planner):
    def __init__(self, plan: ExecutionPlan):
        self.plan = plan

    def create_plan(self, request, cancel_event=None):
        return self.plan


class _OKVerifier:
    def verify(self, *_args, **_kwargs):
        return VerificationResult(success=True, errors=[], details=[])


class _Settings:
    def get(self, key, default=None):
        return True if key == "packaging.enabled" else default


class _SlowPackaging:
    def __init__(self, started: threading.Event):
        self.started = started

    def build(self):
        self.started.set()
        time.sleep(0.3)
        return {"status": "ok"}


class _StreamingProvider(Provider):
    kind = "ollama"
    label = "streaming fake"
    supports_streaming = True

    def __init__(self):
        super().__init__(endpoint="http://127.0.0.1:11434", timeout=5.0)

    def list_models(self):
        return [ProviderModel(id="fake-model", provider="ollama")]

    def chat(self, *_args, **_kwargs):
        raise AssertionError("the blocking chat call must not be used")

    def stream_chat(self, _model, _messages, **kwargs):
        stop = kwargs["should_stop"]
        yield StreamDelta(text="partial answer ")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if stop():
                raise StreamCancelled()
            time.sleep(0.005)
        raise AssertionError("the fake stream was never cancelled")


def _plan(*paths):
    steps = [
        ExecutionStep(
            step_id=f"step-{index}",
            description=f"create {path}",
            action_type=ActionType.CREATE,
            target_path=path,
            content="x = 1\n",
        )
        for index, path in enumerate(paths, start=1)
    ]
    return ExecutionPlan(
        objective="exercise cancellation",
        files_expected_to_change=list(paths),
        steps=steps,
        verification_steps=[],
    )


# --------------------------------------------------------------- task manager


def test_cancel_is_durable_and_distinct_from_a_timeout():
    manager = TaskManager()
    manager.start_task("t1")
    manager.cancel()
    assert manager.is_cancelled() is True
    assert manager.cancel_event.is_set() is True
    assert manager.get_last_task().status == TaskStatus.CANCELLED

    manager.start_task("t2")
    assert manager.is_cancelled() is False
    assert manager.cancel_event.is_set() is False
    manager.complete(success=True)
    assert manager.is_cancelled() is False


def test_a_timeout_aborts_generation_without_being_called_a_cancel():
    manager = TaskManager()
    manager.start_task("t", max_duration=0.05)
    for _ in range(60):
        last = manager.get_last_task()
        if last is not None and last.status == TaskStatus.TIMEOUT:
            break
        time.sleep(0.01)
    assert manager.get_last_task().status == TaskStatus.TIMEOUT
    assert manager.cancel_event.is_set() is True
    assert manager.is_cancelled() is False


# ------------------------------------------------------------ planning phase


def test_cancel_during_planning_is_prompt_and_reports_cancelled(tmp_path):
    manager = TaskManager()
    started = threading.Event()
    loop = _loop(tmp_path, manager, _BlockingPlanner(started))
    thread, holder = _start(
        loop,
        TaskRequest(task_id="plan-cancel", description="do work", max_duration=300),
    )
    assert started.wait(5)

    began = time.monotonic()
    manager.cancel()
    thread.join(2)
    latency = time.monotonic() - began

    assert thread.is_alive() is False
    assert latency < 0.5, f"cancel took {latency:.2f}s to reach the planning call"
    assert holder["result"].status == ExecutionPhase.CANCELLED
    assert holder["result"].failure_info["reason"] == "cancelled"
    assert manager.get_last_task().status == TaskStatus.CANCELLED


def test_cancel_during_planning_racing_the_watchdog_stays_cancelled(tmp_path):
    manager = TaskManager()
    started = threading.Event()
    loop = _loop(tmp_path, manager, _BlockingPlanner(started))
    thread, holder = _start(
        loop,
        TaskRequest(task_id="race", description="do work", max_duration=0.2),
    )
    assert started.wait(5)
    time.sleep(0.05)
    manager.cancel()
    thread.join(2)

    assert holder["result"].status == ExecutionPhase.CANCELLED
    assert manager.get_last_task().status == TaskStatus.CANCELLED
    assert manager.is_cancelled() is True


def test_a_timeout_alone_still_reports_timed_out(tmp_path):
    manager = TaskManager()
    started = threading.Event()
    loop = _loop(tmp_path, manager, _BlockingPlanner(started))
    thread, holder = _start(
        loop,
        TaskRequest(task_id="timeout", description="do work", max_duration=0.15),
    )
    assert started.wait(5)
    thread.join(3)

    assert thread.is_alive() is False
    assert holder["result"].status == ExecutionPhase.TIMED_OUT
    assert manager.get_last_task().status == TaskStatus.TIMEOUT
    assert manager.is_cancelled() is False


# ------------------------------------------------------- provider streaming


def test_cancel_during_provider_streaming_returns_promptly_with_partial_text():
    backend = ProviderChatBackend(_StreamingProvider(), "fake-model")
    event = threading.Event()
    holder: dict = {}

    def work():
        try:
            backend.chat_generate(
                ChatRequest(messages=[ChatMessage(role="user", content="hi")]),
                cancel_event=event,
            )
        except RequestCancelledError as exc:
            holder["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    time.sleep(0.05)
    began = time.monotonic()
    event.set()
    thread.join(2)
    latency = time.monotonic() - began

    assert thread.is_alive() is False
    assert latency < 0.5, f"stream cancel took {latency:.2f}s"
    assert isinstance(holder.get("error"), RequestCancelledError)
    assert "partial answer" in holder["error"].partial_text


def test_cancel_before_a_provider_stream_sends_nothing():
    backend = ProviderChatBackend(_StreamingProvider(), "fake-model")
    event = threading.Event()
    event.set()
    began = time.monotonic()
    try:
        backend.chat_generate(
            ChatRequest(messages=[ChatMessage(role="user", content="hi")]),
            cancel_event=event,
        )
    except RequestCancelledError:
        pass
    else:  # pragma: no cover - the refusal is the behaviour under test
        raise AssertionError("a pre-cancelled request must not run")
    assert time.monotonic() - began < 0.5


# --------------------------------------------------------- tool execution


def test_cancel_during_tool_execution_stops_later_tools(tmp_path):
    manager = TaskManager()
    loop = _loop(tmp_path, manager, _StaticPlanner(_plan("src/a.py", "src/b.py")))
    executed: list[str] = []
    started = threading.Event()

    def slow_step(step):
        executed.append(step.step_id)
        started.set()
        time.sleep(0.3)
        return ActionResult(
            action_type=step.action_type,
            target_path=step.target_path,
            success=True,
            message="ok",
        )

    loop._execute_step = slow_step  # noqa: SLF001 - test seam
    thread, holder = _start(
        loop, TaskRequest(task_id="tools", description="do work", max_duration=300)
    )
    assert started.wait(5)
    manager.cancel()
    thread.join(2)

    assert thread.is_alive() is False
    assert holder["result"].status == ExecutionPhase.CANCELLED
    assert executed == ["step-1"], "a later tool ran after the task was stopped"
    assert not (tmp_path / "src" / "b.py").exists()


# ------------------------------------------------- completion-time races


def test_cancel_immediately_before_completion_is_never_reported_complete(tmp_path):
    manager = TaskManager()
    loop = _loop(tmp_path, manager, _StaticPlanner(_plan("src/a.py")))
    loop._verifier = _OKVerifier()  # noqa: SLF001 - test seam
    loop._settings = _Settings()  # noqa: SLF001 - test seam
    started = threading.Event()
    loop._packaging_builder = _SlowPackaging(started)  # noqa: SLF001 - test seam

    thread, holder = _start(
        loop, TaskRequest(task_id="late-cancel", description="do work", max_duration=300)
    )
    assert started.wait(5)
    manager.cancel()
    thread.join(2)

    assert thread.is_alive() is False
    assert holder["result"].status == ExecutionPhase.CANCELLED
    assert holder["result"].packaging is not None, "work already done was discarded"
    assert manager.get_last_task().status == TaskStatus.CANCELLED


def test_cancel_that_races_a_provider_failure_is_reported_as_cancelled():
    class _FailingProvider(_StreamingProvider):
        def stream_chat(self, _model, _messages, **kwargs):
            stop = kwargs["should_stop"]
            while not stop():
                time.sleep(0.005)
            from giscode.providers.base import ProviderError

            raise ProviderError("the model crashed")

    backend = ProviderChatBackend(_FailingProvider(), "fake-model")
    event = threading.Event()
    holder: dict = {}

    def work():
        try:
            backend.chat_generate(
                ChatRequest(messages=[ChatMessage(role="user", content="hi")]),
                cancel_event=event,
            )
        except RequestCancelledError as exc:
            holder["cancelled"] = exc
        except Exception as exc:  # noqa: BLE001 - the failure mode under test
            holder["failed"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    time.sleep(0.05)
    event.set()
    thread.join(2)

    assert isinstance(holder.get("cancelled"), RequestCancelledError)
    assert "failed" not in holder


# ------------------------------------------------------------- recovery


def test_a_new_task_runs_after_a_cancelled_one(tmp_path):
    manager = TaskManager()
    started = threading.Event()
    loop = _loop(tmp_path, manager, _BlockingPlanner(started))
    thread, holder = _start(
        loop,
        TaskRequest(task_id="first", description="do work", max_duration=300),
    )
    assert started.wait(5)
    manager.cancel()
    thread.join(2)
    assert holder["result"].status == ExecutionPhase.CANCELLED

    loop._planner = _StaticPlanner(_plan("src/after.py"))  # noqa: SLF001
    loop._verifier = _OKVerifier()  # noqa: SLF001
    result = loop.start(
        TaskRequest(task_id="second", description="do work", max_duration=60)
    )

    assert result.status == ExecutionPhase.COMPLETE
    assert manager.get_last_task().status == TaskStatus.COMPLETED
    assert manager.get_active_task() is None
