"""Replaceable task repository and task state management."""
from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from backend.app.orchestrator.models import ExecutionEvent, TaskResponse, TaskStatus


class InMemoryTaskRepository:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskResponse] = {}
        self._lock = RLock()

    def save(self, task: TaskResponse) -> TaskResponse:
        with self._lock:
            self._tasks[task.task_id] = task.model_copy(deep=True)
            return task

    def get(self, task_id: str) -> TaskResponse:
        with self._lock:
            if task_id not in self._tasks:
                raise KeyError(f"Task '{task_id}' not found")
            return self._tasks[task_id].model_copy(deep=True)


class TaskManager:
    def __init__(self, repository: InMemoryTaskRepository | None = None) -> None:
        self.repository = repository or InMemoryTaskRepository()

    def create(self, goal: str, steps: list, missing_information: list[str] | None = None) -> TaskResponse:
        status = TaskStatus.NEEDS_CLARIFICATION if missing_information else TaskStatus.PENDING
        task = TaskResponse(task_id=str(uuid4()), goal=goal, status=status, steps=steps, missing_information=missing_information or [])
        self.event(task, status.value, "Task created")
        return self.repository.save(task)

    def event(self, task: TaskResponse, status: str, message: str, step_id: str | None = None) -> None:
        task.events.append(ExecutionEvent(timestamp=datetime.now(timezone.utc).isoformat(), status=status, message=message, step_id=step_id))

    def save(self, task: TaskResponse) -> TaskResponse:
        return self.repository.save(task)

    def get(self, task_id: str) -> TaskResponse:
        return self.repository.get(task_id)

    def cancel(self, task_id: str) -> TaskResponse:
        task = self.get(task_id)
        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return task
        task.status = TaskStatus.CANCELLED
        self.event(task, "cancelled", "Task cancelled")
        return self.save(task)
