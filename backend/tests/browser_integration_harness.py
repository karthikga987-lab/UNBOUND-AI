"""Test-only simulated orchestrator for complete browser replay integration.

This module deliberately lives under ``backend.tests``. It models a policy
decision and trusted resume-context handoff for tests; it is not an approval
manager and must not be imported by production application code.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from backend.app.browser.replay import WorkflowReplay
from backend.app.browser.replay_state import (
    ReplayPolicyContext,
    ReplayStepAuthorization,
)
from backend.app.browser.replay_state_store import ReplayStateStore
from backend.app.browser.session_manager import BrowserSessionManager
from backend.app.browser.state_encryption import ACTIVE_KEY_ENV, BrowserReplayStateCipher
from backend.app.browser.tools import (
    BrowserReplayInput,
    BrowserToolAdapter,
    TrustedReplayResumeContext,
)
from backend.app.browser.workflow_store import WorkflowStore
from backend.app.schemas.contracts import ToolCall, WorkflowDefinition


class SimulatedPage:
    def __init__(self, controller: "SimulatedController") -> None:
        self.controller = controller
        self.url = "about:blank"


class SimulatedLocator:
    def __init__(self, controller: "SimulatedController", target: dict[str, Any]) -> None:
        self.controller = controller
        self.target = target

    def count(self) -> int:
        return 0 if self.controller.missing_locator else 1


class SimulatedController:
    """Small browser double implementing the controller surface replay uses."""

    timeout_ms = 100

    def __init__(self) -> None:
        self.is_started = False
        self.actions: list[tuple[Any, ...]] = []
        self.page = SimulatedPage(self)
        self.missing_locator = False
        self.uncertain_action: str | None = None

    @property
    def current_url(self) -> str:
        return self.page.url

    def start(self) -> "SimulatedController":
        self.is_started = True
        return self

    def close(self) -> None:
        self.is_started = False

    def navigate(self, url: str) -> str:
        self.actions.append(("navigate", url))
        if self.uncertain_action == "navigate":
            raise RuntimeError("simulated dispatch uncertainty")
        self.page.url = url
        return url

    def click(self, target: dict[str, Any]) -> None:
        self.actions.append(("click", target))
        if self.uncertain_action == "click":
            raise RuntimeError("simulated dispatch uncertainty")

    def fill(self, target: dict[str, Any], value: str) -> None:
        self.actions.append(("fill", target, value))
        if self.uncertain_action == "fill":
            raise RuntimeError("simulated dispatch uncertainty")

    def _require_page(self, operation: str) -> SimulatedPage:
        if not self.is_started:
            raise RuntimeError("simulated session is closed")
        return self.page

    def _locator(self, page: SimulatedPage, target: dict[str, Any]) -> SimulatedLocator:
        return SimulatedLocator(self, target)


class SimulatedCancellationChecker:
    def __init__(self) -> None:
        self.cancelled_tasks: set[UUID] = set()

    def is_cancelled(self, task_id: UUID) -> bool:
        return task_id in self.cancelled_tasks


class SimulatedOrchestrator:
    """Test-only policy/handoff/provider exercising the approved boundaries."""

    def __init__(self, root: Path, workflow: WorkflowDefinition) -> None:
        self.root = root
        self.workflow = workflow
        self.workflow_store = WorkflowStore(root / "workflows")
        self.workflow_store.save(workflow)
        self.controllers: list[SimulatedController] = []

        def make_controller() -> SimulatedController:
            controller = SimulatedController()
            self.controllers.append(controller)
            return controller

        self.sessions = BrowserSessionManager(make_controller)
        self.cancellation = SimulatedCancellationChecker()
        self._issued: dict[UUID, tuple[str, UUID]] = {}
        self._resume_context: TrustedReplayResumeContext | None = None
        self.policy_events: list[tuple[Any, ReplayPolicyContext, str]] = []
        self.policy_decisions: list[tuple[str, bool]] = []
        self.parent_call: ToolCall | None = None
        self.state_store = self._make_state_store(root / "state")
        self.engine = self._make_engine()
        self.adapter = self._make_adapter()

    def _make_state_store(self, directory: Path) -> ReplayStateStore:
        return ReplayStateStore(
            directory,
            cipher=BrowserReplayStateCipher(),
            authorization_validator=self._validate_issued_authorization,
            session_validator=self._validate_session,
        )

    def _make_engine(self) -> WorkflowReplay:
        return WorkflowReplay(
            self.workflow_store,
            replay_state_store=self.state_store,
            session_manager=self.sessions,
            cancellation_checker=self.cancellation,
            policy_handoff=self,
        )

    def _make_adapter(self) -> BrowserToolAdapter:
        return BrowserToolAdapter(
            workflow_store=self.workflow_store,
            session_manager=self.sessions,
            replay_engine=self.engine,
            policy_handoff=self,
            resume_context_provider=self,
        )

    def start(self, task_id: UUID, parameters: dict[str, Any] | None = None):
        self.parent_call = ToolCall(
            task_id=task_id,
            step_id="workflow-replay",
            tool_name="browser.replay",
            arguments={
                "operation": "start",
                "workflow_id": str(self.workflow.workflow_id),
                "parameters": parameters or {},
            },
        )
        self.sessions.create_session(self.parent_call)
        return self.adapter.replay(
            BrowserReplayInput(operation="start", workflow_id=self.workflow.workflow_id,
                               parameters=parameters or {}),
            self.parent_call,
        )

    def publish(self, pending_action, policy_context: ReplayPolicyContext, *, workflow_digest: str, explanation: str) -> None:
        # The public pending action is published only after durable checkpoint.
        snapshot = self.state_store.load_result(
            pending_action.replay_run_id,
            current_workflow=self.workflow_store.load(self.workflow.workflow_id),
        )
        if snapshot.pending_action != pending_action or snapshot.continuation is None:
            raise AssertionError("Replay checkpoint was not persisted before pending publication")
        if policy_context.action_digest != pending_action.action_digest:
            raise AssertionError("Policy context is not bound to the pending action")
        self.policy_events.append((pending_action, policy_context, workflow_digest))

    def requires_approval(self, policy_context: ReplayPolicyContext) -> bool:
        target = policy_context.exact_arguments.get("target", {})
        label = " ".join(str(target.get(key, "")) for key in ("name", "text", "value"))
        required = policy_context.action == "click" and any(
            word in label.casefold() for word in ("submit", "purchase", "delete", "send")
        )
        self.policy_decisions.append((policy_context.step_id, required))
        return required

    def issue_authorization(
        self,
        *,
        approved: bool,
        issued_at: datetime | None = None,
        expires_at: datetime | None = None,
        action_digest: str | None = None,
    ) -> ReplayStepAuthorization:
        if not self.policy_events or self.parent_call is None:
            raise RuntimeError("No pending action is available")
        pending, context, _ = self.policy_events[-1]
        now = datetime.now(timezone.utc)
        authorization = ReplayStepAuthorization(
            authorization_id=uuid4(),
            task_id=context.task_id,
            tool_call_id=context.tool_call_id,
            replay_run_id=context.replay_run_id,
            continuation_id=context.continuation_id,
            workflow_id=context.workflow_id,
            step_id=context.step_id,
            action_digest=action_digest or context.action_digest,
            decision="approved" if approved else "allowed_without_approval",
            approval_id=uuid4() if approved else None,
            issued_at=issued_at or now - timedelta(seconds=1),
            expires_at=expires_at or now + timedelta(minutes=2),
            one_time_token=f"test-only:{uuid4()}:{pending.action_digest}",
        )
        self._issued[authorization.authorization_id] = (
            authorization.one_time_token.get_secret_value(), context.task_id
        )
        return authorization

    def prepare_resume(self, authorization: ReplayStepAuthorization) -> None:
        if self.parent_call is None or not self.policy_events:
            raise RuntimeError("No replay is active")
        pending, _, _ = self.policy_events[-1]
        snapshot = self.state_store.load_result(
            pending.replay_run_id,
            current_workflow=self.workflow_store.load(self.workflow.workflow_id),
        )
        if snapshot.continuation is None:
            raise RuntimeError("Replay has no continuation")
        self._resume_context = TrustedReplayResumeContext(
            continuation=snapshot.continuation,
            authorization=authorization,
            parent_tool_call=self.parent_call,
        )

    def resolve(self, tool_call: ToolCall) -> TrustedReplayResumeContext | None:
        context = self._resume_context
        self._resume_context = None  # trusted authorization is handed off once
        if context is None or context.parent_tool_call.task_id != tool_call.task_id:
            return None
        return context

    def resume(self, task_id: UUID):
        call = ToolCall(
            task_id=task_id,
            step_id="resume-replay",
            tool_name="browser.replay",
            arguments={"operation": "resume"},
        )
        return self.adapter.replay(BrowserReplayInput(operation="resume"), call)

    def authorize_and_resume(self, *, approved: bool | None = None):
        if not self.policy_events:
            raise RuntimeError("No pending action is available")
        _, policy_context, _ = self.policy_events[-1]
        must_approve = self.requires_approval(policy_context)
        if must_approve and approved is not True:
            return None
        auth = self.issue_authorization(approved=must_approve)
        self.prepare_resume(auth)
        return self.resume(policy_context.task_id)

    def _validate_issued_authorization(self, authorization: ReplayStepAuthorization, tool_call: ToolCall) -> bool:
        issued = self._issued.get(authorization.authorization_id)
        return bool(
            issued
            and issued[0] == authorization.one_time_token.get_secret_value()
            and issued[1] == tool_call.task_id
            and authorization.task_id == tool_call.task_id
        )

    def _validate_session(self, task_id: UUID, session_id: UUID) -> bool:
        if self.parent_call is None or self.parent_call.task_id != task_id:
            return False
        return self.sessions.validate_session(self.parent_call, session_id)

    def restart_engine(self) -> None:
        """Reopen encrypted checkpoint storage and replace the engine/adapter."""
        self.state_store = self._make_state_store(self.state_store.directory)
        self.engine = self._make_engine()
        self.adapter = self._make_adapter()


def configure_test_encryption_key(monkeypatch) -> None:
    """Install a fresh test-only key; never used by application code."""
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    monkeypatch.setenv(ACTIVE_KEY_ENV, f"phase11b-test:{key}")
