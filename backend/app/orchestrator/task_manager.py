"""Shared-contract task repository with private runtime state and execution claims."""
from __future__ import annotations

from contextlib import contextmanager
from threading import Lock, RLock
from typing import Iterator
from uuid import UUID, uuid4

from pydantic import UUID4

from backend.app.schemas.contracts import (
    ApprovalRequest,
    ApprovalStatus,
    EventStatus,
    ExecutionEvent,
    StepStatus,
    TaskCreateRequest,
    TaskResponse,
    TaskStatus,
    TaskStep,
    ToolCall,
    utc_now,
)


class InvalidApprovalError(ValueError):
    pass


def normalize_task_id(task_id: UUID | str) -> UUID4:
    try:
        parsed = UUID(str(task_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"Invalid task UUID '{task_id}'") from exc
    if parsed.version != 4:
        raise ValueError("Task ID must be a UUID4")
    return parsed  # type: ignore[return-value]


class InMemoryTaskRepository:
    def __init__(self) -> None:
        self._tasks: dict[UUID4, TaskResponse] = {}
        self._lock = RLock()

    def save(self, task: TaskResponse) -> TaskResponse:
        with self._lock:
            self._tasks[task.task_id] = task.model_copy(deep=True)
            return task

    def get(self, task_id: UUID | str) -> TaskResponse:
        key = normalize_task_id(task_id)
        with self._lock:
            if key not in self._tasks:
                raise KeyError(f"Task '{key}' not found")
            return self._tasks[key].model_copy(deep=True)


class TaskManager:
    def __init__(self, repository: InMemoryTaskRepository | None = None) -> None:
        self.repository = repository or InMemoryTaskRepository()
        self._events: dict[UUID4, list[ExecutionEvent]] = {}
        self._missing_information: dict[UUID4, list[str]] = {}
        self._auto_execute: dict[UUID4, bool] = {}
        self._tool_calls: dict[UUID4, list[ToolCall]] = {}
        self._approval_history: dict[UUID4, list[ApprovalRequest]] = {}
        self._consumed_approvals: set[UUID4] = set()
        self._execution_locks: dict[UUID4, Lock] = {}
        self._lock = RLock()

    @contextmanager
    def execution_lock(self, task_id: UUID | str) -> Iterator[None]:
        key = normalize_task_id(task_id)
        with self._lock:
            task_lock = self._execution_locks.setdefault(key, Lock())
        task_lock.acquire()
        try:
            yield
        finally:
            task_lock.release()

    def create(
        self,
        request: TaskCreateRequest,
        steps: list[TaskStep],
        missing_information: list[str] | None = None,
    ) -> TaskResponse:
        self._validate_unique_step_ids(steps)
        task_id = uuid4()
        missing = list(missing_information or [])
        status = TaskStatus.NEEDS_ATTENTION if missing else TaskStatus.CREATED
        task = TaskResponse(task_id=task_id, user_request=request.user_request, status=status, plan=steps)
        if missing:
            task.result = {"missing_information": missing}
        with self._lock:
            self._events[task_id] = []
            self._missing_information[task_id] = missing
            self._auto_execute[task_id] = request.auto_execute
            self._tool_calls[task_id] = []
            self._approval_history[task_id] = []
        self.event(task, "task.created", EventStatus.CREATED, "Task created")
        return self.repository.save(task)

    @staticmethod
    def _validate_unique_step_ids(steps: list[TaskStep]) -> None:
        step_ids = [step.step_id for step in steps]
        duplicates = sorted({step_id for step_id in step_ids if step_ids.count(step_id) > 1})
        if duplicates:
            raise ValueError(f"Duplicate step_id values are not allowed: {', '.join(duplicates)}")

    def event(
        self,
        task: TaskResponse,
        event_type: str,
        status: EventStatus,
        message: str,
        step_id: str | None = None,
        evidence_ids: list[UUID4] | None = None,
    ) -> ExecutionEvent:
        event = ExecutionEvent(
            task_id=task.task_id,
            step_id=step_id,
            event_type=event_type,
            message=message,
            status=status,
            evidence_ids=evidence_ids or [],
        )
        with self._lock:
            self._events.setdefault(task.task_id, []).append(event)
        return event

    def events(self, task_id: UUID | str) -> list[ExecutionEvent]:
        key = normalize_task_id(task_id)
        with self._lock:
            return [event.model_copy(deep=True) for event in self._events.get(key, [])]

    def missing_information(self, task_id: UUID | str) -> list[str]:
        key = normalize_task_id(task_id)
        with self._lock:
            return list(self._missing_information.get(key, []))

    def auto_execute(self, task_id: UUID | str) -> bool:
        key = normalize_task_id(task_id)
        with self._lock:
            return self._auto_execute.get(key, False)

    def record_tool_call(self, task_id: UUID | str, tool_call: ToolCall) -> None:
        key = normalize_task_id(task_id)
        with self._lock:
            self._tool_calls.setdefault(key, []).append(tool_call.model_copy(deep=True))

    def tool_calls(self, task_id: UUID | str) -> list[ToolCall]:
        key = normalize_task_id(task_id)
        with self._lock:
            return [call.model_copy(deep=True) for call in self._tool_calls.get(key, [])]

    def resolve_approval(
        self,
        task_id: UUID | str,
        approval_id: UUID | str,
        decision: ApprovalStatus | str,
    ) -> TaskResponse:
        key = normalize_task_id(task_id)
        try:
            approval_key = normalize_task_id(approval_id)
        except ValueError as exc:
            raise InvalidApprovalError(f"Invalid approval UUID '{approval_id}'") from exc
        try:
            resolved_status = ApprovalStatus(decision)
        except (ValueError, TypeError) as exc:
            raise InvalidApprovalError("Approval decision must be APPROVED or REJECTED") from exc
        if resolved_status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise InvalidApprovalError("Approval decision must be APPROVED or REJECTED")
        with self._lock:
            task = self.repository.get(key)
            pending = next((item for item in task.pending_approvals if item.approval_id == approval_key), None)
            if pending is None or pending.task_id != key or pending.status != ApprovalStatus.PENDING:
                raise InvalidApprovalError(f"No pending approval '{approval_key}' belongs to task '{key}'")
            step = next((item for item in task.plan if item.step_id == pending.step_id), None)
            if step is None or step.tool_name != pending.tool_name or step.input != pending.arguments:
                raise InvalidApprovalError("Pending approval no longer matches its planned step and arguments")
            pending.status = resolved_status
            self._approval_history.setdefault(key, []).append(pending.model_copy(deep=True))
            task.pending_approvals = [item for item in task.pending_approvals if item.approval_id != approval_key]
            if resolved_status == ApprovalStatus.APPROVED:
                step.status = StepStatus.PENDING
                task.status = TaskStatus.EXECUTING
                task.error = None
                event_status = EventStatus.RUNNING
                message = f"Approval {approval_key} approved"
                event_type = "approval.approved"
            else:
                step.status = StepStatus.SKIPPED
                step.error = "User rejected the approval request"
                task.status = TaskStatus.NEEDS_ATTENTION
                task.error = step.error
                event_status = EventStatus.NEEDS_ATTENTION
                message = f"Approval {approval_key} rejected; step was not executed"
                event_type = "approval.rejected"
            self.event(task, event_type, event_status, message, pending.step_id)
            return self.save(task)

    def approval_history(self, task_id: UUID | str) -> list[ApprovalRequest]:
        key = normalize_task_id(task_id)
        with self._lock:
            return [approval.model_copy(deep=True) for approval in self._approval_history.get(key, [])]

    def approved_request(self, call: ToolCall) -> ApprovalRequest | None:
        key = normalize_task_id(call.task_id)
        with self._lock:
            for approval in reversed(self._approval_history.get(key, [])):
                if (
                    approval.status == ApprovalStatus.APPROVED
                    and approval.approval_id not in self._consumed_approvals
                    and approval.step_id == call.step_id
                    and approval.tool_name == call.tool_name
                    and approval.arguments == call.arguments
                ):
                    return approval.model_copy(deep=True)
        return None

    def claim_tool_call(self, task_id: UUID | str, call: ToolCall, approval: ApprovalRequest | None) -> bool:
        key = normalize_task_id(task_id)
        with self._lock:
            task = self.repository.get(key)
            if task.status not in {TaskStatus.CREATED, TaskStatus.EXECUTING}:
                return False
            if call.task_id != key:
                return False
            step = next((item for item in task.plan if item.step_id == call.step_id), None)
            if step is None or step.tool_name != call.tool_name or step.input != call.arguments:
                return False
            if step.status not in {StepStatus.PENDING, StepStatus.AWAITING_APPROVAL}:
                return False
            if task.current_step not in {None, call.step_id}:
                return False
            if step.status not in {StepStatus.PENDING, StepStatus.AWAITING_APPROVAL}:
                return False
            if approval is not None:
                approved = next(
                    (
                        item for item in self._approval_history.get(key, [])
                        if item.approval_id == approval.approval_id
                        and item.status == ApprovalStatus.APPROVED
                        and item.approval_id not in self._consumed_approvals
                        and item.step_id == call.step_id
                        and item.tool_name == call.tool_name
                        and item.arguments == call.arguments
                    ),
                    None,
                )
                if approved is None:
                    return False
                self._consumed_approvals.add(approved.approval_id)
            task.status = TaskStatus.EXECUTING
            task.current_step = call.step_id
            step.status = StepStatus.RUNNING
            self.record_tool_call(key, call)
            self.event(task, "tool.started", EventStatus.RUNNING, f"Executing {call.tool_name}", call.step_id)
            self.save(task)
            return True

    def save(self, task: TaskResponse) -> TaskResponse:
        with self._lock:
            current = self.repository.get(task.task_id)
            if current.status == TaskStatus.CANCELLED and task.status != TaskStatus.CANCELLED:
                task.status = TaskStatus.CANCELLED
            task.updated_at = utc_now()
            return self.repository.save(task)

    def get(self, task_id: UUID | str) -> TaskResponse:
        return self.repository.get(task_id)

    def cancel(self, task_id: UUID | str) -> TaskResponse:
        key = normalize_task_id(task_id)
        with self._lock:
            task = self.repository.get(key)
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                return task
            task.status = TaskStatus.CANCELLED
            for approval in task.pending_approvals:
                if approval.status == ApprovalStatus.PENDING:
                    approval.status = ApprovalStatus.CANCELLED
                    self._approval_history.setdefault(key, []).append(approval.model_copy(deep=True))
            task.pending_approvals = []
            self.event(task, "task.cancelled", EventStatus.CANCELLED, "Task cancelled", task.current_step)
            return self.save(task)
