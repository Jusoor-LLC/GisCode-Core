from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from giscode.task_router.memory import TaskMemoryStore
from giscode.task_router.router import CapabilityRouter
from giscode.task_router.types import (
    Attachment,
    CapabilityPlan,
    CapabilityType,
    RouterDecision,
    TaskIntent,
    TaskMemory,
    TaskState,
)
from giscode.task_router.understanding import detect_intent, needs_image_for_intent


@dataclass
class ExecutionContext:
    task: TaskMemory
    project_context: Any = None
    coding_agent: Any = None
    vision_service: Any = None
    model_manager: Any = None


class DelegationEngine:
    def __init__(
        self,
        task_store: TaskMemoryStore,
        capability_router: CapabilityRouter,
        vision_service: Optional[Any] = None,
        model_manager: Optional[Any] = None,
        coding_agent: Optional[Any] = None,
        project_context: Optional[Any] = None,
        inference: Optional[Any] = None,
    ):
        self._store = task_store
        self._router = capability_router
        self._vision_service = vision_service
        self._model_manager = model_manager
        self._coding_agent = coding_agent
        self._project_context = project_context
        self._inference = inference

        self._router.set_vision_service(vision_service) if vision_service else None

    def execute(self, task_id: str, on_status: Optional[Callable[[str], None]] = None) -> Any:
        task = self._store.get_task(task_id)
        if not task:
            return {"error": f"Task {task_id} not found"}

        if not task.intent:
            return {"error": "No intent for task"}

        # Get routing decision
        decision = self._router.route(task.intent, project_active=bool(task.project_id))

        # Plan capabilities
        available = self._get_available_capabilities()
        plan = self._router.plan_capabilities(task.intent, available)
        self._store.set_capability_plan(task_id, plan)

        # Load missing capabilities
        for cap in plan.load_order:
            if on_status:
                on_status(f"Loading {cap.value} capability...")
            self._load_capability(cap)
            self._store.set_current_capability(task_id, cap)

        # Execute based on primary capability
        task.state = TaskState.EXECUTING
        self._store.update_state(task_id, TaskState.EXECUTING)

        result = self._execute_decision(decision, task)

        if on_status:
            on_status("Completed")

        return result

    def _get_available_capabilities(self) -> list[str]:
        caps = ["general_reasoning", "chat"]
        if self._vision_service and self._vision_service.is_available():
            caps.append("vision")
        if self._coding_agent:
            caps.append("coding")
        if self._project_context:
            caps.append("project_inspection")
        if self._inference:
            caps.append("inference")
        return caps

    def _load_capability(self, cap: CapabilityType) -> bool:
        if cap == CapabilityType.VISION:
            if self._vision_service and not self._vision_service.is_available():
                # Vision service will handle on-demand loading during analyze()
                return True
        return True

    def _execute_decision(self, decision: RouterDecision, task: TaskMemory) -> Any:
        primary = decision.primary_capability

        if primary == CapabilityType.VISION:
            return self._execute_vision(task)
        elif primary == CapabilityType.CODING:
            return self._execute_coding(task)
        elif primary == CapabilityType.PROJECT_INSPECTION:
            return self._execute_project_inspection(task)
        elif primary == CapabilityType.GENERAL_REASONING and self._inference:
            return self._execute_inference(task)
        else:
            return self._execute_general(task)

    def _execute_vision(self, task: TaskMemory) -> Any:
        if not self._vision_service or not task.attachments:
            return {"error": "No vision service or attachments"}

        # Use first attachment
        attachment = task.attachments[0]
        from giscode.vision.types import VisionRequest

        request = VisionRequest(
            image_paths=[attachment.path],
            prompt=task.pending_text or "Analyze this image",
            request_id=f"vision-{task.task_id}",
            project_id=task.project_id,
        )

        result = self._vision_service.analyze(request)
        return {
            "vision_result": result.text if result.status.value == "complete" else None,
            "model_used": result.model_used,
            "error": result.error,
        }

    def _execute_coding(self, task: TaskMemory) -> Any:
        if self._coding_agent:
            # Delegate to coding agent loop
            from giscode.agentic.types import TaskRequest

            request = TaskRequest(
                task_id=task.task_id,
                description=task.pending_text or "Coding task",
                max_duration=300,
            )

            result = self._coding_agent.start(request)
            return {
                "coding_result": result.response,
                "status": result.status.value,
                "error": result.error,
            }
        # Fall back to inference for coding tasks
        return self._execute_inference(task)

    def _execute_project_inspection(self, task: TaskMemory) -> Any:
        if not self._project_context:
            return {"error": "Project context not available"}

        # Return project structure info
        return {
            "project_path": self._project_context.root_path,
            "active_section": self._project_context.active_section,
            "section_history": self._project_context.section_history,
        }

    def _execute_inference(self, task: TaskMemory) -> Any:
        if not self._inference:
            return {"error": "No inference available"}

        # Build messages like the old _generate_chat_reply with project context
        from giscode.inference.runtime import ChatRequest, ChatMessage

        context_block = ""
        if self._project_context and self._project_context.root_path:
            context_block = (
                "\n\nActive project context:\n"
                f"- project name: {self._project_context.name or ''}\n"
                f"- project path: {self._project_context.root_path}\n"
                f"- active section: {self._project_context.active_section or 'Overview'}\n"
                "\nThe active project is a SOFTWARE DEVELOPMENT PROJECT. "
                "This GisCode app is a software-development tool; commands such as "
                "'inspect', 'code', 'test', 'build' or 'fix' must be interpreted as "
                "software-development operations on the active project path above, "
                "NOT as GIS/geospatial data operations, unless the user explicitly "
                "says they are working on geospatial/GIS data."
            )

        messages = [
            ChatMessage(
                role="system",
                content=(
                    "You are GisCode, a local-first AI assistant inside the GisCode desktop app. "
                    "Reply concisely and directly to the user's latest message using its exact instructions."
                    + context_block
                ),
            ),
            ChatMessage(role="user", content=task.pending_text),
        ]

        request = ChatRequest(messages=messages, max_tokens=1024, temperature=0.7)
        response = self._inference.chat_generate(request)
        return {"response": response.text, "model": "inference"}

    def _execute_general(self, task: TaskMemory) -> Any:
        # Fallback to simple chat response
        return {"response": "Task understood but no specific handler available."}


def create_task_from_input(
    text: str,
    project_id: str,
    section: str,
    has_image: bool = False,
    store: Optional[TaskMemoryStore] = None,
) -> TaskMemory:
    intent = detect_intent(text, has_image=has_image, project_active=bool(project_id))
    task_id = f"task-{intent.category.value}-{id(text)}"

    memory = TaskMemory(
        task_id=task_id,
        project_id=project_id,
        section=section,
        state=TaskState.PENDING if not needs_image_for_intent(intent) else TaskState.WAITING_FOR_INPUT,
        intent=intent,
        pending_text=text,
    )

    if store:
        store._memories[task_id] = memory

    return memory