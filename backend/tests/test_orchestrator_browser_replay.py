from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import os
from threading import Event, Thread, current_thread
from uuid import uuid4

import pytest

from backend.app.browser.replay import WorkflowReplay
from backend.app.browser.replay_state import ReplayPolicyContext
from backend.app.browser.replay_state_store import ReplayStateStore
from backend.app.browser.session_manager import BrowserSessionManager
from backend.app.browser.state_encryption import ACTIVE_KEY_ENV, BrowserReplayStateCipher
from backend.app.browser.workflow_store import WorkflowStore
from backend.app.orchestrator.browser_replay import (
    BrowserReplayBridgeError,
    BrowserReplayOrchestratorBridge,
)
from backend.app.orchestrator.task_manager import InvalidApprovalError, TaskManager
from backend.app.schemas.contracts import (
    TaskCreateRequest,
    TaskStep,
    ToolCall,
    WorkflowAction,
    WorkflowDefinition,
)


class FakeController:
    def __init__(self) -> None:
        self.is_started = False
        self.timeout_ms = 100
        self.actions: list[tuple] = []
        self.page = object()
        self.action_dispatched = Event()

    def start(self):
        self.is_started = True
        return self

    def close(self):
        self.is_started = False

    def navigate(self, url):
        self.actions.append(("navigate", url))
        self.action_dispatched.set()
        return url

    def click(self, target):
        self.actions.append(("click", target))

    def _require_page(self, operation):
        if not self.is_started:
            raise RuntimeError("closed")
        return self.page

    def _locator(self, page, target):
        return FakeLocator(self)


class FakeLocator:
    def __init__(self, controller):
        self.controller = controller

    def count(self):
        return 1

    def click(self, **kwargs):
        self.controller.actions.append(("click", kwargs))


@pytest.fixture
def integration(tmp_path, monkeypatch):
    monkeypatch.setenv(
        ACTIVE_KEY_ENV,
        "bridge-test:" + base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("="),
    )
    manager = TaskManager()
    call_arguments = {
        "operation": "start",
        "workflow_id": None,
        "parameters": {},
    }
    task = manager.create(
        TaskCreateRequest(user_request="Replay a workflow", auto_execute=False),
        [TaskStep(
            step_id="replay",
            tool_name="browser.replay",
            description="Replay a saved browser workflow",
            input=call_arguments,
            expected_outcome="Workflow completed",
        )],
    )
    workflow = WorkflowDefinition(
        name="Bridge workflow",
        start_url="https://example.test/start",
        steps=[WorkflowAction(
            step_id="next",
            action="click",
            target={"type": "role", "role": "button", "name": "Continue"},
        )],
    )
    call_arguments["workflow_id"] = str(workflow.workflow_id)
    # Keep the task plan and ToolCall payload identical as TaskManager expects.
    task.plan[0].input = dict(call_arguments)
    manager.save(task)
    parent = ToolCall(
        task_id=task.task_id,
        step_id="replay",
        tool_name="browser.replay",
        arguments=dict(call_arguments),
    )
    manager.record_tool_call(task.task_id, parent)

    workflow_store = WorkflowStore(tmp_path / "workflows")
    workflow_store.save(workflow)
    controller = FakeController()
    sessions = BrowserSessionManager(lambda: controller)
    sessions.create_session(parent)
    bridge = BrowserReplayOrchestratorBridge(
        manager,
        workflow_store,
        sessions,
        authorization_ttl=timedelta(seconds=30),
    )
    state_store = ReplayStateStore(
        tmp_path / "state",
        cipher=BrowserReplayStateCipher(),
        authorization_validator=bridge.validate_authorization,
        session_validator=bridge.validate_session,
    )
    bridge.attach_state_store(state_store)
    engine = WorkflowReplay(
        workflow_store,
        replay_state_store=state_store,
        session_manager=sessions,
        cancellation_checker=bridge,
        policy_handoff=bridge,
    )
    result = engine.start_replay(
        workflow.workflow_id,
        {},
        tool_call=parent,
        policy_handoff=bridge,
    )
    assert result.status == "pending_action"
    return {
        "manager": manager,
        "task": task,
        "workflow": workflow,
        "parent": parent,
        "workflow_store": workflow_store,
        "controller": controller,
        "sessions": sessions,
        "bridge": bridge,
        "state_store": state_store,
        "engine": engine,
        "result": result,
        "tmp_path": tmp_path,
    }


