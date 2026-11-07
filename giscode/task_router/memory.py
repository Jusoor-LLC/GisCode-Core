from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

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
class PendingTask:
    task_id: str
    intent: TaskIntent
    state: TaskState = TaskState.WAITING_FOR_INPUT
    awaiting: str = "image"
    result: Any = None


class TaskMemoryStore:
    def __init__(self):
        self._memories: Dict[str, TaskMemory] = {}
        self._pending: Dict[str, PendingTask] = {}
        self._lock = threading.RLock()

    def create_task(
        self,
        project_id: str,
        section: str,
        initial_text: str = "",
        has_image: bool = False,
    ) -> TaskMemory:
        with self._lock:
            task_id = f"task-{uuid.uuid4().hex[:8]}"
            intent = detect_intent(initial_text, has_image=has_image, project_active=bool(project_id))

            memory = TaskMemory(
                task_id=task_id,
                project_id=project_id,
                section=section,
                state=TaskState.ROUTING if not needs_image_for_intent(intent) else TaskState.WAITING_FOR_INPUT,
                intent=intent,
                pending_text=initial_text,
            )
            self._memories[task_id] = memory
            return memory

    def get_task(self, task_id: str) -> Optional[TaskMemory]:
        with self._lock:
            return self._memories.get(task_id)

    def get_active_task(self, project_id: str) -> Optional[TaskMemory]:
        with self._lock:
            for mem in self._memories.values():
                if mem.project_id == project_id and mem.state in (
                    TaskState.PENDING,
                    TaskState.WAITING_FOR_INPUT,
                    TaskState.ROUTING,
                    TaskState.LOADING_CAPABILITY,
                    TaskState.EXECUTING,
                ):
                    return mem
            return None

    def add_attachment(self, task_id: str, attachment: Attachment) -> Optional[TaskMemory]:
        with self._lock:
            mem = self._memories.get(task_id)
            if mem:
                mem.add_attachment(attachment)
                if mem.state == TaskState.WAITING_FOR_INPUT:
                    mem.state = TaskState.ROUTING
                mem.updated_at = datetime.now()
            return mem

    def set_intent(self, task_id: str, intent: TaskIntent) -> Optional[TaskMemory]:
        with self._lock:
            mem = self._memories.get(task_id)
            if mem:
                mem.set_intent(intent)
            return mem

    def set_capability_plan(self, task_id: str, plan: CapabilityPlan) -> Optional[TaskMemory]:
        with self._lock:
            mem = self._memories.get(task_id)
            if mem:
                mem.capability_plan = plan
                mem.updated_at = datetime.now()
            return mem

    def update_state(self, task_id: str, state: TaskState) -> Optional[TaskMemory]:
        with self._lock:
            mem = self._memories.get(task_id)
            if mem:
                mem.state = state
                mem.updated_at = datetime.now()
            return mem

    def set_current_capability(self, task_id: str, cap: CapabilityType) -> Optional[TaskMemory]:
        with self._lock:
            mem = self._memories.get(task_id)
            if mem:
                mem.current_capability = cap
                mem.updated_at = datetime.now()
            return mem

    def complete(self, task_id: str, result: Any) -> Optional[TaskMemory]:
        with self._lock:
            mem = self._memories.get(task_id)
            if mem:
                mem.state = TaskState.COMPLETED
                mem.result = result
                mem.updated_at = datetime.now()
            return mem

    def fail(self, task_id: str, error: str) -> Optional[TaskMemory]:
        with self._lock:
            mem = self._memories.get(task_id)
            if mem:
                mem.state = TaskState.FAILED
                mem.error = error
                mem.updated_at = datetime.now()
            return mem

    def cancel(self, task_id: str) -> Optional[TaskMemory]:
        with self._lock:
            mem = self._memories.get(task_id)
            if mem:
                mem.state = TaskState.CANCELLED
                mem.updated_at = datetime.now()
            return mem

    def list_tasks(self, project_id: Optional[str] = None) -> list[TaskMemory]:
        with self._lock:
            if project_id:
                return [m for m in self._memories.values() if m.project_id == project_id]
            return list(self._memories.values())