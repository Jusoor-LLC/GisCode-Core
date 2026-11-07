import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from giscode.agentic.actions import WorkspaceActionExecutor
from giscode.agentic.write_safety import SnapshotManager
from giscode.agentic.planner import Planner, PlanningError
from giscode.agentic.rooting import snapshot_profile_root
from giscode.agentic.types import (
    ActionType,
    ActionResult,
    ExecutionPhase,
    ExecutionPlan,
    LoopState,
    TaskRequest,
    TaskResult,
    VerificationResult,
)
from giscode.agentic.verifier import Verifier
from giscode.inference.api import InferenceAPI
from giscode.inference.runtime import GenerationRequest
from giscode.knowledge.service import KnowledgeService
from giscode.ocr.service import OCRService
from giscode.openhands.adapter import OpenHandsAdapter
from giscode.openhands.types import OpenHandsPhase, OpenHandsRequest, OpenHandsResult
from giscode.packaging.builder import PackagingBuilder, SigningService
from giscode.packaging.config import PackageStatus, SigningStatus
from giscode.routing.service import RoutingService
from giscode.sandbox.executor import SandboxExecutor
from giscode.sandbox.types import SandboxRequest, SandboxStatus
from giscode.self_improvement.service import SelfImprovementService
from giscode.task_control.manager import TaskManager
from giscode.task_control.types import TaskPhase, TaskStatus
from giscode.terminal.executor import CommandExecutor, CommandRequest, CommandStatus
from giscode.research.service import ResearchService
from giscode.research.types import ResearchRequest, ResearchStatus
from giscode.workspace.workspace import Workspace


# Reported instead of crashing when the loop has no task manager to report
# through. Names the missing object and states that nothing was changed.
MISSING_TASK_MANAGER_MESSAGE = (
    "internal error: task manager is not available, no changes made"
)

# Reported when the shared task manager already has an active task. The loop
# must not adopt work it did not start: that task's state, budget and rollback
# records belong to whoever launched it.
BUSY_TASK_MANAGER_MESSAGE = (
    "another task is already running in the shared task manager; this task was "
    "not started and no changes were made"
)


