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


@dataclass(repr=False)
class _ReplayApprovalBinding:
    """Private replay-action approval state; never attached to TaskResponse."""

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
        return (self.task_id, self.parent_tool_call_id, self.pending_action_id)


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
    def __init__(
        self,
        repository: InMemoryTaskRepository | None = None,
        *,
        replay_approval_ttl: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(replay_approval_ttl, timedelta) or replay_approval_ttl <= timedelta(0):
            raise ValueError("replay_approval_ttl must be positive")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.repository = repository or InMemoryTaskRepository()
        self._events: dict[UUID4, list[ExecutionEvent]] = {}
        self._missing_information: dict[UUID4, list[str]] = {}
        self._auto_execute: dict[UUID4, bool] = {}
        self._tool_calls: dict[UUID4, list[ToolCall]] = {}
        self._approval_history: dict[UUID4, list[ApprovalRequest]] = {}
        self._consumed_approvals: set[UUID4] = set()
        self._replay_actions: dict[tuple[UUID, UUID, str], _ReplayApprovalBinding] = {}
        self._replay_approval_ids: dict[UUID, _ReplayApprovalBinding] = {}
        self._replay_approval_ttl = replay_approval_ttl
        self._clock = clock
        self._execution_locks: dict[UUID4, Lock] = {}
        self._replay_dispatch_locks: dict[UUID4, Lock] = {}
        self._terminal_callbacks: list[Callable[[UUID4, TaskStatus], None]] = []
        self._resume_approval_caps: dict[UUID, tuple[UUID, UUID]] = {}
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
        """Serialize cancellation against one replay dispatch and its checkpoint."""
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

    def register_terminal_callback(
        self, callback: Callable[[UUID4, TaskStatus], None]
    ) -> None:
        if not callable(callback):
            raise TypeError("terminal callback must be callable")
        with self._lock:
            if callback not in self._terminal_callbacks:
                self._terminal_callbacks.append(callback)

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
                # Lifecycle cleanup must not leak browser diagnostics or alter task state.
                continue

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
        self.repository.save(task)
        return self._public_task(task)

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

    def replay_authorization_decision(self, pending_action, policy_context):
        """Create/reuse a replay approval through this manager's normal lifecycle.

        The bridge calls this with trusted browser-local models. The first call
        either creates one redacted ``ApprovalRequest`` in ``TaskResponse`` or
        issues an internal no-approval decision for a policy-classified safe
        action. An approved record yields one decision exactly once.
        """
        # Local imports avoid a module cycle: the bridge depends on TaskManager.
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

        task_id = policy_context.task_id
        with self._lock:
            try:
                task = self.repository.get(task_id)
            except (KeyError, ValueError):
                return None
            if task.status in {
                TaskStatus.CANCELLED,
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.NEEDS_ATTENTION,
            }:
                return None

            parent = self._find_original_replay_call_locked(policy_context)
            if parent is None:
                return None
            try:
                parent_workflow_id = UUID(str(parent.arguments.get("workflow_id")))
            except (TypeError, ValueError, AttributeError):
                return None
            if (
                parent_workflow_id != policy_context.workflow_id
                or parent.arguments.get("operation") != "start"
            ):
                return None
            step = next((item for item in task.plan if item.step_id == parent.step_id), None)
            if (
                step is None
                or step.tool_name != "browser.replay"
                or step.input != parent.arguments
                or step.status not in {StepStatus.PENDING, StepStatus.RUNNING, StepStatus.AWAITING_APPROVAL}
            ):
                return None

            pending_action_id = self._pending_action_identity(pending_action)
            action_key = (task_id, parent.tool_call_id, pending_action_id)
            existing = self._replay_actions.get(action_key)
            if existing is not None:
                if not self._replay_binding_matches(existing, pending_action, policy_context, arguments):
                    return None
                if self._now() >= existing.expires_at:
                    self._expire_replay_approval_locked(task, step, existing)
                    return None
                if existing.status == ApprovalStatus.PENDING:
                    return None
                if (
                    existing.status != ApprovalStatus.APPROVED
                    or existing.authorization_issued
                    or (existing.approval_id is not None and existing.approval_id not in {
                        item.approval_id for item in self._approval_history.get(task_id, [])
                        if item.status == ApprovalStatus.APPROVED
                    })
                ):
                    return None
                existing.authorization_issued = True
                existing.exact_arguments = {}
                decision = "approved" if existing.approval_id is not None else "allowed_without_approval"
                return ReplayAuthorizationDecision(decision, approval_id=existing.approval_id)

            try:
                tool_name = {
                    "navigate": "browser.navigate",
                    "click": "browser.click",
                    "fill": "browser.fill",
                    "select_option": "browser.fill",
                }[policy_context.action]
                requires_approval = action_requires_approval(
                    tool_name=tool_name,
                    description=policy_context.action,
                    arguments=arguments,
                )
                now = self._now()
            except (KeyError, TypeError, ValueError):
                return None

            approval_id = uuid4() if requires_approval else None
            binding = _ReplayApprovalBinding(
                task_id=task_id,
                parent_tool_call_id=parent.tool_call_id,
                continuation_id=policy_context.continuation_id,
                workflow_id=policy_context.workflow_id,
                replay_run_id=policy_context.replay_run_id,
                pending_action_id=pending_action_id,
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
                approval = ApprovalRequest(
                    approval_id=approval_id,
                    task_id=task_id,
                    step_id=parent.step_id,
                    tool_name="browser.replay",
                    description="A browser workflow action requires approval.",
                    arguments={},
                )
                task.pending_approvals.append(approval)
                task.current_step = parent.step_id
                task.status = TaskStatus.AWAITING_APPROVAL
                step.status = StepStatus.AWAITING_APPROVAL
                self._replay_approval_ids[approval_id] = binding
                self.event(
                    task,
                    "approval.required",
                    EventStatus.AWAITING_APPROVAL,
                    "Approval required before the next browser workflow action",
                    parent.step_id,
                )
                self.save(task)
                return None

            binding.authorization_issued = True
            binding.exact_arguments = {}
            return ReplayAuthorizationDecision("allowed_without_approval")

    @staticmethod
    def _replay_inputs_match(pending_action, policy_context) -> bool:
        return (
            pending_action.replay_run_id == policy_context.replay_run_id
            and pending_action.continuation_id == policy_context.continuation_id
            and pending_action.workflow_id == policy_context.workflow_id
            and pending_action.step_id == policy_context.step_id
            and pending_action.action == policy_context.action
            and pending_action.action_digest == policy_context.action_digest
            and pending_action.sequence_index == policy_context.sequence_index
        )

    @staticmethod
    def _pending_action_identity(pending_action) -> str:
        return (
            f"{pending_action.replay_run_id}:{pending_action.continuation_id}:"
            f"{pending_action.sequence_index}:{pending_action.step_id}:{pending_action.action_digest}"
        )

    def _find_original_replay_call_locked(self, policy_context) -> ToolCall | None:
        calls = [
            call for call in self._tool_calls.get(policy_context.task_id, [])
            if call.tool_call_id == policy_context.tool_call_id
            and call.task_id == policy_context.task_id
            and call.tool_name == "browser.replay"
        ]
        return calls[0] if len(calls) == 1 else None

    @staticmethod
    def _replay_binding_matches(binding, pending_action, policy_context, arguments: dict) -> bool:
        return (
            binding.task_id == policy_context.task_id
            and binding.parent_tool_call_id == policy_context.tool_call_id
            and binding.continuation_id == policy_context.continuation_id
            and binding.workflow_id == policy_context.workflow_id
            and binding.replay_run_id == policy_context.replay_run_id
            and binding.pending_action_id == TaskManager._pending_action_identity(pending_action)
            and binding.step_id == policy_context.step_id
            and binding.action == policy_context.action
            and binding.action_digest == policy_context.action_digest
            and binding.exact_arguments == arguments
        )

    def replay_resume_approval(self, call: ToolCall, authorization):
        """Return a private registry-compatible approval for a trusted resume.

        This is not a new approval decision: it is an ephemeral capability derived
        only from an already issued TaskManager replay authorization.
        """
        from backend.app.browser.replay_state import ReplayStepAuthorization

        if not isinstance(call, ToolCall) or not isinstance(authorization, ReplayStepAuthorization):
            return None
        with self._lock:
            binding = self._binding_for_authorization_locked(call, authorization)
            if binding is None or binding.resume_claimed or authorization.authorization_id in self._resume_approval_caps:
                return None
            capability_id = uuid4()
            self._resume_approval_caps[authorization.authorization_id] = (call.task_id, capability_id)
            return ApprovalRequest(
                approval_id=capability_id,
                task_id=call.task_id,
                step_id=call.step_id,
                tool_name=call.tool_name,
                description="Trusted one-action replay continuation",
                arguments=call.arguments,
                status=ApprovalStatus.APPROVED,
            )

    def claim_replay_resume(self, call: ToolCall, authorization, registry_approval: ApprovalRequest) -> bool:
        """Atomically consume the internal registry capability for one resume."""
        from backend.app.browser.replay_state import ReplayStepAuthorization

        if (
            not isinstance(call, ToolCall)
            or not isinstance(authorization, ReplayStepAuthorization)
            or not isinstance(registry_approval, ApprovalRequest)
        ):
            return False
        with self._lock:
            binding = self._binding_for_authorization_locked(call, authorization)
            cap_record = self._resume_approval_caps.get(authorization.authorization_id)
            expected_cap = None if cap_record is None else cap_record[1]
            if (
                binding is None
                or binding.resume_claimed
                or expected_cap is None
                or registry_approval.approval_id != expected_cap
                or registry_approval.status != ApprovalStatus.APPROVED
                or registry_approval.task_id != call.task_id
                or registry_approval.step_id != call.step_id
                or registry_approval.tool_name != call.tool_name
                or registry_approval.arguments != call.arguments
            ):
                return False
            try:
                task = self.repository.get(call.task_id)
            except (KeyError, ValueError):
                return False
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
        if (
            call.tool_name != "browser.replay"
            or call.arguments != {"operation": "resume"}
            or call.task_id != authorization.task_id
            or call.tool_call_id != authorization.tool_call_id
        ):
            return None
        parents = [
            item for item in self._tool_calls.get(call.task_id, [])
            if item.task_id == call.task_id
            and item.tool_call_id == call.tool_call_id
            and item.tool_name == "browser.replay"
            and item.step_id == call.step_id
            and item.arguments.get("operation") == "start"
        ]
        if len(parents) != 1:
            return None
        bindings = [
            item for item in self._replay_actions.values()
            if item.task_id == authorization.task_id
            and item.parent_tool_call_id == authorization.tool_call_id
            and item.replay_run_id == authorization.replay_run_id
            and item.continuation_id == authorization.continuation_id
            and item.workflow_id == authorization.workflow_id
            and item.step_id == authorization.step_id
            and item.action_digest == authorization.action_digest
        ]
        if len(bindings) != 1:
            return None
        binding = bindings[0]
        try:
            now = self._now()
        except Exception:
            return None
        if (
            not binding.authorization_issued
            or binding.status != ApprovalStatus.APPROVED
            or now >= binding.expires_at
            or authorization.expires_at <= now
            or authorization.issued_at > now
            or authorization.approval_id != binding.approval_id
            or (binding.approval_id is not None and authorization.decision != "approved")
            or (binding.approval_id is None and authorization.decision != "allowed_without_approval")
        ):
            return None
        return binding

    def _expire_replay_approval_locked(self, task: TaskResponse, step: TaskStep, binding) -> None:
        if binding.status == ApprovalStatus.CANCELLED:
            return
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
        self.event(
            task,
            "approval.expired",
            EventStatus.NEEDS_ATTENTION,
            "Browser workflow approval expired before it was used",
            step.step_id,
        )
        self.save(task)

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("TaskManager clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)

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
                return self._resolve_replay_approval_locked(
                    task,
                    pending,
                    replay_binding,
                    resolved_status,
                )
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

    def _resolve_replay_approval_locked(
        self,
        task: TaskResponse,
        pending: ApprovalRequest,
        binding: _ReplayApprovalBinding,
        decision: ApprovalStatus,
    ) -> TaskResponse:
        """Resolve a replay action through the shared approval state transition."""
        step = next((item for item in task.plan if item.step_id == pending.step_id), None)
        parents = [
                call for call in self._tool_calls.get(task.task_id, [])
                if call.tool_call_id == binding.parent_tool_call_id
                and call.task_id == binding.task_id
                and call.tool_name == "browser.replay"
        ]
        parent = parents[0] if len(parents) == 1 else None
        if (
            binding.task_id != task.task_id
            or binding.approval_id != pending.approval_id
            or binding.status != ApprovalStatus.PENDING
            or step is None
            or parent is None
            or step.step_id != parent.step_id
            or step.tool_name != "browser.replay"
            or step.input != parent.arguments
            or pending.step_id != parent.step_id
            or pending.tool_name != "browser.replay"
            or pending.description != "A browser workflow action requires approval."
            or pending.arguments != {}
        ):
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
        if decision == ApprovalStatus.APPROVED:
            step.status = StepStatus.PENDING
            task.status = TaskStatus.EXECUTING
            task.error = None
            event_status = EventStatus.RUNNING
            event_type = "approval.approved"
            message = "Browser workflow action approval granted"
        else:
            step.status = StepStatus.SKIPPED
            step.error = "The browser workflow action was rejected"
            binding.exact_arguments = {}
            task.status = TaskStatus.NEEDS_ATTENTION
            task.error = step.error
            event_status = EventStatus.NEEDS_ATTENTION
            event_type = "approval.rejected"
            message = "Browser workflow action rejected; replay was not continued"
        self.event(task, event_type, event_status, message, pending.step_id)
        return self.save(task)

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
        """Persist task state and return a redacted user-facing copy."""
        return self._save(task, public=True)

    def save_for_execution(self, task: TaskResponse) -> TaskResponse:
        """Persist and return the private task model for trusted executor code."""
        return self._save(task, public=False)

    def _save(self, task: TaskResponse, *, public: bool) -> TaskResponse:
        with self._lock:
            current = self.repository.get(task.task_id)
            if current.status == TaskStatus.CANCELLED and task.status != TaskStatus.CANCELLED:
                task.status = TaskStatus.CANCELLED
            task.updated_at = utc_now()
            saved = self.repository.save(task)
            return self._public_task(saved) if public else saved

    def get(self, task_id: UUID | str) -> TaskResponse:
        return self._public_task(self.repository.get(task_id))

    def get_for_execution(self, task_id: UUID | str) -> TaskResponse:
        """Read private task data for trusted executor internals only."""
        return self.repository.get(task_id)

    @staticmethod
    def _public_task(task: TaskResponse) -> TaskResponse:
        """Remove replay inputs and continuation internals from public copies."""
        public = task.model_copy(deep=True)
        for step in public.plan:
            if step.tool_name == "browser.replay":
                parameters = step.input.get("parameters")
                if isinstance(parameters, dict):
                    step.input["parameters"] = {str(key): "[REDACTED]" for key in parameters}
                if isinstance(step.verification, dict) and "tool_result" in step.verification:
                    raw = step.verification.get("tool_result")
                    step.verification["tool_result"] = TaskManager._public_replay_result(raw)
            elif step.tool_name == "browser.fill" and "value" in step.input:
                step.input["value"] = "[REDACTED]"
        for approval in public.pending_approvals:
            if approval.tool_name == "browser.replay":
                approval.arguments = {}
                approval.description = "A browser workflow action requires approval."
            elif approval.tool_name == "browser.fill" and "value" in approval.arguments:
                approval.arguments["value"] = "[REDACTED]"
        for approval in public.pending_approvals:
            approval.arguments = TaskManager._redact_sensitive_mapping(approval.arguments)
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
        result = {"status": value.get("status")} if isinstance(value.get("status"), str) else {}
        pending = value.get("pending_action")
        if isinstance(pending, dict):
            result["pending_action"] = {
                key: pending[key] for key in ("step_id", "action") if isinstance(pending.get(key), str)
            }
        error = value.get("error")
        if isinstance(error, str):
            result["error"] = error
        checkpoints = value.get("checkpoints")
        if isinstance(checkpoints, list):
            result["checkpoints"] = [
                {key: item[key] for key in ("step_id", "action", "state") if key in item}
                for item in checkpoints if isinstance(item, dict)
            ]
        return result

    @staticmethod
    def _redact_sensitive_mapping(value):
        sensitive_names = {
            "password", "passwd", "secret", "token", "authorization", "credential",
            "cookie", "session", "one_time_token", "exact_arguments", "resolved_parameters",
            "continuation", "session_id", "replay_run_id", "workflow_digest", "action_digest",
            "file_path", "path",
        }
        if isinstance(value, dict):
            redacted = {}
            for key, child in value.items():
                normalized_key = str(key).casefold()
                if normalized_key in sensitive_names:
                    redacted[str(key)] = "[REDACTED]"
                elif normalized_key == "tool_result":
                    redacted[str(key)] = TaskManager._public_replay_result(child)
                elif normalized_key == "file_ref" and isinstance(child, str) and (
                    Path(child).is_absolute() or ntpath.isabs(child) or "/" in child or "\\" in child
                ):
                    redacted[str(key)] = "[REDACTED]"
                else:
                    redacted[str(key)] = TaskManager._redact_sensitive_mapping(child)
            return redacted
        if isinstance(value, list):
            return [TaskManager._redact_sensitive_mapping(item) for item in value]
        return value

    def cancel(self, task_id: UUID | str) -> TaskResponse:
        key = normalize_task_id(task_id)
        # Lock ordering is replay gate, then manager state lock. Dispatch holds
        # the same gate through browser side effects and durable outcome writes.
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
                        auth_id: cap for auth_id, cap in self._resume_approval_caps.items()
                        if cap[0] != key
                    }
                    task.pending_approvals = []
                    self.event(task, "task.cancelled", EventStatus.CANCELLED, "Task cancelled", task.current_step)
                    result = self._save(task, public=True)
        self.notify_task_terminal(key)
        return result
