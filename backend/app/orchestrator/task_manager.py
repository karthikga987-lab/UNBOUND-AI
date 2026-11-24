"""Shared-contract task repository with private runtime state and execution claims."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import ntpath
from pathlib import Path
from threading import Lock, RLock
from typing import Callable, Iterator
from uuid import UUID, uuid4

from pydantic import UUID4

from backend.app.safety.permissions import action_requires_approval

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


@dataclass(repr=False)
class _ReplayApprovalBinding:
    """Private action authorization state; never stored in public TaskResponse."""

    task_id: UUID
    parent_tool_call_id: UUID
    continuation_id: UUID
    workflow_id: UUID
    replay_run_id: UUID
    pending_action_id: str
    step_id: str
    action: str
    action_digest: str
    exact_arguments: dict = field(repr=False)
    expires_at: datetime
    approval_id: UUID | None
    status: ApprovalStatus
    authorization_issued: bool = False
    resume_claimed: bool = False

    @property
    def action_key(self) -> tuple[UUID, UUID, str]:
        return self.task_id, self.parent_tool_call_id, self.pending_action_id


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
    def __init__(
        self,
        repository: InMemoryTaskRepository | None = None,
        *,
        approval_ttl_seconds: int = 600,
        replay_approval_ttl: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be positive")
        if not isinstance(replay_approval_ttl, timedelta) or replay_approval_ttl <= timedelta(0):
            raise ValueError("replay_approval_ttl must be positive")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.repository = repository or InMemoryTaskRepository()
        self._approval_ttl = timedelta(seconds=approval_ttl_seconds)
        self._replay_approval_ttl = replay_approval_ttl
        self._clock = clock
        self._events: dict[UUID4, list[ExecutionEvent]] = {}
        self._missing_information: dict[UUID4, list[str]] = {}
        self._auto_execute: dict[UUID4, bool] = {}
        self._tool_calls: dict[UUID4, list[ToolCall]] = {}
        self._approval_history: dict[UUID4, list[ApprovalRequest]] = {}
        self._consumed_approvals: set[UUID4] = set()
        self._replay_actions: dict[tuple[UUID, UUID, str], _ReplayApprovalBinding] = {}
        self._replay_approval_ids: dict[UUID, _ReplayApprovalBinding] = {}
        self._resume_approval_caps: dict[UUID, tuple[UUID, UUID]] = {}
        self._execution_locks: dict[UUID4, Lock] = {}
        self._replay_dispatch_locks: dict[UUID4, Lock] = {}
        self._terminal_callbacks: list[Callable[[UUID4, TaskStatus], None]] = []
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

    @contextmanager
    def replay_dispatch_lock(self, task_id: UUID | str) -> Iterator[None]:
        """Serialize replay action dispatch/checkpointing against task cancellation."""
        key = normalize_task_id(task_id)
        with self._lock:
            task_lock = self._replay_dispatch_locks.setdefault(key, Lock())
        task_lock.acquire()
        try:
            yield
        finally:
            task_lock.release()

    def is_cancelled(self, task_id: UUID | str) -> bool:
        try:
            return self.repository.get(task_id).status == TaskStatus.CANCELLED
        except Exception:
            return True

    def register_terminal_callback(self, callback: Callable[[UUID4, TaskStatus], None]) -> None:
        if not callable(callback):
            raise TypeError("terminal callback must be callable")
        with self._lock:
            if callback not in self._terminal_callbacks:
                self._terminal_callbacks.append(callback)

    def invalidate_replay_authorizations(self) -> None:
        """Invalidate process-local replay grants during application shutdown."""
        with self._lock:
            for binding in self._replay_actions.values():
                binding.status = ApprovalStatus.CANCELLED
                binding.authorization_issued = False
                binding.resume_claimed = True
                binding.exact_arguments = {}
            self._replay_actions.clear()
            self._replay_approval_ids.clear()
            self._resume_approval_caps.clear()

    def notify_task_terminal(self, task_id: UUID | str) -> None:
        key = normalize_task_id(task_id)
        try:
            status = self.repository.get(key).status
        except Exception:
            return
        if status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.NEEDS_ATTENTION, TaskStatus.CANCELLED}:
            return
        with self._lock:
            callbacks = tuple(self._terminal_callbacks)
        for callback in callbacks:
            try:
                callback(key, status)
            except Exception:
                continue

    def create(
        self,
        request: TaskCreateRequest,
        steps: list[TaskStep],
        missing_information: list[str] | None = None,
    ) -> TaskResponse:
        steps = [step.model_copy(deep=True) for step in steps]
        self._validate_unique_step_ids(steps)
        for index, step in enumerate(steps):
            if index and not step.depends_on:
                step.depends_on = [steps[index - 1].step_id]
        task_id = uuid4()
        missing = list(missing_information or [])
        if not steps and not missing:
            missing.append("No executable steps were produced; clarify the requested action and prepare a valid plan.")
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
        if missing:
            self.event(
                task,
                "task.needs_attention",
                EventStatus.NEEDS_ATTENTION,
                "Task requires additional information before execution",
            )
        else:
            self.event(task, "task.created", EventStatus.CREATED, "Task created")
        self.repository.save(task)
        return self._public_task(task)

    @staticmethod
    def _validate_unique_step_ids(steps: list[TaskStep]) -> None:
        step_ids = [step.step_id for step in steps]
        duplicates = sorted({step_id for step_id in step_ids if step_ids.count(step_id) > 1})
        if duplicates:
            raise ValueError(f"Duplicate step_id values are not allowed: {', '.join(duplicates)}")
        positions = {step_id: index for index, step_id in enumerate(step_ids)}
        for index, step in enumerate(steps):
            for dependency in step.depends_on:
                if dependency not in positions:
                    raise ValueError(f"Step '{step.step_id}' depends on unknown step '{dependency}'")
                if positions[dependency] >= index:
                    raise ValueError(f"Step '{step.step_id}' must depend only on an earlier step; invalid dependency '{dependency}'")

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

    def replay_authorization_decision(self, pending_action, policy_context):
        """Resolve a trusted replay action through the existing approval authority."""
        from backend.app.browser.replay_state import ReplayPendingAction, ReplayPolicyContext
        from backend.app.orchestrator.browser_replay import ReplayAuthorizationDecision

        if not isinstance(pending_action, ReplayPendingAction) or not isinstance(policy_context, ReplayPolicyContext):
            return None
        if not self._replay_inputs_match(pending_action, policy_context):
            return None
        try:
            arguments = json.loads(json.dumps(
                policy_context.exact_arguments,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ))
        except (TypeError, ValueError):
            return None

        key = policy_context.task_id
        with self._lock:
            try:
                task = self.repository.get(key)
            except (KeyError, ValueError):
                return None
            if task.status in {TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.NEEDS_ATTENTION}:
                return None
            parents = [
                call for call in self._tool_calls.get(key, [])
                if call.tool_call_id == policy_context.tool_call_id
                and call.task_id == key
                and call.tool_name == "browser.replay"
            ]
            if len(parents) != 1:
                return None
            parent = parents[0]
            try:
                parent_workflow_id = UUID(str(parent.arguments.get("workflow_id")))
            except (TypeError, ValueError, AttributeError):
                return None
            if parent_workflow_id != policy_context.workflow_id or parent.arguments.get("operation") != "start":
                return None
            step = next((item for item in task.plan if item.step_id == parent.step_id), None)
            if step is None or step.tool_name != "browser.replay" or step.input != parent.arguments:
                return None
            if step.status not in {StepStatus.PENDING, StepStatus.RUNNING, StepStatus.AWAITING_APPROVAL}:
                return None

            action_id = self._pending_action_identity(pending_action)
            action_key = (key, parent.tool_call_id, action_id)
            binding = self._replay_actions.get(action_key)
            if binding is not None:
                if not self._replay_binding_matches(binding, pending_action, policy_context, arguments):
                    return None
                if self._now() >= binding.expires_at:
                    self._expire_replay_approval_locked(task, step, binding)
                    return None
                if binding.status != ApprovalStatus.APPROVED or binding.authorization_issued:
                    return None
                if binding.approval_id is not None and not any(
                    item.approval_id == binding.approval_id and item.status == ApprovalStatus.APPROVED
                    for item in self._approval_history.get(key, [])
                ):
                    return None
                binding.authorization_issued = True
                binding.exact_arguments = {}
                choice = "approved" if binding.approval_id is not None else "allowed_without_approval"
                return ReplayAuthorizationDecision(choice, approval_id=binding.approval_id)

            tool_name = {
                "navigate": "browser.navigate",
                "click": "browser.click",
                "fill": "browser.fill",
                "select_option": "browser.fill",
            }.get(policy_context.action)
            if tool_name is None:
                return None
            requires_approval = action_requires_approval(
                tool_name=tool_name,
                description=policy_context.action,
                arguments=arguments,
            )
            now = self._now()
            approval_id = uuid4() if requires_approval else None
            binding = _ReplayApprovalBinding(
                task_id=key,
                parent_tool_call_id=parent.tool_call_id,
                continuation_id=policy_context.continuation_id,
                workflow_id=policy_context.workflow_id,
                replay_run_id=policy_context.replay_run_id,
                pending_action_id=action_id,
                step_id=policy_context.step_id,
                action=policy_context.action,
                action_digest=policy_context.action_digest,
                exact_arguments=arguments,
                expires_at=now + self._replay_approval_ttl,
                approval_id=approval_id,
                status=ApprovalStatus.PENDING if requires_approval else ApprovalStatus.APPROVED,
            )
            self._replay_actions[action_key] = binding
            if requires_approval:
                task.pending_approvals.append(ApprovalRequest(
                    approval_id=approval_id,
                    task_id=key,
                    step_id=parent.step_id,
                    tool_name="browser.replay",
                    description="A browser workflow action requires approval.",
                    arguments={},
                ))
                self._replay_approval_ids[approval_id] = binding
                task.current_step = parent.step_id
                task.status = TaskStatus.AWAITING_APPROVAL
                step.status = StepStatus.AWAITING_APPROVAL
                self.event(task, "approval.required", EventStatus.AWAITING_APPROVAL,
                           "Approval required before the next browser workflow action", parent.step_id)
                self._save(task, public=False)
                return None

            binding.authorization_issued = True
            binding.exact_arguments = {}
            return ReplayAuthorizationDecision("allowed_without_approval")

    @staticmethod
    def _replay_inputs_match(pending_action, policy_context) -> bool:
        return all((
            pending_action.replay_run_id == policy_context.replay_run_id,
            pending_action.continuation_id == policy_context.continuation_id,
            pending_action.workflow_id == policy_context.workflow_id,
            pending_action.step_id == policy_context.step_id,
            pending_action.action == policy_context.action,
            pending_action.action_digest == policy_context.action_digest,
            pending_action.sequence_index == policy_context.sequence_index,
        ))

    @staticmethod
    def _pending_action_identity(pending_action) -> str:
        return (f"{pending_action.replay_run_id}:{pending_action.continuation_id}:"
                f"{pending_action.sequence_index}:{pending_action.step_id}:{pending_action.action_digest}")

    @staticmethod
    def _replay_binding_matches(binding, pending_action, policy_context, arguments: dict) -> bool:
        return all((
            binding.task_id == policy_context.task_id,
            binding.parent_tool_call_id == policy_context.tool_call_id,
            binding.continuation_id == policy_context.continuation_id,
            binding.workflow_id == policy_context.workflow_id,
            binding.replay_run_id == policy_context.replay_run_id,
            binding.pending_action_id == TaskManager._pending_action_identity(pending_action),
            binding.step_id == policy_context.step_id,
            binding.action == policy_context.action,
            binding.action_digest == policy_context.action_digest,
            binding.exact_arguments == arguments or binding.authorization_issued,
        ))

    def replay_resume_approval(self, call: ToolCall, authorization):
        """Build an internal one-use registry capability from manager state."""
        from backend.app.browser.replay_state import ReplayStepAuthorization

        if not isinstance(call, ToolCall) or not isinstance(authorization, ReplayStepAuthorization):
            return None
        with self._lock:
            binding = self._binding_for_authorization_locked(call, authorization)
            if binding is None or binding.resume_claimed or authorization.authorization_id in self._resume_approval_caps:
                return None
            capability = uuid4()
            self._resume_approval_caps[authorization.authorization_id] = (call.task_id, capability)
            return ApprovalRequest(
                approval_id=capability,
                task_id=call.task_id,
                step_id=call.step_id,
                tool_name=call.tool_name,
                description="Trusted one-action replay continuation",
                arguments=call.arguments,
                status=ApprovalStatus.APPROVED,
            )

    def claim_replay_resume(self, call: ToolCall, authorization, registry_approval: ApprovalRequest) -> bool:
        """Consume the manager capability exactly once immediately before dispatch."""
        from backend.app.browser.replay_state import ReplayStepAuthorization

        if not isinstance(call, ToolCall) or not isinstance(authorization, ReplayStepAuthorization):
            return False
        with self._lock:
            binding = self._binding_for_authorization_locked(call, authorization)
            cap = self._resume_approval_caps.get(authorization.authorization_id)
            if (binding is None or binding.resume_claimed or cap is None
                    or registry_approval.approval_id != cap[1]
                    or registry_approval.status != ApprovalStatus.APPROVED
                    or registry_approval.task_id != call.task_id
                    or registry_approval.step_id != call.step_id
                    or registry_approval.tool_name != call.tool_name
                    or registry_approval.arguments != call.arguments):
                return False
            task = self.repository.get(call.task_id)
            if task.status in {TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.NEEDS_ATTENTION}:
                return False
            step = next((item for item in task.plan if item.step_id == call.step_id), None)
            if step is None or step.tool_name != "browser.replay":
                return False
            binding.resume_claimed = True
            self._resume_approval_caps.pop(authorization.authorization_id, None)
            task.status = TaskStatus.EXECUTING
            task.current_step = step.step_id
            step.status = StepStatus.RUNNING
            self.repository.save(task)
            return True

    def _binding_for_authorization_locked(self, call: ToolCall, authorization):
        if (call.tool_name != "browser.replay" or call.arguments != {"operation": "resume"}
                or call.task_id != authorization.task_id or call.tool_call_id != authorization.tool_call_id):
            return None
        parents = [item for item in self._tool_calls.get(call.task_id, [])
                   if item.task_id == call.task_id and item.tool_call_id == call.tool_call_id
                   and item.step_id == call.step_id and item.tool_name == "browser.replay"
                   and item.arguments.get("operation") == "start"]
        if len(parents) != 1:
            return None
        bindings = [item for item in self._replay_actions.values()
                    if item.task_id == authorization.task_id
                    and item.parent_tool_call_id == authorization.tool_call_id
                    and item.replay_run_id == authorization.replay_run_id
                    and item.continuation_id == authorization.continuation_id
                    and item.workflow_id == authorization.workflow_id
                    and item.step_id == authorization.step_id
                    and item.action_digest == authorization.action_digest]
        if len(bindings) != 1:
            return None
        binding = bindings[0]
        now = self._now()
        if (not binding.authorization_issued or binding.status != ApprovalStatus.APPROVED
                or now >= binding.expires_at or authorization.expires_at <= now
                or authorization.issued_at > now or authorization.approval_id != binding.approval_id
                or (binding.approval_id is not None and authorization.decision != "approved")
                or (binding.approval_id is None and authorization.decision != "allowed_without_approval")):
            return None
        return binding

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
            replay_binding = self._replay_approval_ids.get(approval_key)
            if replay_binding is not None:
                return self._resolve_replay_approval_locked(task, pending, replay_binding, resolved_status)
            if self._approval_expired(pending):
                pending.status = ApprovalStatus.CANCELLED
                self._approval_history.setdefault(key, []).append(pending.model_copy(deep=True))
                task.pending_approvals = [item for item in task.pending_approvals if item.approval_id != approval_key]
                step = next((item for item in task.plan if item.step_id == pending.step_id), None)
                if step is not None and step.status == StepStatus.AWAITING_APPROVAL:
                    step.status = StepStatus.PENDING
                task.status = TaskStatus.CREATED
                task.error = None
                self.event(task, "approval.expired", EventStatus.NEEDS_ATTENTION, "Approval expired; a fresh approval is required", pending.step_id)
                self.save(task)
                raise InvalidApprovalError("Approval has expired; request a fresh approval")
            step = next((item for item in task.plan if item.step_id == pending.step_id), None)
            if step is None or step.tool_name != pending.tool_name or step.input != pending.arguments:
                raise InvalidApprovalError("Pending approval no longer matches its planned step and arguments")
            pending.status = resolved_status
            self._approval_history.setdefault(key, []).append(pending.model_copy(deep=True))
            task.pending_approvals = [item for item in task.pending_approvals if item.approval_id != approval_key]
            if resolved_status == ApprovalStatus.APPROVED:
                step.status = StepStatus.PENDING
                # Approval records consent for this exact action, but is not an
                # execution request. Return to the planned state so the caller
                # must explicitly invoke the existing execute endpoint.
                task.status = TaskStatus.CREATED
                task.error = None
                event_status = EventStatus.CREATED
                message = "Approval recorded; explicit task execution is still required"
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

    def _resolve_replay_approval_locked(
        self,
        task: TaskResponse,
        pending: ApprovalRequest,
        binding: _ReplayApprovalBinding,
        decision: ApprovalStatus,
    ) -> TaskResponse:
        step = next((item for item in task.plan if item.step_id == pending.step_id), None)
        parents = [item for item in self._tool_calls.get(task.task_id, [])
                   if item.tool_call_id == binding.parent_tool_call_id
                   and item.task_id == binding.task_id and item.tool_name == "browser.replay"]
        parent = parents[0] if len(parents) == 1 else None
        if (binding.task_id != task.task_id or binding.approval_id != pending.approval_id
                or binding.status != ApprovalStatus.PENDING or step is None or parent is None
                or step.step_id != parent.step_id or step.tool_name != "browser.replay"
                or step.input != parent.arguments or pending.tool_name != "browser.replay"
                or pending.arguments != {}
                or pending.description != "A browser workflow action requires approval."):
            raise InvalidApprovalError("Replay approval no longer matches its trusted action")
        if self._now() >= binding.expires_at:
            self._expire_replay_approval_locked(task, step, binding)
            raise InvalidApprovalError("Replay approval has expired")
        if task.status == TaskStatus.CANCELLED:
            binding.status = ApprovalStatus.CANCELLED
            raise InvalidApprovalError("Replay approval is no longer active")

        pending.status = decision
        self._approval_history.setdefault(task.task_id, []).append(pending.model_copy(deep=True))
        task.pending_approvals = [item for item in task.pending_approvals if item.approval_id != pending.approval_id]
        binding.status = decision
        self._replay_approval_ids.pop(pending.approval_id, None)
        if decision == ApprovalStatus.APPROVED:
            step.status = StepStatus.PENDING
            # Approval is consent only. The user must still explicitly run the plan.
            task.status = TaskStatus.CREATED
            task.error = None
            event_type, event_status = "approval.approved", EventStatus.CREATED
            message = "Browser workflow action approved; explicitly run the plan to continue"
        else:
            step.status = StepStatus.PENDING
            binding.exact_arguments = {}
            task.status = TaskStatus.NEEDS_ATTENTION
            task.error = "The browser workflow action was rejected"
            event_type, event_status = "approval.rejected", EventStatus.NEEDS_ATTENTION
            message = "Browser workflow action rejected; replay was not continued"
        self.event(task, event_type, event_status, message, pending.step_id)
        return self.save(task)

    def _expire_replay_approval_locked(self, task: TaskResponse, step: TaskStep, binding: _ReplayApprovalBinding) -> None:
        approval = None if binding.approval_id is None else next(
            (item for item in task.pending_approvals if item.approval_id == binding.approval_id), None
        )
        if approval is not None:
            approval.status = ApprovalStatus.CANCELLED
            self._approval_history.setdefault(task.task_id, []).append(approval.model_copy(deep=True))
            task.pending_approvals = [item for item in task.pending_approvals if item.approval_id != binding.approval_id]
        binding.status = ApprovalStatus.CANCELLED
        binding.authorization_issued = False
        binding.exact_arguments = {}
        step.status = StepStatus.PENDING
        task.status = TaskStatus.NEEDS_ATTENTION
        task.error = "A browser workflow approval expired"
        self.event(task, "approval.expired", EventStatus.NEEDS_ATTENTION,
                   "Browser workflow approval expired before it was used", step.step_id)
        self._save(task, public=False)

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("TaskManager clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)

    def approval_history(self, task_id: UUID | str) -> list[ApprovalRequest]:
        key = normalize_task_id(task_id)
        with self._lock:
            approvals = [approval.model_copy(deep=True) for approval in self._approval_history.get(key, [])]
        for approval in approvals:
            if approval.tool_name == "browser.replay":
                approval.arguments = {}
                approval.description = "Browser workflow action approval decision."
            elif approval.tool_name == "browser.fill" and "value" in approval.arguments:
                approval.arguments["value"] = "[REDACTED]"
            else:
                approval.arguments = self._redact_sensitive_mapping(approval.arguments)
        return approvals

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
                    if self._approval_expired(approval):
                        approval.status = ApprovalStatus.CANCELLED
                        task = self.repository.get(key)
                        self.event(task, "approval.expired", EventStatus.NEEDS_ATTENTION, "Approval expired before execution; a fresh approval is required", approval.step_id)
                        self.save(task)
                        continue
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
                        and not self._approval_expired(item)
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

    def _approval_expired(self, approval: ApprovalRequest) -> bool:
        return utc_now() - approval.created_at >= self._approval_ttl

    def save(self, task: TaskResponse) -> TaskResponse:
        """Persist state and return a public-safe task snapshot."""
        return self._save(task, public=True)

    def save_for_execution(self, task: TaskResponse) -> TaskResponse:
        """Persist and return private state for trusted executor operations."""
        return self._save(task, public=False)

    def _save(self, task: TaskResponse, *, public: bool) -> TaskResponse:
        with self._lock:
            current = self.repository.get(task.task_id)
            if current.status == TaskStatus.CANCELLED and task.status != TaskStatus.CANCELLED:
                task.status = TaskStatus.CANCELLED
            task.updated_at = utc_now()
            saved = self.repository.save(task)
        self.notify_task_terminal(task.task_id)
        return self._public_task(saved) if public else saved

    def get(self, task_id: UUID | str) -> TaskResponse:
        return self._public_task(self.repository.get(task_id))

    def get_for_execution(self, task_id: UUID | str) -> TaskResponse:
        return self.repository.get(task_id)

    @staticmethod
    def _public_task(task: TaskResponse) -> TaskResponse:
        public = task.model_copy(deep=True)
        for step in public.plan:
            if step.tool_name == "browser.replay":
                parameters = step.input.get("parameters")
                if isinstance(parameters, dict):
                    step.input["parameters"] = {str(key): "[REDACTED]" for key in parameters}
                if isinstance(step.verification, dict) and "tool_result" in step.verification:
                    step.verification["tool_result"] = TaskManager._public_replay_result(step.verification["tool_result"])
            elif step.tool_name == "browser.fill" and "value" in step.input:
                step.input["value"] = "[REDACTED]"
        for approval in public.pending_approvals:
            if approval.tool_name == "browser.replay":
                approval.arguments = {}
                approval.description = "A browser workflow action requires approval."
            elif approval.tool_name == "browser.fill" and "value" in approval.arguments:
                approval.arguments["value"] = "[REDACTED]"
            # Exact file paths are part of the user's local-file approval
            # decision and must remain visible so the user can confirm the
            # requested target. Other sensitive fields remain redacted.
            approval.arguments = TaskManager._redact_sensitive_mapping(
                approval.arguments,
                redact_paths=approval.tool_name != "document.read_local_file",
            )
        if isinstance(public.result, dict):
            public.result = TaskManager._redact_sensitive_mapping(public.result)
        for evidence in public.evidence:
            if evidence.file_path:
                evidence.file_path = "[REDACTED]"
        public.artifacts = TaskManager._redact_sensitive_mapping(public.artifacts)
        return public

    @staticmethod
    def _public_replay_result(value):
        if not isinstance(value, dict):
            return {"status": "unavailable"}
        result = {"status": value["status"]} if isinstance(value.get("status"), str) else {}
        pending = value.get("pending_action")
        if isinstance(pending, dict):
            result["pending_action"] = {key: pending[key] for key in ("step_id", "action") if isinstance(pending.get(key), str)}
        if isinstance(value.get("error"), str):
            result["error"] = value["error"]
        checkpoints = value.get("checkpoints")
        if isinstance(checkpoints, list):
            result["checkpoints"] = [
                {key: item[key] for key in ("step_id", "action", "state") if key in item}
                for item in checkpoints if isinstance(item, dict)
            ]
        return result

    @staticmethod
    def _redact_sensitive_mapping(value, *, redact_paths: bool = True):
        sensitive = {
            "password", "passwd", "secret", "token", "authorization", "credential", "cookie",
            "session", "one_time_token", "exact_arguments", "resolved_parameters", "continuation",
            "session_id", "replay_run_id", "workflow_digest", "action_digest",
        }
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                normalized = str(key).casefold()
                if normalized in sensitive or (redact_paths and normalized in {"file_path", "path"}):
                    result[str(key)] = "[REDACTED]"
                elif normalized == "tool_result":
                    is_replay_result = (
                        isinstance(child, dict)
                        and (
                            {"workflow_id", "replay_run_id", "session_id"}.issubset(child)
                            or any(name in child for name in ("pending_action", "checkpoints", "resolved_parameters", "continuation"))
                        )
                    )
                    has_page_observation = (
                        isinstance(child, dict)
                        and isinstance(child.get("page_observation"), dict)
                        and child["page_observation"].get("content_trust") == "untrusted_webpage_data"
                    )
                    if is_replay_result:
                        result[str(key)] = TaskManager._public_replay_result(child)
                    elif has_page_observation:
                        result[str(key)] = TaskManager._public_browser_observation_result(child)
                    else:
                        # Other tool outputs, including document-analysis data,
                        # remain private unless a feature defines an explicit
                        # public result contract.
                        result[str(key)] = {}
                elif normalized == "file_ref" and isinstance(child, str) and (Path(child).is_absolute() or ntpath.isabs(child) or "/" in child or "\\" in child):
                    result[str(key)] = "[REDACTED]"
                else:
                    result[str(key)] = TaskManager._redact_sensitive_mapping(child, redact_paths=redact_paths)
            return result
        if isinstance(value, list):
            return [TaskManager._redact_sensitive_mapping(item, redact_paths=redact_paths) for item in value]
        return value

    @staticmethod
    def _public_browser_observation_result(value: dict) -> dict:
        """Expose only the bounded observation contract to task clients."""
        from urllib.parse import urlsplit, urlunsplit

        observation = value.get("page_observation")
        if not isinstance(observation, dict):
            return {}
        url = observation.get("url")
        safe_url = ""
        if isinstance(url, str):
            try:
                parts = urlsplit(url)
                if parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password:
                    safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))[:1024]
            except ValueError:
                pass
        public_observation = {
            "url": safe_url,
            "title": observation.get("title", "")[:300] if isinstance(observation.get("title"), str) else "",
            "visible_text": observation.get("visible_text", "")[:4000] if isinstance(observation.get("visible_text"), str) else "",
            "truncated": observation.get("truncated") is True,
            "content_trust": "untrusted_webpage_data",
        }
        result = {"page_observation": public_observation}
        for key in ("task_id", "navigated", "clicked"):
            if key in value and isinstance(value[key], (str, bool)):
                result[key] = value[key]
        return result

    def cancel(self, task_id: UUID | str) -> TaskResponse:
        key = normalize_task_id(task_id)
        with self.replay_dispatch_lock(key):
            with self._lock:
                task = self.repository.get(key)
                if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                    result = self._public_task(task)
                else:
                    task.status = TaskStatus.CANCELLED
                    for approval in task.pending_approvals:
                        if approval.status == ApprovalStatus.PENDING:
                            approval.status = ApprovalStatus.CANCELLED
                            self._approval_history.setdefault(key, []).append(approval.model_copy(deep=True))
                    for action_key, binding in list(self._replay_actions.items()):
                        if binding.task_id == key:
                            binding.status = ApprovalStatus.CANCELLED
                            binding.authorization_issued = False
                            binding.exact_arguments = {}
                            self._replay_actions.pop(action_key, None)
                            if binding.approval_id is not None:
                                self._replay_approval_ids.pop(binding.approval_id, None)
                    self._resume_approval_caps = {
                        auth_id: cap for auth_id, cap in self._resume_approval_caps.items() if cap[0] != key
                    }
                    task.pending_approvals = []
                    self.event(task, "task.cancelled", EventStatus.CANCELLED, "Task cancelled", task.current_step)
                    result = self._save(task, public=True)
        self.notify_task_terminal(key)
        return result
