from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import threading
from uuid import uuid4

import pytest

from backend.app.browser.controller import BrowserController
from backend.app.browser.replay import (
    BrowserSessionBinding,
    WorkflowReplay,
)
from backend.app.browser.replay_state import ReplayActionJournalState, ReplayStepAuthorization
from backend.app.browser.replay_state_store import ReplayStateStore
from backend.app.browser.state_encryption import ACTIVE_KEY_ENV, BrowserReplayStateCipher
from backend.app.browser.workflow_store import WorkflowStore
from backend.app.schemas.contracts import ToolCall, WorkflowAction, WorkflowDefinition, WorkflowParameter


class FakeLocator:
    def __init__(self, controller):
        self.controller = controller

    def count(self):
        return 1

    def click(self, **kwargs):
        self.controller.actions.append(("click", kwargs))

    def fill(self, value, **kwargs):
        self.controller.actions.append(("fill", value))

    def select_option(self, value, **kwargs):
        self.controller.actions.append(("select_option", value))


class FakeController:
    def __init__(self):
        self.is_started = True
        self.timeout_ms = 100
        self.actions = []
        self.page = object()

    def navigate(self, url):
        self.actions.append(("navigate", url))

    def click(self, target):
        self.actions.append(("click", target))

    def fill(self, target, value):
        self.actions.append(("fill", value))

    def _require_page(self, operation):
        return self.page

    def _locator(self, page, target):
        return FakeLocator(FakeController.current)


FakeController.current = None


class FakeSessionManager:
    def __init__(self, binding):
        self.binding = binding

    def get_session(self, task_id):
        return self.binding


class FakeCancellationChecker:
    def __init__(self, cancel_on_call=None):
        self.calls = 0
        self.cancel_on_call = cancel_on_call
        self.cancelled = False

    def is_cancelled(self, task_id):
        self.calls += 1
        return self.cancelled or self.calls == self.cancel_on_call


class FakePolicyHandoff:
    def __init__(self):
        self.events = []
        self.fail = False

    def publish(self, pending_action, policy_context, *, workflow_digest, explanation):
        if self.fail:
            raise RuntimeError("handoff failure")
        self.events.append((pending_action, policy_context, workflow_digest, explanation))


def _key_setting():
    return "phase9-test:" + base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


@pytest.fixture
def replay_env(tmp_path, monkeypatch):
    monkeypatch.setenv(ACTIVE_KEY_ENV, _key_setting())
    workflow = WorkflowDefinition(
        name="Phase 9 workflow",
        start_url="https://example.test/start",
        parameters=[WorkflowParameter(name="email", type="string", required=True)],
        steps=[
            WorkflowAction(
                step_id="fill-email",
                action="fill",
                target={"type": "label", "value": "Email"},
                input={"value": "{{email}}"},
                expected_outcome="Email is entered",
            ),
            WorkflowAction(
                step_id="submit-form",
                action="click",
                target={"type": "role", "role": "button", "name": "Submit"},
            ),
        ],
    )
    workflow_store = WorkflowStore(tmp_path / "workflows")
    workflow_store.save(workflow)
    task_id = uuid4()
    call = ToolCall(
        task_id=task_id,
        step_id="workflow-replay",
        tool_name="browser.replay",
        arguments={"workflow_id": str(workflow.workflow_id), "parameters": {"email": "member@example.test"}},
    )
    session_id = uuid4()
    controller = FakeController()
    FakeController.current = controller
    binding = BrowserSessionBinding(session_id=session_id, controller=controller)
    token_prefix = "trusted-phase9-token-"
    state_store = ReplayStateStore(
        tmp_path / "replay-state",
        cipher=BrowserReplayStateCipher(),
        authorization_validator=lambda authorization, tool_call: (
            tool_call.task_id == task_id
            and authorization.one_time_token.get_secret_value() == token_prefix + authorization.step_id
        ),
        session_validator=lambda active_task, active_session: (
            active_task == task_id and active_session == session_id
        ),
    )
    cancellation = FakeCancellationChecker()
    handoff = FakePolicyHandoff()
    engine = WorkflowReplay(
        workflow_store,
        replay_state_store=state_store,
        session_manager=FakeSessionManager(binding),
        cancellation_checker=cancellation,
        policy_handoff=handoff,
    )
    initial = engine.start_replay(
        workflow.workflow_id,
        {"email": "member@example.test"},
        tool_call=call,
        policy_handoff=handoff,
    )
    return {
        "engine": engine,
        "workflow": workflow,
        "workflow_store": workflow_store,
        "state_store": state_store,
        "call": call,
        "session_id": session_id,
        "controller": controller,
        "cancellation": cancellation,
        "handoff": handoff,
        "token_prefix": token_prefix,
        "result": initial,
        "tmp_path": tmp_path,
    }