def _context(env) -> ReplayPolicyContext:
    pending = env["bridge"].pending_for_task(env["task"].task_id)
    assert pending is not None
    return pending[1]


def _resume_call(env, **arguments) -> ToolCall:
    return ToolCall(
        task_id=env["task"].task_id,
        tool_call_id=env["parent"].tool_call_id,
        step_id=env["parent"].step_id,
        tool_name="browser.replay",
        arguments={"operation": "resume", **arguments},
    )


def _advance_to_click(env):
    pending_action, context = env["bridge"].pending_for_task(env["task"].task_id)
    authorization = env["bridge"].issue_authorization(pending_action, context)
    assert authorization is not None
    result = env["engine"].resume_replay(
        env["result"].continuation,
        authorization,
        tool_call=env["parent"],
    )
    assert result.status == "pending_action"
    return result


def test_start_publishes_pending_without_dispatch_and_recovers_original_call(integration):
    env = integration
    pending = env["bridge"].pending_for_task(env["task"].task_id)

    assert pending is not None
    assert pending[0] == env["result"].pending_action
    assert env["controller"].actions == []
    assert env["bridge"].recover_parent_tool_call(pending[1]) == env["parent"]
    # Exact prepared arguments are available only via this trusted internal API.
    assert pending[1].exact_arguments == {"url": "https://example.test/start"}
    assert "exact_arguments" not in pending[1].model_dump()


def test_safe_pending_action_gets_one_no_approval_decision(integration):
    env = integration
    pending_action, context = env["bridge"].pending_for_task(env["task"].task_id)
    authorization = env["bridge"].issue_authorization(pending_action, context)
    assert authorization is not None
    assert authorization.decision == "allowed_without_approval"
    assert env["manager"].get(env["task"].task_id).pending_approvals == []
    assert env["manager"].replay_authorization_decision(pending_action, context) is None


def test_trusted_taskmanager_decision_binds_authorization_to_parent_and_one_action(integration):
    env = integration
    click_result = _advance_to_click(env)
    pending_action, context = env["bridge"].pending_for_task(env["task"].task_id)
    assert pending_action.step_id == "next"
    assert env["bridge"].issue_authorization(pending_action, context) is None
    task_with_approval = env["manager"].get(env["task"].task_id)
    assert len(task_with_approval.pending_approvals) == 1
    approval = task_with_approval.pending_approvals[0]
    assert approval.arguments == {}
    assert approval.description == "A browser workflow action requires approval."
    approved = env["manager"].resolve_approval(
        env["task"].task_id,
        approval.approval_id,
        "approved",
    )
    assert approved.pending_approvals == []
    assert approved.status.value == "executing"
    authorization = env["bridge"].issue_authorization(pending_action, context)
    assert authorization is not None
    assert authorization.decision == "approved"
    assert authorization.approval_id == approval.approval_id
    assert all(binding.exact_arguments == {} for binding in env["manager"]._replay_actions.values())

    resume_call = _resume_call(
        env,
        # These planner-controlled fields are deliberately ignored by the bridge.
        **{
            "continuation": {"replay_run_id": str(uuid4())},
            "authorization": {"one_time_token": "untrusted"},
        },
    )
    trusted = env["bridge"].resolve(resume_call)
    assert trusted is not None
    assert trusted.parent_tool_call == env["parent"]
    assert trusted.continuation == click_result.continuation
    assert trusted.authorization == authorization
    assert env["bridge"].validate_authorization(authorization, env["parent"])

    wrong_step = _resume_call(env).model_copy(update={"step_id": "other"})
    wrong_call_id = _resume_call(env).model_copy(update={"tool_call_id": uuid4()})
    assert env["bridge"].resolve(wrong_step) is None
    assert env["bridge"].resolve(wrong_call_id) is None
    assert not env["bridge"].validate_authorization(authorization, wrong_step)

    completed = env["engine"].resume_replay(
        trusted.continuation,
        trusted.authorization,
        tool_call=trusted.parent_tool_call,
    )
    assert completed.status == "completed"
    assert len(env["controller"].actions) == 2
    assert env["manager"].approval_history(env["task"].task_id)[0].status.value == "approved"


