"""Trusted orchestration bridge for browser replay continuations.

This module adapts the browser engine's policy handoff and resume-provider
protocols to task-owned runtime state. It never decides whether an action needs
approval. TaskManager owns approval creation, resolution and one-use decisions.
Authorization issuance fails closed whenever the manager has no active decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import secrets
from threading import RLock
from typing import Literal, Protocol
from uuid import UUID, uuid4

from backend.app.browser.replay import CancellationChecker, ReplayPolicyHandoff
from backend.app.browser.replay_state import (
    ReplayContinuation,
    ReplayPendingAction,
    ReplayPolicyContext,
    ReplayStepAuthorization,
)
from backend.app.browser.replay_state_store import ReplayStateStore
from backend.app.browser.session_manager import BrowserSessionManager
from backend.app.browser.tools import (
    ReplayResumeContextProvider,
    TrustedReplayResumeContext,
)
from backend.app.browser.workflow_store import WorkflowStore
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.schemas.contracts import TaskStatus, ToolCall


class BrowserReplayBridgeError(RuntimeError):
    """Safe failure at the browser/orchestrator trust boundary."""


@dataclass(frozen=True)
class ReplayAuthorizationDecision:
    """Internal TaskManager decision, never accepted from a tool/API payload."""

    decision: Literal["approved", "allowed_without_approval"]
    approval_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.decision == "approved" and self.approval_id is None:
            raise ValueError("Approved replay authorization requires a manager approval ID")
        if self.decision == "allowed_without_approval" and self.approval_id is not None:
            raise ValueError("No-approval replay authorization cannot contain an approval ID")


class ReplayDecisionSource(Protocol):
    """TaskManager-owned replay decision hook."""

    def replay_authorization_decision(
        self,
        pending_action: ReplayPendingAction,
        policy_context: ReplayPolicyContext,
    ) -> ReplayAuthorizationDecision | None: ...


@dataclass(frozen=True)
class _PendingReplay:
    pending_action: ReplayPendingAction
    policy_context: ReplayPolicyContext = field(repr=False)
    workflow_digest: str
    continuation: ReplayContinuation
    parent_tool_call: ToolCall = field(repr=False)
    authorization: ReplayStepAuthorization | None = field(default=None, repr=False)


class BrowserReplayOrchestratorBridge(
    ReplayPolicyHandoff,
    ReplayResumeContextProvider,
    CancellationChecker,
):
    """Handoff, resume-context, cancellation, and trusted auth adapter.

    The bridge keeps orchestration associations process-local. A restart that
    loses task/call/approval state cannot resume from planner arguments; it
    fails closed and leaves durable replay state for attention/recovery policy.
    """

    def __init__(
        self,
        task_manager: TaskManager,
        workflow_store: WorkflowStore,
        session_manager: BrowserSessionManager,
        *,
        state_store: ReplayStateStore | None = None,
        authorization_ttl: timedelta = timedelta(minutes=2),
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(authorization_ttl, timedelta) or authorization_ttl <= timedelta(0):
            raise ValueError("authorization_ttl must be positive")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.task_manager = task_manager
        self.workflow_store = workflow_store
        self.session_manager = session_manager
        self.state_store = state_store
        self.authorization_ttl = authorization_ttl
        self._clock = clock
        self._pending_by_task: dict[UUID, _PendingReplay] = {}
        self._issued: dict[UUID, ReplayStepAuthorization] = {}
        self._lock = RLock()
        self._publish_lock = RLock()

    def attach_state_store(self, state_store: ReplayStateStore) -> None:
        """Attach the store after construction so it can use this auth validator."""
        if not isinstance(state_store, ReplayStateStore):
            raise TypeError("state_store must be a ReplayStateStore")
        with self._lock:
            if self.state_store is not None and self.state_store is not state_store:
                raise BrowserReplayBridgeError("Replay state store is already attached")
            self.state_store = state_store

    def publish(
        self,
        pending_action: ReplayPendingAction,
        policy_context: ReplayPolicyContext,
        *,
        workflow_digest: str,
        explanation: str,
    ) -> None:
        """Record only a pending action that matches its durable checkpoint."""
        del explanation  # UI-safe explanation policy remains orchestration-owned.
        self._assert_pending_binding(pending_action, policy_context)
        # Serialize checkpoint reads with publication. Otherwise an older
        # publisher can validate first, pause, and overwrite a newer cursor.
        with self._publish_lock:
            try:
                workflow = self.workflow_store.load(policy_context.workflow_id)
                state_store = self._require_store()
                snapshot = state_store.load_result(
                    pending_action.replay_run_id,
                    current_workflow=workflow,
                )
                parent = self.recover_parent_tool_call(policy_context)
                durable_context = state_store.get_pending_policy_context(
                    snapshot.continuation,
                    tool_call=parent,
                    current_workflow=workflow,
                )
            except Exception:
                raise BrowserReplayBridgeError("Replay checkpoint could not be validated") from None

            continuation = snapshot.continuation
            if (
                snapshot.status != "pending_action"
                or snapshot.pending_action != pending_action
                or continuation is None
                or continuation.task_id != policy_context.task_id
                or continuation.workflow_id != policy_context.workflow_id
                or continuation.continuation_id != policy_context.continuation_id
                or continuation.replay_run_id != policy_context.replay_run_id
                or continuation.workflow_digest != workflow_digest
                or policy_context.sequence_index != continuation.next_action_index
                or durable_context != policy_context
            ):
                raise BrowserReplayBridgeError("Pending replay action does not match its durable checkpoint")

            pending = _PendingReplay(
                pending_action=pending_action,
                policy_context=policy_context.model_copy(deep=True),
                workflow_digest=workflow_digest,
                continuation=continuation,
                parent_tool_call=parent,
            )
            task_id = policy_context.task_id
            with self._lock:
                previous = self._pending_by_task.get(task_id)
                if previous is not None and previous.pending_action.replay_run_id != pending_action.replay_run_id:
                    raise BrowserReplayBridgeError("Another browser replay is already active for this task")
                if previous is not None and self._is_older_cursor(previous.continuation, continuation):
                    raise BrowserReplayBridgeError("Replay checkpoint is older than the active cursor")
                # Re-publishing the same durable cursor is idempotent and retains a
                # currently issued permit. A later cursor invalidates that permit.
                if previous is not None and previous.continuation == continuation:
                    pending = _PendingReplay(
                        pending_action=pending_action,
                        policy_context=policy_context.model_copy(deep=True),
                        workflow_digest=workflow_digest,
                        continuation=continuation,
                        parent_tool_call=parent,
                        authorization=previous.authorization,
                    )
                elif previous is not None and previous.authorization is not None:
                    self._issued.pop(previous.authorization.authorization_id, None)
                self._pending_by_task[task_id] = pending

    def recover_parent_tool_call(self, policy_context: ReplayPolicyContext) -> ToolCall:
        """Recover the exact original replay call recorded before adapter invoke."""
        try:
            calls = self.task_manager.tool_calls(policy_context.task_id)
        except Exception:
            raise BrowserReplayBridgeError("Original replay ToolCall is unavailable") from None
        matches = [
            call for call in calls
            if call.tool_call_id == policy_context.tool_call_id
            and call.task_id == policy_context.task_id
            and call.tool_name == "browser.replay"
        ]
        if len(matches) != 1:
            raise BrowserReplayBridgeError("Original replay ToolCall could not be recovered")
        return matches[0]

    def issue_authorization(
        self,
        pending_action: ReplayPendingAction,
        policy_context: ReplayPolicyContext,
    ) -> ReplayStepAuthorization | None:
        """Materialize a TaskManager decision; never creates or decides approval."""
        pending = self._matching_pending(pending_action, policy_context)
        decision_method = getattr(self.task_manager, "replay_authorization_decision", None)
        if not callable(decision_method):
            # No fallback permit if the authoritative manager hook is absent.
            return None
        try:
            decision = decision_method(pending_action, policy_context)
        except Exception:
            return None
        if not isinstance(decision, ReplayAuthorizationDecision):
            return None

        now = self._now()
        authorization = ReplayStepAuthorization(
            authorization_id=uuid4(),
            task_id=policy_context.task_id,
            tool_call_id=policy_context.tool_call_id,
            replay_run_id=policy_context.replay_run_id,
            continuation_id=policy_context.continuation_id,
            workflow_id=policy_context.workflow_id,
            step_id=policy_context.step_id,
            action_digest=policy_context.action_digest,
            decision=decision.decision,
            approval_id=decision.approval_id,
            issued_at=now,
            expires_at=now + self.authorization_ttl,
            one_time_token=secrets.token_urlsafe(32),
        )
        with self._lock:
            current = self._pending_by_task.get(policy_context.task_id)
            if current is None or current.continuation != pending.continuation:
                return None
            current = _PendingReplay(
                pending_action=current.pending_action,
                policy_context=current.policy_context,
                workflow_digest=current.workflow_digest,
                continuation=current.continuation,
                parent_tool_call=current.parent_tool_call,
                authorization=authorization,
            )
            self._pending_by_task[policy_context.task_id] = current
            self._issued[authorization.authorization_id] = authorization
        return authorization

    def pending_for_task(
        self,
        task_id: UUID,
    ) -> tuple[ReplayPendingAction, ReplayPolicyContext] | None:
        """Return the internal policy handoff to trusted executor code only."""
        with self._lock:
            pending = self._pending_by_task.get(task_id)
        if pending is None or self.is_cancelled(task_id):
            return None
        return pending.pending_action, pending.policy_context

    def resolve(self, tool_call: ToolCall) -> TrustedReplayResumeContext | None:
        """Return private resume data; continuation/auth fields are never inputs."""
        if (
            not isinstance(tool_call, ToolCall)
            or tool_call.tool_name != "browser.replay"
            or tool_call.arguments.get("operation") != "resume"
        ):
            return None
        with self._lock:
            pending = self._pending_by_task.get(tool_call.task_id)
        if (
            pending is None
            or pending.authorization is None
            or tool_call.task_id != pending.parent_tool_call.task_id
            or tool_call.step_id != pending.parent_tool_call.step_id
            or tool_call.tool_call_id != pending.parent_tool_call.tool_call_id
            or self.is_cancelled(tool_call.task_id)
        ):
            return None
        try:
            recovered_parent = self.recover_parent_tool_call(pending.policy_context)
            workflow = self.workflow_store.load(pending.policy_context.workflow_id)
            snapshot = self._require_store().load_result(
                pending.pending_action.replay_run_id,
                current_workflow=workflow,
            )
            durable_context = self._require_store().get_pending_policy_context(
                snapshot.continuation,
                tool_call=recovered_parent,
                current_workflow=workflow,
            )
        except Exception:
            return None
        if (
            recovered_parent != pending.parent_tool_call
            or snapshot.status != "pending_action"
            or snapshot.continuation != pending.continuation
            or snapshot.pending_action != pending.pending_action
            or durable_context != pending.policy_context
            or not self.validate_authorization(pending.authorization, recovered_parent)
        ):
            return None
        return TrustedReplayResumeContext(
            continuation=pending.continuation,
            authorization=pending.authorization,
            parent_tool_call=recovered_parent,
        )

    def validate_authorization(self, authorization: ReplayStepAuthorization, tool_call: ToolCall) -> bool:
        """Validate bridge-issued authorization; ReplayStateStore consumes it."""
        if not isinstance(authorization, ReplayStepAuthorization) or not isinstance(tool_call, ToolCall):
            return False
        with self._lock:
            issued = self._issued.get(authorization.authorization_id)
            pending = self._pending_by_task.get(authorization.task_id)
        if (
            issued is None
            or issued != authorization
            or pending is None
            or pending.authorization != authorization
            or pending.parent_tool_call != tool_call
            or tool_call.task_id != authorization.task_id
            or tool_call.tool_call_id != authorization.tool_call_id
            or tool_call.tool_name != "browser.replay"
            or pending.pending_action.replay_run_id != authorization.replay_run_id
            or pending.pending_action.continuation_id != authorization.continuation_id
            or pending.pending_action.workflow_id != authorization.workflow_id
            or pending.pending_action.step_id != authorization.step_id
            or pending.pending_action.action_digest != authorization.action_digest
        ):
            return False
        try:
            return self._now() < authorization.expires_at
        except Exception:
            return False

    def validate_session(self, task_id: UUID, session_id: UUID) -> bool:
        """Fail-closed session validator callback for ReplayStateStore."""
        try:
            session = self.session_manager.get_session(task_id)
            return session is not None and session.session_id == session_id and session.controller.is_started
        except Exception:
            return False

    def is_cancelled(self, task_id: UUID) -> bool:
        """Expose authoritative TaskManager cancellation as a replay checker."""
        try:
            return self.task_manager.get(task_id).status == TaskStatus.CANCELLED
        except Exception:
            # If task state is unavailable, replay must stop before dispatch.
            return True

    def forget_replay(self, task_id: UUID, replay_run_id: UUID) -> None:
        """Drop process-local associations after the executor handles a terminal result."""
        with self._lock:
            pending = self._pending_by_task.get(task_id)
            if pending is None or pending.pending_action.replay_run_id != replay_run_id:
                return
            self._pending_by_task.pop(task_id, None)
            self._issued = {
                auth_id: authorization
                for auth_id, authorization in self._issued.items()
                if authorization.replay_run_id != replay_run_id
            }

    def forget_task(self, task_id: UUID) -> None:
        """Clear process-local replay associations after terminal task cleanup."""
        with self._lock:
            pending = self._pending_by_task.pop(task_id, None)
            if pending is None:
                return
            self._issued = {
                auth_id: authorization
                for auth_id, authorization in self._issued.items()
                if authorization.task_id != task_id
            }

    def _matching_pending(
        self,
        pending_action: ReplayPendingAction,
        policy_context: ReplayPolicyContext,
    ) -> _PendingReplay:
        self._assert_pending_binding(pending_action, policy_context)
        with self._lock:
            pending = self._pending_by_task.get(policy_context.task_id)
        if (
            pending is None
            or pending.pending_action != pending_action
            or pending.policy_context != policy_context
        ):
            raise BrowserReplayBridgeError("Replay policy decision does not match the pending action")
        return pending

    @staticmethod
    def _is_older_cursor(current: ReplayContinuation, incoming: ReplayContinuation) -> bool:
        """Reject cursor rollback and conflicting continuations at one version."""
        return (
            incoming.checkpoint_version < current.checkpoint_version
            or incoming.next_action_index < current.next_action_index
            or (
                incoming.checkpoint_version == current.checkpoint_version
                and incoming != current
            )
        )

    @staticmethod
    def _assert_pending_binding(
        pending_action: ReplayPendingAction,
        policy_context: ReplayPolicyContext,
    ) -> None:
        if (
            not isinstance(pending_action, ReplayPendingAction)
            or not isinstance(policy_context, ReplayPolicyContext)
            or pending_action.replay_run_id != policy_context.replay_run_id
            or pending_action.continuation_id != policy_context.continuation_id
            or pending_action.workflow_id != policy_context.workflow_id
            or pending_action.step_id != policy_context.step_id
            or pending_action.action != policy_context.action
            or pending_action.action_digest != policy_context.action_digest
            or pending_action.sequence_index != policy_context.sequence_index
        ):
            raise BrowserReplayBridgeError("Replay policy context does not match its pending action")

    def _require_store(self) -> ReplayStateStore:
        if self.state_store is None:
            raise BrowserReplayBridgeError("Replay state store is unavailable")
        return self.state_store

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)