def _authorization(env, result=None, *, token=None, digest=None, expires=None):
    result = result or env["result"]
    context = env["state_store"].get_pending_policy_context(
        result.continuation,
        tool_call=env["call"],
        current_workflow=env["workflow"],
    )
    now = datetime.now(timezone.utc)
    expires = expires or now + timedelta(minutes=2)
    issued = min(now - timedelta(seconds=1), expires - timedelta(seconds=1))
    return ReplayStepAuthorization(
        authorization_id=uuid4(),
        task_id=context.task_id,
        tool_call_id=context.tool_call_id,
        replay_run_id=context.replay_run_id,
        continuation_id=context.continuation_id,
        workflow_id=context.workflow_id,
        step_id=context.step_id,
        action_digest=digest or context.action_digest,
        decision="allowed_without_approval",
        issued_at=issued,
        expires_at=expires,
        one_time_token=token or env["token_prefix"] + context.step_id,
    )


def _resume(env, result=None, authorization=None):
    result = result or env["result"]
    return env["engine"].resume_replay(
        result.continuation,
        authorization or _authorization(env, result),
        tool_call=env["call"],
    )


def test_start_persists_checkpoint_publishes_policy_and_executes_nothing(replay_env):
    env = replay_env
    result = env["result"]

    assert result.status == "pending_action"
    assert result.pending_action.step_id == "workflow.start"
    assert result.continuation.next_action_index == 0
    assert env["controller"].actions == []
    assert len(env["handoff"].events) == 1
    pending, private_context, workflow_digest, explanation = env["handoff"].events[0]
    assert pending == result.pending_action
    assert private_context.exact_arguments == {"url": "https://example.test/start"}
    assert workflow_digest == result.continuation.workflow_digest
    assert "Navigate" in explanation
    assert env["state_store"].load_result(
        result.replay_run_id, current_workflow=env["workflow"]
    ).continuation == result.continuation


def test_each_resume_executes_one_action_and_returns_next_pending(replay_env):
    env = replay_env
    after_start = _resume(env)

    assert after_start.status == "pending_action"
    assert after_start.pending_action.step_id == "fill-email"
    assert env["controller"].actions == [("navigate", "https://example.test/start")]
    assert len(env["handoff"].events) == 2

    after_fill = _resume(env, after_start)
    assert after_fill.status == "pending_action"
    assert after_fill.pending_action.step_id == "submit-form"
    assert env["controller"].actions[-1][0] == "fill"
    assert len(env["controller"].actions) == 2

    completed = _resume(env, after_fill)
    assert completed.status == "completed"
    assert env["controller"].actions[-1][0] == "click"
    assert len(env["controller"].actions) == 3


def test_start_pauses_before_sensitive_parameter_fill_and_hides_value(replay_env):
    env = replay_env
    secret = "member@example.test"
    serialized = env["result"].model_dump_json()
    private_context = env["handoff"].events[0][1]

    assert secret not in serialized
    assert secret not in repr(private_context)
    assert secret not in private_context.model_dump_json()
    assert env["controller"].actions == []


def test_expired_mismatched_and_reused_authorizations_do_not_dispatch(replay_env):
    env = replay_env
    expired = _authorization(env, expires=datetime.now(timezone.utc) - timedelta(seconds=1))
    expired_result = _resume(env, authorization=expired)
    assert expired_result.status == "failed"
    assert env["controller"].actions == []

    wrong = _authorization(env, digest="0" * 64)
    wrong_result = _resume(env, authorization=wrong)
    assert wrong_result.status == "failed"
    assert env["controller"].actions == []

    valid = _authorization(env)
    first = _resume(env, authorization=valid)
    assert first.status == "pending_action"
    replayed = env["engine"].resume_replay(
        env["result"].continuation,
        valid,
        tool_call=env["call"],
    )
    assert replayed.status == "failed"
    assert env["controller"].actions == [("navigate", "https://example.test/start")]


def test_completed_step_is_not_repeated_on_stale_continuation(replay_env):
    env = replay_env
    stale = env["result"].continuation
    valid = _authorization(env)
    _resume(env, authorization=valid)
    repeated = env["engine"].resume_replay(stale, valid, tool_call=env["call"])

    assert repeated.status == "failed"
    assert env["controller"].actions == [("navigate", "https://example.test/start")]


def test_concurrent_resume_dispatches_at_most_once(replay_env):
    env = replay_env
    authorization = _authorization(env)
    barrier = threading.Barrier(2)

    def invoke():
        barrier.wait()
        return _resume(env, authorization=authorization)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(invoke), pool.submit(invoke)]]

    assert sum(action[0] == "navigate" for action in env["controller"].actions) == 1
    assert len(results) == 2


def test_session_loss_fails_closed(replay_env):
    env = replay_env
    env["engine"].session_manager.binding = None
    lost = _resume(env)
    assert lost.status == "needs_attention"
    assert env["controller"].actions == []