class CodingAgentLoop:
    def __init__(
        self,
        workspace: Workspace,
        inference: InferenceAPI,
        task_manager: TaskManager,
        planner: Optional[Planner] = None,
        terminal_executor: Optional[CommandExecutor] = None,
        sandbox_executor: Optional[SandboxExecutor] = None,
        openhands_adapter: Optional[OpenHandsAdapter] = None,
        knowledge_service: Optional[KnowledgeService] = None,
        routing_service: Optional[RoutingService] = None,
        ocr_service: Optional[OCRService] = None,
        self_improvement_service: Optional[SelfImprovementService] = None,
        research_service: Optional[ResearchService] = None,
        packaging_builder: Optional[PackagingBuilder] = None,
        signing_service: Optional[SigningService] = None,
        settings: Optional[object] = None,
        approval_callback=None,
    ):
        self._workspace = workspace
        self._inference = inference
        self._task_manager = task_manager
        # Which collaborators this loop created itself. A per-run re-root may
        # rebuild only its own; an injected planner or sandbox executor belongs
        # to the caller and is left exactly as handed over.
        self._planner_is_default = planner is None
        self._sandbox_executor_is_default = sandbox_executor is None
        if planner is None:
            planner = Planner(
                inference=inference,
                workspace_root=workspace.root_path,
                reasoning_budget_fn=self._reasoning_budget,
            )
        self._planner = planner
        self._executor = WorkspaceActionExecutor(workspace)
        self._terminal_executor = terminal_executor or CommandExecutor(workspace)
        self._sandbox_executor = sandbox_executor or SandboxExecutor(workspace)
        self._verifier = Verifier(workspace.root_path, terminal_executor=self._terminal_executor)
        self._openhands_adapter = openhands_adapter
        self._knowledge_service = knowledge_service
        self._routing_service = routing_service
        self._ocr_service = ocr_service
        self._self_improvement_service = self_improvement_service
        self._research_service = research_service
        self._packaging_builder = packaging_builder
        self._signing_service = signing_service
        self._settings = settings
        self._approval_callback = approval_callback
        self._snapshot_manager: Optional[SnapshotManager] = None
        self._current_result: Optional[TaskResult] = None
        self._cancel_event = threading.Event()
        # Planning model kept resident for the rest of the task; released by
        # implementation only when a DIFFERENT coding model is selected.
        self._planning_model_to_release: Optional[str] = None

    @property
    def workspace_root(self) -> Path:
        """The folder this loop's edits, commands and verifier are rooted at."""
        return self._workspace.root_path

    def changed_files_for(self, task_id: str) -> list[str]:
        """Project-relative files this task actually wrote, when known.

        Empty is a real answer here: a build that ran and produced no file is
        reported as "completed with no changes" instead of plain completion.
        """
        snapshot = self.snapshot_for(task_id)
        if snapshot is None:
            return []
        try:
            return [relative for relative, _digest, _created in snapshot.written_files()]
        except Exception:  # noqa: BLE001 - a missing record is not a crash
            return []

    def snapshot_for(self, task_id: str) -> Optional[SnapshotManager]:
        """The snapshot manager this loop used for *task_id*, if it ran it.

        The manager holds the pre-edit bytes of every file the task wrote, so
        the caller that decides a change must be undone (the GisAI engine,
        after requested tests fail) reads it through here instead of the
        loop's private attribute. A snapshot that belongs to a different task
        is never returned.
        """
        snapshot = self._snapshot_manager
        if snapshot is None:
            return None
        try:
            if Path(snapshot.root).name != str(task_id):
                return None
        except (AttributeError, TypeError):
            return None
        return snapshot

    def set_edit_approval_callback(self, callback) -> None:
        """Bind the host's Apply/Reject callback for existing-file writes."""
        self._approval_callback = callback
        if self._snapshot_manager is not None:
            self._executor.configure_write_safety(
                self._snapshot_manager,
                self._approval_for_write,
            )

    def _approval_for_write(self, path, diff) -> bool:
        callback = self._approval_callback
        if not callable(callback):
            return False
        pause = getattr(self._task_manager, "pause_timeout", None)
        resume = getattr(self._task_manager, "resume_timeout", None)
        if callable(pause):
            pause()
        try:
            return bool(callback(path, diff))
        finally:
            if callable(resume):
                resume()

    def _reasoning_budget(self) -> str:
        """Thinking mode for the planner, from settings (default ``off``)."""
        settings = self._settings
        value = None
        getter = getattr(settings, "get", None)
        if callable(getter):
            try:
                value = getter("model.reasoning_budget", "off")
            except Exception:
                value = None
        value = str(value or "off").strip().lower()
        return value if value in ("off", "low", "default") else "off"

    def execute_command(self, request) -> ActionResult:
        if hasattr(request, "command"):
            cmd_request = request
        else:
            cmd_request = CommandRequest(
                command=request.get("command", ""),
                arguments=request.get("arguments", []),
                working_directory=request.get("working_directory"),
                timeout=request.get("timeout"),
            )
        result = self._terminal_executor.execute(cmd_request)
        return ActionResult(
            action_type="command",
            target_path=result.working_directory,
            success=result.status == CommandStatus.COMPLETED,
            message=f"Command finished with exit code {result.exit_code}",
            error=result.error,
        )

    def execute_in_sandbox(self, request) -> ActionResult:
        if hasattr(request, "command"):
            sandbox_request = request
        else:
            sandbox_request = SandboxRequest(
                command=request.get("command", ""),
                arguments=request.get("arguments", []),
                working_directory=request.get("working_directory", "/workspace"),
                timeout=request.get("timeout"),
            )
        result = self._sandbox_executor.execute(sandbox_request)
        return ActionResult(
            action_type="sandbox",
            target_path=result.working_directory,
            success=result.status == SandboxStatus.COMPLETED,
            message=f"Sandbox command finished with exit code {result.exit_code}",
            error=result.error,
        )

    def submit(self, request: TaskRequest) -> TaskResult:
        self._cancel_event.clear()
        self._current_result = TaskResult(
            task_id=request.task_id,
            status=ExecutionPhase.INTAKE,
        )
        return self._current_result

    def start(
        self,
        request: TaskRequest,
        *,
        root_path: Optional[str] = None,
    ) -> TaskResult:
        """Run one task, optionally rooted at *root_path* for this run only.

        ``root_path`` re-roots the planner, action executor (and the guarded
        writer it builds), command executor, sandbox executor, verifier and
        snapshot for the duration of the call. Everything is restored in
        ``finally``, so the caller's own workspace and any later task are left
        exactly as they were.
        """
        if self._task_manager is None:
            # Every phase of this loop reports through the task manager. Rather
            # than dereferencing None deep inside a handler - where the
            # resulting AttributeError replaces the real error and reads as a
            # bare crash - fail once, up front, with a message that says what
            # was missing. No work is attempted, so nothing is changed.
            result = TaskResult(
                task_id=request.task_id,
                status=ExecutionPhase.FAILED,
            )
            result.error = MISSING_TASK_MANAGER_MESSAGE
            result.response = MISSING_TASK_MANAGER_MESSAGE
            result.failure_info = {
                "reason": "missing_task_manager",
                "message": MISSING_TASK_MANAGER_MESSAGE,
            }
            return result

        active = self._task_manager.get_active_task()
        if active is not None:
            # Do not adopt someone else's task: its budget, cancellation and
            # rollback records belong to whoever started it.
            return self._refuse_busy(request, active)

        result = self.submit(request)
        saved_rooting = None
        if root_path is not None:
            saved_rooting = self._capture_rooting()
            try:
                self._root_at(Path(root_path))
            except Exception as exc:  # noqa: BLE001 - refuse, never half-root
                self._restore_rooting(saved_rooting)
                return self._refuse_rooting(request, root_path, exc)
        try:
            return self._run_started_task(request, result)
        finally:
            if saved_rooting is not None:
                self._restore_rooting(saved_rooting)

    def _refuse_busy(self, request: TaskRequest, active) -> TaskResult:
        """Return the busy verdict without touching task, snapshot or root state."""
        active_id = getattr(active, "task_id", None)
        result = TaskResult(
            task_id=request.task_id,
            status=ExecutionPhase.FAILED,
        )
        result.error = BUSY_TASK_MANAGER_MESSAGE
        result.response = BUSY_TASK_MANAGER_MESSAGE
        result.failure_info = {
            "reason": "task_manager_busy",
            "active_task_id": active_id,
            "message": BUSY_TASK_MANAGER_MESSAGE,
        }
        return result

    def _refuse_rooting(
        self,
        request: TaskRequest,
        root_path: str,
        exc: BaseException,
    ) -> TaskResult:
        """Return a failure for an unusable root; no task, no edits."""
        message = (
            f"the task could not be rooted at {root_path}: "
            f"{type(exc).__name__}: {exc}. Nothing was started and no files "
            f"were changed."
        )
        result = TaskResult(
            task_id=request.task_id,
            status=ExecutionPhase.FAILED,
        )
        result.error = message
        result.response = message
        result.failure_info = {
            "reason": "rooting_failed",
            "root_path": str(root_path),
            "message": str(exc),
        }
        return result

    # Attributes that a per-run re-root replaces. Anything injected by the
    # caller (a custom planner, a sandbox executor) is only replaced when this
    # loop created it, so ownership stays with the caller.
    _ROOTED_ATTRIBUTES = (
        "_workspace",
        "_planner",
        "_executor",
        "_terminal_executor",
        "_sandbox_executor",
        "_verifier",
    )

    def _capture_rooting(self) -> dict:
        return {name: getattr(self, name) for name in self._ROOTED_ATTRIBUTES}

    def _restore_rooting(self, saved: dict) -> None:
        for name, value in saved.items():
            setattr(self, name, value)

    def _root_at(self, root: Path) -> None:
        """Rebuild every rooted collaborator for a run at *root*.

        ``ensure_structure=False`` matters: re-rooting must not create the
        ``src/tests/docs`` scaffold inside somebody's real project.
        """
        workspace = Workspace(root, ensure_structure=False)
        if self._planner_is_default:
            self._planner = Planner(
                inference=self._inference,
                workspace_root=workspace.root_path,
                reasoning_budget_fn=self._reasoning_budget,
            )
        self._workspace = workspace
        self._executor = WorkspaceActionExecutor(workspace)
        self._terminal_executor = CommandExecutor(workspace)
        if self._sandbox_executor_is_default:
            self._sandbox_executor = SandboxExecutor(workspace)
        self._verifier = Verifier(
            workspace.root_path,
            terminal_executor=self._terminal_executor,
        )

    def _run_started_task(
        self, request: TaskRequest, result: TaskResult
    ) -> TaskResult:
        try:
            self._task_manager.start_task(
                request.task_id,
                max_duration=request.max_duration,
                completion_criteria=request.completion_criteria,
            )
            criteria = (
                request.completion_criteria
                if isinstance(request.completion_criteria, dict)
                else {}
            )
            self._snapshot_manager = SnapshotManager(
                request.task_id,
                profile_root=snapshot_profile_root(criteria.get("project_id")),
            )
            self._executor.configure_write_safety(
                self._snapshot_manager,
                self._approval_for_write,
            )
            self._task_manager.update_phase(TaskPhase.ANALYZING)

            result = self._run_intake(request, result)
            if self._is_cancelled(result):
                return self._finalize(result, request)

            result = self._run_planning(request, result)
            if result.status in (ExecutionPhase.FAILED, ExecutionPhase.CANCELLED):
                return self._finalize(result, request)
            if (
                result.plan is not None
                and not result.plan.steps
                and (request.description or "").strip()
            ):
                result.status = ExecutionPhase.FAILED
                result.error = (
                    "planner produced no executable steps for a "
                    "non-empty coding request"
                )
                result.failure_info = {
                    "reason": "empty_plan",
                    "description": request.description,
                }
                self._task_manager.complete(
                    success=False, failure_info=result.failure_info
                )
                return self._finalize(result, request)

            if self._is_cancelled(result):
                return self._finalize(result, request)

            result = self._run_implementation(result)
            if result.status in (ExecutionPhase.FAILED, ExecutionPhase.CANCELLED, ExecutionPhase.TIMED_OUT):
                return self._finalize(result, request)

            result = self._run_verification(result, request.description)
            if result.status in (ExecutionPhase.FAILED, ExecutionPhase.CANCELLED, ExecutionPhase.TIMED_OUT):
                return self._finalize(result, request)

            if result.verification is not None and not result.verification.success:
                result = self._run_repair_loop(result, request)
                if result.status in (ExecutionPhase.FAILED, ExecutionPhase.CANCELLED, ExecutionPhase.TIMED_OUT):
                    return self._finalize(result, request)

            packaging_result = self._run_packaging()
            result.packaging = packaging_result

            result.status = ExecutionPhase.COMPLETE
            self._complete_task(success=True)
        except Exception as e:
            result.status = ExecutionPhase.FAILED
            result.error = str(e)
            result.failure_info = {"reason": "exception", "message": str(e)}
            self._complete_task(success=False, failure_info=result.failure_info)

        return self._finalize(result, request)

    def _complete_task(self, *, success: bool, failure_info: Optional[dict] = None) -> None:
        """Finish the tracked task without ever masking the real outcome.

        Reporting completion is bookkeeping: if there is no task manager, or
        reporting itself raises, the caller's result (including a genuine
        failure reason) must survive instead of being replaced by an
        AttributeError from this line.
        """
        if self._task_manager is None:
            return
        try:
            if success:
                self._task_manager.complete(success=True)
            else:
                self._task_manager.complete(success=False, failure_info=failure_info)
        except Exception:  # noqa: BLE001 - bookkeeping must not replace the result
            pass

    def _finalize(self, result: TaskResult, request: TaskRequest) -> TaskResult:
        """Generate the final textual response and return."""
        self._rollback_failed_task(result)
        try:
            result.response = self._generate_response(request, result)
        except Exception:
            result.response = None
        self._current_result = result
        return result

    def _rollback_failed_task(self, result: TaskResult) -> None:
        if result.status not in (
            ExecutionPhase.FAILED,
            ExecutionPhase.CANCELLED,
            ExecutionPhase.TIMED_OUT,
        ):
            return
        snapshot = self._snapshot_manager
        if snapshot is None:
            return
        # Restore only what is still this task's own output. A file the user
        # saved after the task wrote it is reported as a conflict and kept.
        from giscode.agentic.rollback import rollback_task_edits

        try:
            outcome = rollback_task_edits(snapshot, self._workspace.root_path)
        except Exception as exc:  # noqa: BLE001 - a skipped undo is not silence
            info = result.failure_info if isinstance(result.failure_info, dict) else {}
            info["rollback_blocked"] = f"{type(exc).__name__}: {exc}"
            result.failure_info = info
            result.error = (
                f"{result.error or 'task failed'}; could not roll back: "
                f"{type(exc).__name__}: {exc}"
            )
            return
        info = result.failure_info if isinstance(result.failure_info, dict) else {}
        if outcome.conflicts:
            info["rollback_conflicts"] = [
                {"path": path, "reason": reason}
                for path, reason in outcome.conflicts
            ]
        if not outcome.touched:
            # Nothing could be undone (every file was left alone). Reporting an
            # undo that never happened would be worse than saying so - but the
            # files and the reasons still have to reach the user.
            if outcome.conflicts:
                info["rollback_remaining"] = outcome.kept
                detail = "; ".join(
                    f"{path} ({reason})" for path, reason in outcome.conflicts
                )
                note = f"task edits left in place, not undone: {detail}"
                result.error = f"{result.error or 'task failed'}; {note}"
            elif outcome.blocked:
                # A rollback that could not even run is not "no edits to undo".
                info["rollback_blocked"] = outcome.blocked
                note = f"could not roll back: {outcome.blocked}"
                result.error = f"{result.error or 'task failed'}; {note}"
            if info:
                result.failure_info = info
            return
        info["rolled_back"] = True
        info["rolled_back_files"] = outcome.restored + outcome.removed
        if outcome.discarded:
            # The undo stands; the content it dropped was kept first, so the
            # user can put a specific edit back deliberately.
            info["discarded_edits"] = list(outcome.discarded)
            info["recovery_patch_dir"] = outcome.patch_dir
            result.error = f"{result.error or 'task failed'}; {outcome.discarded_report()}"
        if outcome.conflicts:
            info["partial_rollback"] = True
            info["rollback_remaining"] = outcome.kept
        result.failure_info = info
        note = f"edits rolled back: {', '.join(outcome.restored + outcome.removed)}"
        if outcome.conflicts:
            kept = ", ".join(
                f"{path} ({reason})" for path, reason in outcome.conflicts
            )
            note += f"; left in place after a conflict: {kept}"
        result.error = f"{result.error or 'task failed'}; {note}"

    def _generate_response(self, request: TaskRequest, result: TaskResult) -> Optional[str]:
        if self._inference is None:
            return None
        try:
            health = self._inference.get_health()
            if not health.get("model_loaded"):
                model_state = health.get("model_state")
                if model_state == "error":
                    model_status = self._inference.get_model_status()
                    if model_status is not None and getattr(model_status, "error_message", None):
                        result.inference_error = model_status.error_message
                return None
            prompt = request.description or "Respond to the user."
            gen_request = GenerationRequest(prompt=prompt, max_tokens=256, temperature=0.7)
            response = self._inference.generate(gen_request)
            text = response.text.strip() if response.text else ""
            if text:
                return text
            result.inference_error = "Inference runtime returned no text"
            return None
        except Exception as e:
            result.inference_error = f"{type(e).__name__}: {e}"
            return None

    def _is_cancelled(self, result: TaskResult) -> bool:
        if self._cancel_event.is_set():
            result.status = ExecutionPhase.CANCELLED
            self._task_manager.cancel()
            return True
        if result.status in (ExecutionPhase.FAILED, ExecutionPhase.CANCELLED, ExecutionPhase.TIMED_OUT):
            return False
        active = self._task_manager.get_active_task()
        if active is None or active.status.value == "timeout":
            result.status = ExecutionPhase.TIMED_OUT
            result.failure_info = {"reason": "timeout"}
            return True
        return False

    def _preferred_routing_model(self) -> Optional[str]:
        """The model GisAi is currently delegating to: the running real model."""
        try:
            current = self._inference.get_model_status()
        except Exception:
            return None
        if current is None:
            return None
        return getattr(current, "name", None)

    def _run_intake(self, request: TaskRequest, result: TaskResult) -> TaskResult:
        result.status = ExecutionPhase.INTAKE
        self._task_manager.update_phase(TaskPhase.ANALYZING)
        return result

    def _run_planning(self, request: TaskRequest, result: TaskResult) -> TaskResult:
        result.status = ExecutionPhase.PLANNING
        self._task_manager.update_phase(TaskPhase.ANALYZING)
        if self._is_cancelled(result):
            return result

        planning_model_loaded = False
        planning_model_name = None

        if self._routing_service is not None:
            try:
                from giscode.routing.types import ModelAvailability, ModelRole, RoutingRequest
                routing_request = RoutingRequest(
                    task_id=request.task_id,
                    task_type="planning",
                    requested_role=ModelRole.PLANNING,
                    task_description=request.description,
                    preferred_model=self._preferred_routing_model(),
                )
                routing_result, _ = self._routing_service.route_and_assemble(routing_request)

                if (
                    routing_result.availability == ModelAvailability.UNAVAILABLE
                    or not routing_result.selected_model
                ):
                    result.status = ExecutionPhase.FAILED
                    result.error = "planning failure: no planning model available"
                    result.failure_info = {"reason": "planning_model_unavailable"}
                    self._task_manager.complete(
                        success=False, failure_info=result.failure_info
                    )
                    return result

                planning_model_name = routing_result.selected_model
                model_manager = self._routing_service._model_manager
                metadata = model_manager.get_model_status(planning_model_name)
                current = self._inference.get_model_status()
                if metadata is not None and (
                    current is None or current.path != str(metadata.path)
                ):
                    model_manager.load_model(planning_model_name)
                    planning_model_loaded = True
            except Exception as exc:
                result.status = ExecutionPhase.FAILED
                result.error = f"planning failure: model load failed: {exc}"
                result.failure_info = {
                    "reason": "planning_model_load_failure",
                    "message": str(exc),
                }
                self._task_manager.complete(
                    success=False, failure_info=result.failure_info
                )
                return result

        try:
            try:
                plan = self._planner.create_plan(request)
            except PlanningError as exc:
                # Translate a planning failure into a clean FAILED
                # status. Without this, the planner's empty / null
                # response would silently fall through and the loop
                # would later report COMPLETE with verification
                # vacuously succeeding.
                result.status = ExecutionPhase.FAILED
                result.error = f"planner failure: {exc}"
                result.failure_info = {
                    "reason": "planning_failure",
                    "message": str(exc),
                }
                self._task_manager.complete(
                    success=False, failure_info=result.failure_info
                )
                return result
            result.plan = plan
            return result
        finally:
            if planning_model_loaded and planning_model_name and self._routing_service is not None:
                if result.plan is None:
                    # Planning failed: nothing later in this task needs the
                    # model, so free it now.
                    try:
                        self._routing_service._model_manager.unload_model(planning_model_name)
                    except Exception:
                        pass
                else:
                    # Planning succeeded. Keep the model resident for the rest
                    # of the task: unloading it here and reloading it for
                    # implementation is a mid-task reset. With a single local
                    # model the reload can fail or be skipped, and every later
                    # step - repair planning included - then runs with
                    # "[No model loaded]", which is the J8 symptom. Implementation
                    # releases this model only when it selects a different coding
                    # model, so a two-model setup still frees the planning model
                    # before the coding model loads.
                    self._planning_model_to_release = planning_model_name

    def _run_implementation(self, result: TaskResult) -> TaskResult:
        if self._is_cancelled(result):
            return result
        result.status = ExecutionPhase.IMPLEMENTATION
        self._task_manager.update_phase(TaskPhase.CODING)

        if result.plan is None:
            result.status = ExecutionPhase.FAILED
            result.error = "No execution plan"
            return result

        coding_model_loaded = False
        coding_model_name = None

        if self._routing_service is not None:
            try:
                from giscode.routing.types import ModelAvailability, ModelRole, RoutingRequest
                routing_request = RoutingRequest(
                    task_id=result.task_id,
                    task_type="implementation",
                    requested_role=ModelRole.CODING,
                    task_description=result.plan.objective if result.plan else None,
                    preferred_model=self._preferred_routing_model(),
                )
                routing_result, _ = self._routing_service.route_and_assemble(routing_request)

                if (
                    routing_result.availability == ModelAvailability.UNAVAILABLE
                    or not routing_result.selected_model
                ):
                    result.status = ExecutionPhase.FAILED
                    result.error = "implementation failure: no coding model available"
                    result.failure_info = {"reason": "coding_model_unavailable"}
                    self._task_manager.complete(
                        success=False, failure_info=result.failure_info
                    )
                    return result

                coding_model_name = routing_result.selected_model
                model_manager = self._routing_service._model_manager
                # Release the planning model first when implementation selected a
                # DIFFERENT model, so its memory is freed before the coding model
                # loads. Same model: keep it loaded - unloading and reloading it
                # here is the mid-task reset that leaves repair planning with no
                # model at all.
                pending = self._planning_model_to_release
                self._planning_model_to_release = None
                if pending and pending != coding_model_name:
                    try:
                        model_manager.unload_model(pending)
                    except Exception:
                        pass
                metadata = model_manager.get_model_status(coding_model_name)
                current = self._inference.get_model_status()
                already_resident = current is not None and (
                    metadata is None or current.path == str(metadata.path)
                )
                if not already_resident:
                    model_manager.load_model(coding_model_name)
                    coding_model_loaded = True
            except Exception as exc:
                result.status = ExecutionPhase.FAILED
                result.error = f"implementation failure: model load failed: {exc}"
                result.failure_info = {
                    "reason": "coding_model_load_failure",
                    "message": str(exc),
                }
                self._task_manager.complete(
                    success=False, failure_info=result.failure_info
                )
                return result

        for step in result.plan.steps:
            if self._is_cancelled(result):
                return result

            time.sleep(0.01)
            action_result = self._execute_step(step)
            result.actions.append(action_result)

            if not action_result.success:
                result.status = ExecutionPhase.FAILED
                result.error = action_result.error
                result.failure_info = {"reason": "action_failed", "step": step.step_id, "error": action_result.error}
                self._task_manager.complete(success=False, failure_info=result.failure_info)
                return result

        return result

    def _run_verification(self, result: TaskResult, request_text: str = "") -> TaskResult:
        result.status = ExecutionPhase.VERIFICATION
        self._task_manager.update_phase(TaskPhase.VERIFYING)

        if self._is_cancelled(result):
            return result

        if result.plan is None:
            result.status = ExecutionPhase.FAILED
            result.error = "No execution plan for verification"
            return result

        verification = self._verifier.verify(
            result.plan,
            result.actions,
            request_text=request_text,
        )
        result.verification = verification

        if not verification.success:
            result.error = "; ".join(verification.errors)
            result.failure_info = {"reason": "verification_failed", "errors": verification.errors}

        return result

    def _build_repair_request(
        self, request: TaskRequest, result: TaskResult
    ) -> TaskRequest:
        verification = result.verification or VerificationResult(
            success=False, errors=[], details=[]
        )
        errors_block = "\n".join(f"- {e}" for e in verification.errors)
        details_block = "\n".join(f"- {d}" for d in verification.details)
        expected = result.plan.files_expected_to_change if result.plan else []
        expected_block = "\n".join(f"- {p}" for p in expected)

        # Identify implementation files vs test files to give the model
        # better context about what likely needs repair.
        impl_files = []
        test_files = []
        for path in expected:
            if path.startswith("tests/") or path.startswith("test_") or "/tests/" in path:
                test_files.append(path)
            else:
                impl_files.append(path)

        impl_block = "\n".join(f"- {p}" for p in impl_files) or "- (none identified)"
        test_block = "\n".join(f"- {p}" for p in test_files) or "- (none identified)"

        repair_description = (
            "AUTONOMOUS REPAIR REQUEST\n\n"
            "The original task failed verification. You must diagnose the failure "
            "and produce a corrective execution plan. Do NOT simply re-run the same plan.\n\n"
            "--- ORIGINAL USER REQUIREMENT ---\n"
            f"{request.description.strip()}\n\n"
            "--- EXPECTED BEHAVIOR ---\n"
            "The generated code and tests must satisfy the original requirement above.\n\n"
            "--- ACTUAL OBSERVED BEHAVIOR ---\n"
            f"{details_block}\n\n"
            "--- VERIFICATION FAILURES ---\n"
            f"{errors_block}\n\n"
            "--- FILES EXPECTED FROM ORIGINAL PLAN ---\n"
            f"{expected_block}\n\n"
            "--- LIKELY IMPLEMENTATION FILES ---\n"
            f"{impl_block}\n"
            "--- LIKELY TEST FILES ---\n"
            f"{test_block}\n\n"
            "REPAIR PROTOCOL:\n"
            "1. Inspect the relevant implementation file(s) before modifying them.\n"
            "2. Do NOT modify or weaken tests merely to make them pass.\n"
            "3. If the implementation violates the original requirement, fix the implementation.\n"
            "4. Use the evidence from the runtime failure (expected vs actual) rather than guessing.\n"
            "5. After repair, verification will be rerun automatically and must resolve the reported failure.\n\n"
            "First, briefly explain your diagnosis and the intended corrective action. "
            "Then produce JSON only with create/modify/read steps."
        )
        return TaskRequest(
            task_id=f"{request.task_id}-repair",
            description=repair_description,
            max_duration=request.max_duration,
            completion_criteria=request.completion_criteria,
        )

    def _is_test_path(self, path: str) -> bool:
        normalized = path.lower().replace("\\", "/")
        return (
            normalized.startswith("tests/")
            or normalized.startswith("test_")
            or "/tests/" in normalized
            or normalized.endswith("_test.py")
        )

    def _is_likely_implementation_path(self, path: str, original_expected_files: list[str]) -> bool:
        if self._is_test_path(path):
            return False
        normalized = path.lower().replace("\\", "/")
        if normalized.startswith("src/") or normalized.startswith("src\\"):
            return True
        if original_expected_files and path in original_expected_files:
            return True
        return False

    def _repair_plan_looks_suspicious(
        self,
        repair_plan: ExecutionPlan,
        original_expected_files: list[str],
        verification: VerificationResult,
    ) -> tuple[bool, str]:
        if repair_plan is None or not repair_plan.steps:
            return False, ""

        repair_paths = [
            step.target_path
            for step in repair_plan.steps
            if step.action_type.value != "read"
        ]
        if not repair_paths:
            return False, ""

        original_impl_files = [
            f for f in original_expected_files
            if self._is_likely_implementation_path(f, original_expected_files)
        ]
        if not original_impl_files:
            return False, ""

        has_test_modification = any(self._is_test_path(p) for p in repair_paths)
        has_impl_modification = any(
            self._is_likely_implementation_path(p, original_expected_files)
            for p in repair_paths
        )

        if has_impl_modification:
            return False, ""

        if not has_test_modification:
            return False, ""

        evidence_text = " ".join(verification.errors + verification.details).lower()

        test_problem_indicators = [
            "importerror: failed to import test module",
            "syntaxerror in test",
            "modulenotfounderror in test",
            "failed to import test module",
        ]
        has_test_problem_evidence = any(ind in evidence_text for ind in test_problem_indicators)
        if has_test_problem_evidence:
            return False, ""

        impl_problem_indicators = [
            "assertionerror",
            "expected ",
            " actual",
            "output mismatch",
            "returned",
            "printed",
        ]
        has_impl_problem_evidence = any(ind in evidence_text for ind in impl_problem_indicators)
        if has_impl_problem_evidence:
            return (
                True,
                "Repair plan modifies only test files, but verification evidence indicates "
                "an implementation behavior failure (expected vs actual mismatch). "
                "The implementation should be corrected, not the test.",
            )

        return False, ""

    def _build_corrective_replan_request(
        self,
        request: TaskRequest,
        result: TaskResult,
        repair_plan: ExecutionPlan,
        validation_reason: str,
    ) -> TaskRequest:
        verification = result.verification or VerificationResult(
            success=False, errors=[], details=[]
        )
        errors_block = "\n".join(f"- {e}" for e in verification.errors)
        details_block = "\n".join(f"- {d}" for d in verification.details)
        expected = result.plan.files_expected_to_change if result.plan else []
        expected_block = "\n".join(f"- {p}" for p in expected)

        plan_summary = []
        for step in repair_plan.steps:
            plan_summary.append(
                f"- {step.action_type.value}: {step.target_path}"
            )
        plan_block = "\n".join(plan_summary) or "- (empty)"

        repair_description = (
            "AUTONOMOUS REPAIR - CORRECTIVE RE-PLAN REQUEST\n\n"
            "A previous repair attempt was REJECTED because it did not address the "
            "underlying implementation failure. You must produce a corrected plan.\n\n"
            f"{validation_reason}\n\n"
            "--- ORIGINAL USER REQUIREMENT ---\n"
            f"{request.description.strip()}\n\n"
            "--- VERIFICATION FAILURES ---\n"
            f"{errors_block}\n\n"
            "--- VERIFICATION DETAILS ---\n"
            f"{details_block}\n\n"
            "--- FILES EXPECTED FROM ORIGINAL PLAN ---\n"
            f"{expected_block}\n\n"
            "--- REJECTED REPAIR PLAN ---\n"
            f"{plan_block}\n\n"
            "REPAIR PROTOCOL:\n"
            "1. Inspect the relevant implementation file(s) before modifying them.\n"
            "2. Do NOT modify or weaken tests merely to make them pass.\n"
            "3. If the implementation violates the original requirement, fix the implementation.\n"
            "4. Use the evidence from the runtime failure (expected vs actual) rather than guessing.\n"
            "5. After repair, verification will be rerun automatically and must resolve the reported failure.\n\n"
            "First, briefly explain your diagnosis and the intended corrective action. "
            "Then produce JSON only with create/modify/read steps."
        )
        return TaskRequest(
            task_id=f"{request.task_id}-repair-corrective",
            description=repair_description,
            max_duration=request.max_duration,
            completion_criteria=request.completion_criteria,
        )

    def _run_repair_loop(self, result: TaskResult, request: TaskRequest) -> TaskResult:
        if self._is_cancelled(result):
            return result
        if result.status in (ExecutionPhase.FAILED, ExecutionPhase.CANCELLED, ExecutionPhase.TIMED_OUT):
            return result

        coding_model_loaded = False
        coding_model_name = None

        if self._routing_service is not None:
            try:
                from giscode.routing.types import ModelAvailability, ModelRole, RoutingRequest
                routing_request = RoutingRequest(
                    task_id=request.task_id,
                    task_type="repair",
                    requested_role=ModelRole.CODING,
                    task_description=request.description,
                    preferred_model=self._preferred_routing_model(),
                )
                routing_result, _ = self._routing_service.route_and_assemble(routing_request)

                if (
                    routing_result.availability == ModelAvailability.UNAVAILABLE
                    or not routing_result.selected_model
                ):
                    result.status = ExecutionPhase.FAILED
                    result.error = "repair failure: no coding model available"
                    result.failure_info = {"reason": "coding_model_unavailable"}
                    self._task_manager.complete(
                        success=False, failure_info=result.failure_info
                    )
                    return result

                coding_model_name = routing_result.selected_model
                model_manager = self._routing_service._model_manager
                metadata = model_manager.get_model_status(coding_model_name)
                current = self._inference.get_model_status()
                already_resident = current is not None and (
                    metadata is None or current.path == str(metadata.path)
                )
                if not already_resident:
                    model_manager.load_model(coding_model_name)
                    coding_model_loaded = True
                else:
                    coding_model_loaded = False
            except Exception as exc:
                result.status = ExecutionPhase.FAILED
                result.error = f"repair failure: model load failed: {exc}"
                result.failure_info = {
                    "reason": "coding_model_load_failure",
                    "message": str(exc),
                }
                self._task_manager.complete(
                    success=False, failure_info=result.failure_info
                )
                return result

        max_repairs = 2
        repair_request = self._build_repair_request(request, result)

        for attempt in range(1, max_repairs + 1):
            if self._is_cancelled(result):
                return result

            try:
                repair_plan = self._planner.create_plan(repair_request)
            except PlanningError as exc:
                result.status = ExecutionPhase.FAILED
                result.error = f"repair planning failure: {exc}"
                result.failure_info = {
                    "reason": "repair_planning_failure",
                    "attempt": attempt,
                    "message": str(exc),
                }
                self._task_manager.complete(
                    success=False, failure_info=result.failure_info
                )
                return result

            original_expected = (
                result.plan.files_expected_to_change if result.plan else []
            )
            verification = result.verification or VerificationResult(
                success=False, errors=[], details=[]
            )

            suspicious, reason = self._repair_plan_looks_suspicious(
                repair_plan, original_expected, verification
            )
            if suspicious:
                corrective_request = self._build_corrective_replan_request(
                    request, result, repair_plan, reason
                )
                try:
                    repair_plan = self._planner.create_plan(corrective_request)
                except PlanningError:
                    corrective_failed = True
                else:
                    suspicious, reason = self._repair_plan_looks_suspicious(
                        repair_plan, original_expected, verification
                    )
                    if suspicious:
                        corrective_failed = True
                    else:
                        corrective_failed = False

                if corrective_failed:
                    repair_request = self._build_repair_request(request, result)
                    continue

            result.plan = repair_plan
            if self._is_cancelled(result):
                return result
            result = self._run_implementation(result)
            if result.status in (ExecutionPhase.FAILED, ExecutionPhase.CANCELLED, ExecutionPhase.TIMED_OUT):
                return result

            result = self._run_verification(result, request.description)
            if result.status in (ExecutionPhase.FAILED, ExecutionPhase.CANCELLED, ExecutionPhase.TIMED_OUT):
                return result

            if result.verification is not None and result.verification.success:
                result.error = None
                result.failure_info = None
                return result

            repair_request = self._build_repair_request(request, result)

        result.status = ExecutionPhase.FAILED
        verification = result.verification or VerificationResult(
            success=False, errors=[], details=[]
        )
        result.error = "; ".join(verification.errors) or "repair attempts exhausted"
        result.failure_info = {
            "reason": "repair_exhausted",
            "attempts": max_repairs,
            "errors": verification.errors,
        }
        self._task_manager.complete(success=False, failure_info=result.failure_info)
        return result

    def _run_packaging(self) -> Optional[dict]:
        if self._packaging_builder is None:
            return None

        settings = self._settings if hasattr(self, "_settings") and self._settings is not None else None
        if settings is None:
            try:
                from giscode.config.settings import SettingsManager
                settings = SettingsManager()
            except Exception:
                settings = None

        if settings is not None and not settings.get("packaging.enabled", False):
            return {
                "enabled": False,
                "status": "skipped",
                "message": "Packaging is not enabled in settings",
            }

        packaging_result: dict = {
            "enabled": True,
            "status": "pending",
        }

        try:
            build_result = self._packaging_builder.build()
            packaging_result.update(build_result)
        except Exception as exc:
            packaging_result.update({
                "status": "build_failed",
                "error": str(exc),
            })
            return packaging_result

        if self._signing_service is not None:
            try:
                package_path = build_result.get("package")
                if package_path:
                    signing_result = self._signing_service.sign(Path(package_path))
                    packaging_result["signing"] = signing_result
                else:
                    packaging_result["signing"] = {
                        "status": SigningStatus.UNSIGNED.value,
                        "message": "No package path available for signing",
                    }
            except Exception as exc:
                packaging_result["signing"] = {
                    "status": SigningStatus.VERIFICATION_FAILED.value,
                    "message": str(exc),
                }

        return packaging_result

    @staticmethod
    def _map_action_type_to_phase(action_type: ActionType) -> Optional[OpenHandsPhase]:
        mapping = {
            ActionType.CREATE: OpenHandsPhase.CODING,
            ActionType.MODIFY: OpenHandsPhase.CODING,
            ActionType.VERIFY: OpenHandsPhase.VERIFYING,
            ActionType.REPAIR: OpenHandsPhase.CODING,
            ActionType.READ: OpenHandsPhase.CODING,
        }
        return mapping.get(action_type)

    def _execute_step(self, step) -> ActionResult:
        if self._openhands_adapter is not None:
            command = step.action_type.value
            working_directory = step.target_path
            if step.action_type.value == "read":
                command = "read"
            request = OpenHandsRequest(
                task_id=step.step_id,
                command=command,
                arguments=[],
                working_directory=working_directory,
                input_data=step.content,
            )
            result = self._openhands_adapter.execute(request)
            result.phase = self._map_action_type_to_phase(step.action_type)
            return ActionResult(
                action_type=step.action_type,
                target_path=step.target_path,
                success=result.status.value == "completed",
                message=result.stdout,
                error=result.error,
            )
        return self._executor.execute(step)

    def retrieve_knowledge(
        self,
        query: str,
        namespace: Optional[str] = None,
        source_type: Optional[str] = None,
        top_k: int = 10,
    ) -> list[dict]:
        if self._knowledge_service is None:
            return []
        try:
            from giscode.knowledge.types import KnowledgeSourceType
            st = KnowledgeSourceType(source_type) if source_type else None
            results = self._knowledge_service.search(
                query=query,
                namespace=namespace,
                source_type=st,
                top_k=top_k,
            )
            return [
                {
                    "record_id": r.record.record_id,
                    "content": r.record.content,
                    "source": r.record.source,
                    "source_type": r.record.source_type.value,
                    "score": r.score,
                    "namespace": r.record.namespace,
                }
                for r in results
            ]
        except Exception:
            return []

    def route_and_assemble(
        self,
        task_id: str,
        task_type: str,
        namespace: Optional[str] = None,
        project_id: Optional[str] = None,
        task_description: Optional[str] = None,
        requested_role: Optional[str] = None,
        preferred_model: Optional[str] = None,
        fallback_policy: str = "strict",
    ) -> tuple[Optional[dict], Optional[dict]]:
        if self._routing_service is None:
            return None, None
        try:
            from giscode.routing.types import FallbackPolicy, ModelRole, RoutingRequest
            role = ModelRole(requested_role) if requested_role else None
            policy = FallbackPolicy(fallback_policy)
            request = RoutingRequest(
                task_id=task_id,
                task_type=task_type,
                requested_role=role,
                task_description=task_description,
                namespace=namespace,
                project_id=project_id,
                preferred_model=preferred_model,
                fallback_policy=policy,
            )
            routing_result, context_bundle = self._routing_service.route_and_assemble(request)
            return {
                "task_id": routing_result.task_id,
                "selected_role": routing_result.selected_role.value if routing_result.selected_role else None,
                "selected_model": routing_result.selected_model,
                "availability": routing_result.availability.value,
                "reason": routing_result.reason,
                "confidence": routing_result.confidence,
                "fallback_used": routing_result.fallback_used,
                "fallback_reason": routing_result.fallback_reason,
            }, {
                "task_id": context_bundle.task_id,
                "task_text": context_bundle.task_text,
                "selected_role": context_bundle.selected_role.value if context_bundle.selected_role else None,
                "selected_model": context_bundle.selected_model,
                "namespace": context_bundle.namespace,
                "project_id": context_bundle.project_id,
                "retrieved_records": context_bundle.retrieved_records,
                "formatted_context": context_bundle.formatted_context,
                "record_count": context_bundle.record_count,
                "truncation_occurred": context_bundle.truncation_occurred,
                "truncation_reason": context_bundle.truncation_reason,
                "context_char_count": context_bundle.context_char_count,
            }
        except Exception:
            return None, None

    def ocr(
        self,
        request_id: str,
        source_type: str,
        input_path: Optional[str] = None,
        namespace: str = "default",
        project_id: Optional[str] = None,
        language: str = "eng",
        task_id: Optional[str] = None,
    ) -> Optional[dict]:
        if self._ocr_service is None:
            return None
        try:
            from giscode.ocr.types import OCRRequest, OCRSourceType
            st = OCRSourceType(source_type)
            req = OCRRequest(
                request_id=request_id,
                source_type=st,
                input_path=input_path,
                namespace=namespace,
                project_id=project_id,
                language=language,
                task_id=task_id,
            )
            result = self._ocr_service.process(req)
            knowledge_record = None
            if result.status.value == "completed":
                knowledge_record = self._ocr_service.ingest_result(result, namespace, project_id)
            return {
                "request_id": result.request_id,
                "status": result.status.value,
                "extracted_text": result.extracted_text,
                "language": result.language,
                "confidence": result.confidence,
                "processing_time": result.processing_time,
                "error": result.error,
                "knowledge_record_id": knowledge_record.record_id if knowledge_record else None,
            }
        except Exception:
            return None

    def retrieve_fix_patterns(self, query: str, namespace: str = "default", project_id: Optional[str] = None, top_k: int = 5) -> list[dict]:
        if self._self_improvement_service is None:
            return []
        return self._self_improvement_service.retrieve_fix_patterns(query=query, namespace=namespace, project_id=project_id, top_k=top_k)

    def research(self, query: str, purpose: str = "", namespace: str = "default", project_id: Optional[str] = None, task_id: Optional[str] = None) -> Optional[dict]:
        if self._research_service is None or not self._research_service.enabled:
            return {"status": "unavailable", "error": "Research service is not available"}
        request = ResearchRequest(
            request_id=f"research-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            query=query,
            purpose=purpose,
            namespace=namespace,
            project_id=project_id,
            task_id=task_id,
        )
        result = self._research_service.research(request)
        if result.status == ResearchStatus.COMPLETED and result.finding:
            return {
                "status": result.status.value,
                "finding": result.finding.summary,
                "sources": [
                    {
                        "title": s.title,
                        "url": s.url,
                        "domain": s.domain,
                        "source_type": s.source_type.value,
                        "relevance": s.relevance,
                        "authority": s.authority,
                    }
                    for s in result.finding.sources
                ],
                "confidence": result.finding.confidence,
                "is_current": result.finding.is_current,
                "retrieval_timestamp": result.finding.retrieval_timestamp,
                "provider": result.provider,
            }
        return {"status": result.status.value, "error": result.error}

    def cancel(self) -> Optional[TaskResult]:
        self._cancel_event.set()
        try:
            if self._inference is not None:
                self._inference.cancel()
        except Exception:
            pass
        if self._current_result is not None:
            self._current_result.status = ExecutionPhase.CANCELLED
            self._task_manager.cancel()
            return self._current_result
        return None

    def get_current_state(self) -> Optional[LoopState]:
        task_id = None
        phase = None
        status = None
        model_name = None

        if self._current_result is not None:
            task_id = self._current_result.task_id
            phase = self._current_result.status

        try:
            active = self._task_manager.get_active_task()
            if active is not None:
                status = getattr(active, "status", None)
                status = status.value if status is not None else None
            else:
                last = self._task_manager.get_last_task()
                if last is not None:
                    status = getattr(last, "status", None)
                    status = status.value if status is not None else None
        except Exception:
            pass

        try:
            current_model = self._inference.get_model_status()
            if current_model is not None:
                model_name = current_model.name
        except Exception:
            pass

        return LoopState(
            task_id=task_id,
            phase=phase,
            status=status,
            model_name=model_name,
        )
