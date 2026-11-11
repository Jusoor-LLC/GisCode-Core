"""Adapt Core's model-required requests to the existing CodingAgentLoop.

Core already owns a complete coding pipeline: :class:`CodingAgentLoop` plans
with the selected model, executes create/modify/read steps through the guarded
writer, verifies the project's tests, repairs failures, and rolls back a failed
task. Chat (``model_chat``) is deliberately read-only, so it must not answer a
request to change code.

This module is only the boundary between the two: it turns one Core request
into a ``TaskRequest`` for the loop and turns the loop's ``TaskResult`` back
into the result shape the GUI already consumes. It adds no second planner, tool
registry or approval policy.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Callable, Optional

from giscode.agentic import capabilities
from giscode.agentic.types import (
    ActionType,
    ExecutionPhase,
    TaskRequest,
    TaskResult,
)

#: Generative requests that must edit code rather than answer a question.
#: ``explain`` and ``review`` remain read-only Chat requests; ``plan`` is a
#: planning conversation unless it also asks for implementation.
CODING_CAPABILITIES = frozenset({"fix", "refactor", "generate", "rename"})

_WRITE_TYPES = frozenset({ActionType.CREATE, ActionType.MODIFY})


def can_handle(status: str, capability: str) -> bool:
    """True when this request belongs to the editing loop, not read-only Chat."""
    return status == capabilities.MODEL_REQUIRED and capability in CODING_CAPABILITIES


def _relative(path: str, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return str(path)


def _action_record(action, root: Path) -> dict:
    action_type = getattr(action, "action_type", None)
    target = str(getattr(action, "target_path", "") or "")
    return {
        "tool": "write_file" if action_type in _WRITE_TYPES else "read_file",
        "args": {"path": _relative(target, root)} if target else {},
        "success": bool(getattr(action, "success", False)),
        "original_content": getattr(action, "original_content", None),
        "error": getattr(action, "error", None),
    }


def _verification_record(result: TaskResult) -> dict:
    verification = result.verification
    if verification is None:
        return {
            "success": False,
            "errors": ["the model coding task produced no verification result"],
            "details": [],
            "test_execution_status": "unknown",
        }
    return {
        "success": bool(verification.success),
        "errors": list(verification.errors or []),
        "details": list(verification.details or []),
        "test_execution_status": str(
            getattr(verification, "test_execution_status", "unknown") or "unknown"
        ),
    }


def _status(result: TaskResult) -> str:
    if result.status == ExecutionPhase.COMPLETE:
        return capabilities.COMPLETED
    if result.status == ExecutionPhase.CANCELLED:
        return capabilities.CANCELLED
    if result.status == ExecutionPhase.TIMED_OUT:
        return capabilities.TIMED_OUT
    return capabilities.FAILED


def _summary(
    result: TaskResult,
    *,
    status: str,
    changed_files: list[str],
    verification: dict,
) -> str:
    if status == capabilities.CANCELLED:
        headline = "Model coding task cancelled. Edits already written were kept."
    elif status == capabilities.TIMED_OUT:
        headline = "Model coding task timed out before verification completed."
    elif status == capabilities.FAILED:
        headline = "Model coding task failed. The task was rolled back when possible."
    else:
        headline = "Model coding task completed."

    lines = [headline]
    if result.plan is not None and result.plan.objective:
        lines.append(f"Goal: {result.plan.objective}")
    if changed_files:
        lines.append("")
        lines.append("Changed files:")
        lines.extend(f"- {path}" for path in changed_files)
    else:
        lines.append("")
        lines.append("No project files were changed.")
    lines.append("")
    lines.append(f"Verification: {'passed' if verification.get('success') else 'did not pass'}")
    details = verification.get("details") or []
    if details:
        lines.extend(f"- {detail}" for detail in details[:8])
    errors = verification.get("errors") or []
    if errors:
        lines.append("Verification errors:")
        lines.extend(f"- {error}" for error in errors[:8])
    if result.error:
        lines.append("")
        lines.append(f"Error: {result.error}")
    if result.response:
        lines.append("")
        lines.append("Model summary:")
        lines.append(str(result.response).strip())
    return "\n".join(lines).strip()


def run_coding_agent(
    coding_agent,
    *,
    message: str,
    project_root: str,
    task_id: str,
    capability: str,
    agent_task_id: Optional[str] = None,
    deadline_s: Optional[float] = None,
    approve: Optional[Callable[[Path, str], bool]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Run one request through the existing coding loop and adapt its result.

    ``agent_task_id`` lets the GUI's already-running task record be adopted by
    the loop instead of creating a second task manager entry. It is passed only
    when the loop's public signature supports it, so older hosts and test
    doubles keep working.
    """
    root = Path(project_root).expanduser().resolve()
    setter = getattr(coding_agent, "set_edit_approval_callback", None)
    if callable(setter) and approve is not None:
        setter(approve)

    request = TaskRequest(
        task_id=task_id,
        description=str(message or ""),
        max_duration=deadline_s,
        completion_criteria={"project_root": str(root)},
    )
    kwargs = {"root_path": str(root)}
    try:
        params = inspect.signature(coding_agent.start).parameters
    except (TypeError, ValueError):
        params = {}
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in params.values()
    )
    if "adopt_active_task" in params or accepts_kwargs:
        kwargs["adopt_active_task"] = True
        if agent_task_id:
            kwargs["expected_task_id"] = str(agent_task_id)

    result = coding_agent.start(request, **kwargs)
    if not isinstance(result, TaskResult):
        raise TypeError(
            "CodingAgentLoop.start returned "
            f"{type(result).__name__}, expected TaskResult"
        )

    status = _status(result)
    verification = _verification_record(result)
    actions = [_action_record(action, root) for action in result.actions]
    changed_files = []
    try:
        changed_files = list(coding_agent.changed_files_for(task_id) or [])
    except Exception:  # noqa: BLE001 - a summary must not replace the real result
        changed_files = []
    if not changed_files:
        changed_files = [
            action["args"]["path"]
            for action in actions
            if action.get("success")
            and action.get("tool") == "write_file"
            and action.get("args", {}).get("path")
        ]

    error = result.error
    if status == capabilities.COMPLETED and not verification.get("success"):
        # A loop bug must never turn an unverified edit into a success.
        status = capabilities.FAILED
        error = error or "the coding loop completed without successful verification"
    success = status == capabilities.COMPLETED
    response = _summary(
        result,
        status=status,
        changed_files=changed_files,
        verification=verification,
    )
    return {
        "task_id": task_id,
        "capability": capability,
        "status": status,
        "success": success,
        "response": response,
        "error": error,
        "work_result": {
            "capability": capability,
            "status": status,
            "success": success,
            "error": error,
            "actions": actions,
            "verification": verification,
            "observations": [
                "executed by CodingAgentLoop through Core's dispatcher",
                "no GisAI or Pro module was used",
            ],
            "changed_files": changed_files,
            "repair_attempts": (
                1
                if result.failure_info
                and str(result.failure_info.get("reason", "")).startswith("repair")
                else 0
            ),
        },
    }