def test_expired_and_mutated_authorizations_are_rejected(integration):
    env = integration
    _advance_to_click(env)
    pending_action, context = env["bridge"].pending_for_task(env["task"].task_id)
    assert env["bridge"].issue_authorization(pending_action, context) is None
    approval = env["manager"].get(env["task"].task_id).pending_approvals[0]
    now = [datetime.now(timezone.utc)]
    env["manager"]._clock = lambda: now[0]
    now[0] += timedelta(minutes=11)
    with pytest.raises(InvalidApprovalError, match="expired"):
        env["manager"].resolve_approval(env["task"].task_id, approval.approval_id, "approved")
    expired = env["manager"].get(env["task"].task_id)
    assert expired.pending_approvals == []
    assert expired.status.value == "needs_attention"
    assert env["manager"].approval_history(env["task"].task_id)[0].status.value == "cancelled"
    assert env["bridge"].issue_authorization(pending_action, context) is None


def test_rejected_replay_approval_never_issues_authorization(integration):
    env = integration
    _advance_to_click(env)
    pending_action, context = env["bridge"].pending_for_task(env["task"].task_id)
    assert env["bridge"].issue_authorization(pending_action, context) is None
    approval = env["manager"].get(env["task"].task_id).pending_approvals[0]
    rejected = env["manager"].resolve_approval(env["task"].task_id, approval.approval_id, "rejected")
    assert rejected.status.value == "needs_attention"
    assert rejected.plan[0].status.value == "skipped"
    assert env["bridge"].issue_authorization(pending_action, context) is None
    assert env["controller"].actions == [("navigate", "https://example.test/start")]


def test_replay_approval_binding_rejects_mismatches_and_reuse(integration):
    env = integration
    _advance_to_click(env)
    pending_action, context = env["bridge"].pending_for_task(env["task"].task_id)

    other_workflow = uuid4()
    mismatched_pending = pending_action.model_copy(update={"workflow_id": other_workflow})
    mismatched_context = context.model_copy(update={"workflow_id": other_workflow})
    assert env["manager"].replay_authorization_decision(mismatched_pending, mismatched_context) is None
    assert env["manager"].get(env["task"].task_id).pending_approvals == []

    assert env["bridge"].issue_authorization(pending_action, context) is None

    changed_args = context.model_copy(update={
        "exact_arguments": {"target": {"type": "text", "value": "Different target"}}
    })
    changed_digest = context.model_copy(update={"action_digest": "f" * 64})
    assert env["manager"].replay_authorization_decision(pending_action, changed_args) is None
    assert env["manager"].replay_authorization_decision(pending_action, changed_digest) is None
    assert len(env["manager"].get(env["task"].task_id).pending_approvals) == 1

    approval = env["manager"].get(env["task"].task_id).pending_approvals[0]
    env["manager"].resolve_approval(env["task"].task_id, approval.approval_id, "approved")
    authorization = env["bridge"].issue_authorization(pending_action, context)
    assert authorization is not None
    assert env["bridge"].issue_authorization(pending_action, context) is None
    with pytest.raises(InvalidApprovalError, match="No pending approval"):
        env["manager"].resolve_approval(env["task"].task_id, approval.approval_id, "approved")


def test_replay_approval_rejects_mutated_public_summary(integration):
    env = integration
    _advance_to_click(env)
    pending_action, context = env["bridge"].pending_for_task(env["task"].task_id)
    assert env["bridge"].issue_authorization(pending_action, context) is None
    task = env["manager"].get(env["task"].task_id)
    approval = task.pending_approvals[0]
    task.pending_approvals[0].description = "Changed summary"
    env["manager"].save(task)

    with pytest.raises(InvalidApprovalError, match="trusted action"):
        env["manager"].resolve_approval(env["task"].task_id, approval.approval_id, "approved")
    assert env["bridge"].issue_authorization(pending_action, context) is None


