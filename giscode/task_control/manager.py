import threading
from datetime import datetime, timedelta
from typing import Optional

from giscode.task_control.types import TaskPhase, TaskState, TaskStatus


class TaskManager:
    def __init__(self):
        self._active_task: Optional[TaskState] = None
        self._last_task: Optional[TaskState] = None
        self._lock = threading.Lock()

    def start_task(
        self,
        task_id: str,
        max_duration: Optional[float] = None,
        completion_criteria: Optional[dict] = None,
    ) -> TaskState:
        with self._lock:
            if self._active_task is not None:
                raise RuntimeError("Only one active task may run for a project")
            task = TaskState(
                task_id=task_id,
                start_time=datetime.now(),
                status=TaskStatus.RUNNING,
                current_phase=TaskPhase.ANALYZING,
                max_duration=max_duration,
                completion_criteria=completion_criteria,
                _cancel_event=threading.Event(),
            )
            self._active_task = task
            if max_duration is not None:
                task._timer = threading.Timer(
                    max_duration, self._on_timeout, args=[task]
                )
                # A watchdog timer must not block interpreter/process exit
                # (the GUI never completes the task through TaskManager, so
                # an un-cancelled timer would otherwise keep the process
                # alive for up to max_duration after shutdown).
                task._timer.daemon = True
                task._timer.start()
            return task

    def _on_timeout(self, task: TaskState) -> None:
        with self._lock:
            if self._active_task is task and not task._cancel_event.is_set():
                task.status = TaskStatus.TIMEOUT
                task.failure_info = {
                    "reason": "timeout",
                    "message": f"Task exceeded max duration of {task.max_duration}s",
                }
                if task._timer:
                    task._timer.cancel()
                self._last_task = task
                self._active_task = None

    def update_phase(self, phase: TaskPhase) -> None:
        with self._lock:
            if self._active_task is None:
                raise RuntimeError("No active task")
            self._active_task.current_phase = phase

    def get_elapsed(self) -> float:
        with self._lock:
            if self._active_task is None or self._active_task.start_time is None:
                return 0.0
            return (datetime.now() - self._active_task.start_time).total_seconds()

    def pause_timeout(self) -> None:
        """Pause the active task watchdog while a user approval is pending."""
        with self._lock:
            task = self._active_task
            if task is None or task.max_duration is None or getattr(task, "_approval_paused", False):
                return
            elapsed = (datetime.now() - task.start_time).total_seconds()
            task._approval_paused = True
            task._approval_elapsed = elapsed
            if task._timer:
                task._timer.cancel()

    def resume_timeout(self) -> None:
        """Resume the active task watchdog after an approval decision."""
        with self._lock:
            task = self._active_task
            if task is None or not getattr(task, "_approval_paused", False):
                return
            elapsed = float(getattr(task, "_approval_elapsed", 0.0))
            task.start_time = datetime.now() - timedelta(seconds=elapsed)
            task._approval_paused = False
            remaining = max(0.0, float(task.max_duration) - elapsed)
            if remaining <= 0:
                task.status = TaskStatus.TIMEOUT
                task.failure_info = {
                    "reason": "timeout",
                    "message": f"Task exceeded max duration of {task.max_duration}s",
                }
                self._last_task = task
                self._active_task = None
                return
            task._timer = threading.Timer(remaining, self._on_timeout, args=[task])
            task._timer.daemon = True
            task._timer.start()

    def checkpoint(self, label: str) -> None:
        with self._lock:
            if self._active_task is None:
                raise RuntimeError("No active task")
            self._active_task.checkpoints.append(
                {
                    "label": label,
                    "time": datetime.now().isoformat(),
                    "phase": self._active_task.current_phase.value,
                }
            )

    def cancel(self) -> Optional[TaskState]:
        with self._lock:
            if self._active_task is None:
                return None
            task = self._active_task
            task._cancel_event.set()
            task.status = TaskStatus.CANCELLED
            if task._timer:
                task._timer.cancel()
            self._last_task = task
            self._active_task = None
            return task

    def revoke(self) -> Optional[TaskState]:
        with self._lock:
            if self._active_task is None:
                return None
            task = self._active_task
            task._cancel_event.set()
            task.status = TaskStatus.CANCELLED
            task._restore_required = True
            if task._timer:
                task._timer.cancel()
            self._last_task = task
            self._active_task = None
            return task

    def complete(
        self, success: bool = True, failure_info: Optional[dict] = None
    ) -> Optional[TaskState]:
        with self._lock:
            if self._active_task is None:
                return None
            task = self._active_task
            task._cancel_event.set()
            if task._timer:
                task._timer.cancel()
            if success:
                task.status = TaskStatus.COMPLETED
            else:
                task.status = TaskStatus.FAILED
                task.failure_info = failure_info or {"reason": "unknown"}
            self._last_task = task
            self._active_task = None
            return task

    def get_active_task(self) -> Optional[TaskState]:
        with self._lock:
            return self._active_task

    def get_last_task(self) -> Optional[TaskState]:
        with self._lock:
            return self._last_task