def test_workflow_digest_mismatch_fails_closed(replay_env):
    env = replay_env
    authorization = _authorization(env)
    changed = env["workflow"].model_copy(update={"description": "changed workflow"})
    env["workflow_store"].delete(changed.workflow_id)
    env["workflow_store"].save(changed)
    mismatch = _resume(env, authorization=authorization)
    assert mismatch.status == "needs_attention"
    assert env["controller"].actions == []


def test_action_exception_is_outcome_unknown_and_not_retried(replay_env, monkeypatch):
    env = replay_env
    authorization = _authorization(env)
    monkeypatch.setattr(env["controller"], "navigate", lambda _url: (_ for _ in ()).throw(RuntimeError("secret diagnostics")))
    result = _resume(env, authorization=authorization)

    assert result.status == "needs_attention"
    assert result.checkpoints[0].state == ReplayActionJournalState.OUTCOME_UNKNOWN
    assert "secret diagnostics" not in result.model_dump_json()
    retry = env["engine"].resume_replay(result.continuation, authorization, tool_call=env["call"])
    assert retry.status == "needs_attention"
    assert len(env["controller"].actions) == 0


def test_cancellation_before_claim_stops_without_dispatch(replay_env):
    env = replay_env
    env["cancellation"].cancelled = True
    cancelled = _resume(env)
    assert cancelled.status == "needs_attention"
    assert env["controller"].actions == []


def test_cancellation_after_claim_but_before_dispatch_is_not_uncertain(replay_env):
    env = replay_env
    env["cancellation"].cancel_on_call = 3  # start, pre-claim, then post-claim
    before_dispatch = _resume(env)
    assert before_dispatch.status == "needs_attention"
    assert env["controller"].actions == []
    assert before_dispatch.checkpoints[0].state == ReplayActionJournalState.NOT_STARTED

def test_cancellation_after_action_does_not_claim_rollback(replay_env):
    env = replay_env
    env["controller"].navigate = lambda url: (
        env["controller"].actions.append(("navigate", url)),
        setattr(env["cancellation"], "cancelled", True),
    )
    after_action = _resume(env)
    assert after_action.status == "needs_attention"
    assert after_action.checkpoints[0].state == ReplayActionJournalState.COMPLETED
    assert "cancelled" in after_action.error
    assert len(env["controller"].actions) == 1


def test_checkpoint_persistence_failure_recovers_as_uncertain(replay_env, monkeypatch):
    env = replay_env
    def fail_after_releasing(lease, **kwargs):
        lease.release()
        raise RuntimeError()

    monkeypatch.setattr(env["state_store"], "complete_action", fail_after_releasing)
    result = _resume(env)

    assert result.status == "needs_attention"
    assert result.checkpoints[0].state == ReplayActionJournalState.OUTCOME_UNKNOWN
    assert len(env["controller"].actions) == 1


def test_process_restart_can_resume_with_injected_dependencies(replay_env):
    env = replay_env
    # The first engine's output is durable; a new engine has no in-memory
    # handoff cache and receives its trusted policy handoff through DI.
    first = _resume(env)
    second_handoff = FakePolicyHandoff()
    restored = WorkflowReplay(
        env["workflow_store"],
        replay_state_store=ReplayStateStore(
            env["tmp_path"] / "replay-state",
            cipher=BrowserReplayStateCipher(),
            authorization_validator=env["state_store"].authorization_validator,
            session_validator=env["state_store"].session_validator,
        ),
        session_manager=FakeSessionManager(
            BrowserSessionBinding(env["session_id"], env["controller"])
        ),
        cancellation_checker=FakeCancellationChecker(),
        policy_handoff=second_handoff,
    )
    # Use the same state store object for the next authorization callback's
    # private context lookup; the new engine reloads independently from SQLite.
    result = restored.resume_replay(
        first.continuation,
        _authorization(env, first),
        tool_call=env["call"],
    )

    assert result.status == "pending_action"
    assert result.pending_action.step_id == "submit-form"
    assert second_handoff.events[0][0] == result.pending_action


def test_interrupted_dispatch_recovers_to_unknown_before_any_retry(replay_env):
    env = replay_env
    authorization = _authorization(env)
    context = env["state_store"].get_pending_policy_context(
        env["result"].continuation,
        tool_call=env["call"],
        current_workflow=env["workflow"],
    )
    lease = env["state_store"].claim_action(
        env["result"].continuation,
        authorization,
        tool_call=env["call"],
        current_workflow=env["workflow"],
        active_session_id=env["session_id"],
    )
    assert lease.policy_context.action_digest == context.action_digest
    lease.release()  # process exited after the durable claim but before dispatch

    recovered = env["engine"].resume_replay(
        env["result"].continuation,
        authorization,
        tool_call=env["call"],
    )

    assert recovered.status == "needs_attention"
    assert recovered.checkpoints[0].state == ReplayActionJournalState.OUTCOME_UNKNOWN
    assert env["controller"].actions == []