def test_replay_approval_cancellation_invalidates_private_decision(integration):
    env = integration
    _advance_to_click(env)
    pending_action, context = env["bridge"].pending_for_task(env["task"].task_id)
    assert env["bridge"].issue_authorization(pending_action, context) is None
    cancelled = env["manager"].cancel(env["task"].task_id)
    assert cancelled.pending_approvals == []
    assert env["manager"].approval_history(env["task"].task_id)[0].status.value == "cancelled"
    assert env["manager"].replay_authorization_decision(pending_action, context) is None


def test_replay_action_arguments_remain_private_from_task_approval_serialization(integration):
    env = integration
    _advance_to_click(env)
    pending_action, context = env["bridge"].pending_for_task(env["task"].task_id)
    private_value = "never-public-replay-value"
    sensitive_context = context.model_copy(update={
        "exact_arguments": {
            "target": {"type": "label", "value": "Token"},
            "value": private_value,
        }
    })
    assert env["manager"].replay_authorization_decision(pending_action, sensitive_context) is None
    task = env["manager"].get(env["task"].task_id)
    serialized = task.model_dump_json()
    approval = task.pending_approvals[0]
    assert approval.arguments == {}
    assert private_value not in serialized
    assert str(sensitive_context.continuation_id) not in serialized
    assert sensitive_context.action_digest not in serialized
    assert private_value not in repr(env["manager"]._replay_actions)


def test_resume_uses_recovered_parent_and_executes_at_most_one_action(integration):
    env = integration
    context = _context(env)
    authorization = env["bridge"].issue_authorization(env["result"].pending_action, context)
    assert authorization is not None
    resume_call = _resume_call(env)
    trusted = env["bridge"].resolve(resume_call)
    assert trusted is not None

    result = env["engine"].resume_replay(
        trusted.continuation,
        trusted.authorization,
        tool_call=trusted.parent_tool_call,
    )
    assert result.status == "pending_action"
    assert env["controller"].actions == [("navigate", "https://example.test/start")]
    assert env["bridge"].pending_for_task(env["task"].task_id)[0].step_id == "next"
    assert env["bridge"].resolve(resume_call) is None  # old permit/cursor cannot be reused

    # Reuse the consumed authorization and stale continuation directly against the
    # real replay engine; neither may dispatch another action.
    second_result = env["engine"].resume_replay(
        trusted.continuation,
        trusted.authorization,
        tool_call=trusted.parent_tool_call,
    )
    assert second_result.status in {"failed", "needs_attention"}
    assert env["controller"].actions == [("navigate", "https://example.test/start")]


def test_cancellation_and_restart_fail_closed(integration):
    env = integration
    env["manager"].cancel(env["task"].task_id)
    assert env["bridge"].is_cancelled(env["task"].task_id)
    assert env["bridge"].pending_for_task(env["task"].task_id) is None
    resume_call = _resume_call(env)
    assert env["bridge"].resolve(resume_call) is None

    restarted = BrowserReplayOrchestratorBridge(
        env["manager"], env["workflow_store"], env["sessions"], state_store=env["state_store"]
    )
    assert restarted.resolve(resume_call) is None
    assert restarted.pending_for_task(env["task"].task_id) is None


def test_pending_binding_rejects_mismatched_policy_context(integration):
    env = integration
    pending_action, context = env["bridge"].pending_for_task(env["task"].task_id)
    wrong = context.model_copy(update={"action_digest": "f" * 64})
    with pytest.raises(BrowserReplayBridgeError):
        env["bridge"].issue_authorization(pending_action, wrong)

    altered_arguments = context.model_copy(
        update={"exact_arguments": {"url": "https://attacker.invalid/"}}
    )
    with pytest.raises(BrowserReplayBridgeError):
        env["bridge"].publish(
            pending_action,
            altered_arguments,
            workflow_digest=env["result"].continuation.workflow_digest,
            explanation="ignored",
        )


def test_out_of_order_publish_cannot_roll_back_to_old_checkpoint(integration):
    env = integration
    old_pending, old_context = env["bridge"].pending_for_task(env["task"].task_id)
    authorization = env["bridge"].issue_authorization(old_pending, old_context)
    assert authorization is not None
    result = env["engine"].resume_replay(
        env["result"].continuation,
        authorization,
        tool_call=env["parent"],
    )
    assert result.continuation is not None
    current = env["bridge"].pending_for_task(env["task"].task_id)
    assert current is not None and current[0].step_id == "next"

    with pytest.raises(BrowserReplayBridgeError):
        env["bridge"].publish(
            old_pending,
            old_context,
            workflow_digest=env["result"].continuation.workflow_digest,
            explanation="out-of-order",
        )
    assert env["bridge"].pending_for_task(env["task"].task_id)[0].step_id == "next"


def test_concurrent_out_of_order_publish_keeps_newest_cursor(integration, monkeypatch):
    env = integration
    bridge = env["bridge"]
    old_pending, old_context = bridge.pending_for_task(env["task"].task_id)
    old_digest = env["result"].continuation.workflow_digest
    validation_ready = Event()
    release_old_publisher = Event()
    new_publish_entered = Event()
    new_publish_returned = Event()
    original_context_reader = env["state_store"].get_pending_policy_context
    original_publish = bridge.publish
    errors: list[BaseException] = []
    resume_results = []
    authorization = bridge.issue_authorization(old_pending, old_context)
    assert authorization is not None

    def gated_context_reader(continuation, *, tool_call, current_workflow):
        context = original_context_reader(
            continuation,
            tool_call=tool_call,
            current_workflow=current_workflow,
        )
        if current_thread().name == "stale-publisher":
            validation_ready.set()
            if not release_old_publisher.wait(5):
                raise TimeoutError("test synchronization timed out")
        return context

    def watched_publish(pending_action, policy_context, *, workflow_digest, explanation):
        if policy_context.sequence_index > old_context.sequence_index:
            new_publish_entered.set()
        result = original_publish(
            pending_action,
            policy_context,
            workflow_digest=workflow_digest,
            explanation=explanation,
        )
        if policy_context.sequence_index > old_context.sequence_index:
            new_publish_returned.set()
        return result

    monkeypatch.setattr(env["state_store"], "get_pending_policy_context", gated_context_reader)
    monkeypatch.setattr(bridge, "publish", watched_publish)

    def publish_old():
        try:
            original_publish(
                old_pending,
                old_context,
                workflow_digest=old_digest,
                explanation="delayed old publish",
            )
        except BaseException as exc:
            errors.append(exc)

    stale_thread = Thread(target=publish_old, name="stale-publisher")
    stale_thread.start()
    assert validation_ready.wait(3)

    def resume_once():
        try:
            resume_results.append(env["engine"].resume_replay(
                env["result"].continuation,
                authorization,
                tool_call=env["parent"],
            ))
        except BaseException as exc:
            errors.append(exc)

    resume_thread = Thread(target=resume_once, name="replay-resumer")
    resume_thread.start()
    assert env["controller"].action_dispatched.wait(3)
    assert new_publish_entered.wait(3)
    # The newer publish cannot pass the bridge lock while the old validated
    # publication is paused. Release it, then the newer cursor must win.
    assert not new_publish_returned.wait(0.05)
    release_old_publisher.set()
    stale_thread.join(3)
    resume_thread.join(3)
    assert not stale_thread.is_alive()
    assert not resume_thread.is_alive()
    assert not errors
    assert resume_results and resume_results[0].status == "pending_action"
    current = bridge.pending_for_task(env["task"].task_id)
    assert current is not None and current[0].step_id == "next"


@pytest.mark.parametrize("damage", ["delete", "corrupt"])
def test_checkpoint_loss_after_resolution_prevents_action_dispatch(integration, damage):
    env = integration
    context = _context(env)
    authorization = env["bridge"].issue_authorization(env["result"].pending_action, context)
    assert authorization is not None
    trusted = env["bridge"].resolve(_resume_call(env))
    assert trusted is not None

    checkpoint = env["state_store"]._database_path(trusted.continuation.replay_run_id)
    if damage == "delete":
        checkpoint.unlink()
    else:
        checkpoint.write_bytes(b"not a valid replay database")

    result = env["engine"].resume_replay(
        trusted.continuation,
        trusted.authorization,
        tool_call=trusted.parent_tool_call,
    )
    assert result.status in {"failed", "needs_attention"}
    assert env["controller"].actions == []

