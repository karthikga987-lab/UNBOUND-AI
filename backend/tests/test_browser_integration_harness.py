from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from backend.app.browser.replay_state import ReplayActionJournalState
from backend.app.browser.tools import BrowserNavigateInput, BrowserReplayInput
from backend.app.schemas.contracts import ToolCall, WorkflowAction, WorkflowDefinition, WorkflowParameter
from pydantic import ValidationError
from backend.tests.browser_integration_harness import (
    SimulatedOrchestrator,
    configure_test_encryption_key,
)


SECRET_EMAIL = "private-member-address-913@example.test"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    configure_test_encryption_key(monkeypatch)
    workflow = WorkflowDefinition(
        name="Quarterly workflow",
        start_url="https://example.test/start",
        parameters=[WorkflowParameter(name="email", type="string", required=True)],
        steps=[
            WorkflowAction(
                step_id="fill-email",
                action="fill",
                target={"type": "label", "value": "Email"},
                input={"value": "{{email}}"},
            ),
            WorkflowAction(
                step_id="submit-form",
                action="click",
                target={"type": "role", "role": "button", "name": "Submit"},
            ),
        ],
    )
    return SimulatedOrchestrator(tmp_path, workflow)


def _start(harness):
    return harness.start(uuid4(), {"email": SECRET_EMAIL})


def _latest_status(tool_result):
    assert tool_result.success
    return tool_result.data["status"]


def test_start_publishes_first_pending_action_without_executing(harness):
    started = _start(harness)
    assert _latest_status(started) == "pending_action"
    assert started.data["pending_action"]["step_id"] == "workflow.start"
    assert harness.controllers[0].actions == []
    assert len(harness.policy_events) == 1
    assert harness.policy_events[0][0].step_id == "workflow.start"


def test_complete_workflow_enforces_policy_and_executes_one_action_per_resume(harness):
    task_id = uuid4()
    harness.start(task_id, {"email": SECRET_EMAIL})
    assert harness.controllers[0].actions == []

    # The orchestrator evaluates the first pending navigation and authorizes it
    # without approval. One resume dispatches exactly that one action.
    first_context = harness.policy_events[-1][1]
    assert harness.requires_approval(first_context) is False
    after_navigation = harness.authorize_and_resume()
    assert _latest_status(after_navigation) == "pending_action"
    assert harness.controllers[0].actions == [("navigate", "https://example.test/start")]
    assert after_navigation.data["pending_action"]["step_id"] == "fill-email"
    assert after_navigation.data["checkpoints"][0]["state"] == "completed"

    after_fill = harness.authorize_and_resume()
    assert _latest_status(after_fill) == "pending_action"
    assert len(harness.controllers[0].actions) == 2
    assert harness.controllers[0].actions[1] == (
        "fill", {"type": "label", "value": "Email"}, SECRET_EMAIL
    )
    assert after_fill.data["pending_action"]["step_id"] == "submit-form"

    # Submit is consequential under this test policy. No resume happens until
    # an explicit simulated approval is supplied.
    assert harness.authorize_and_resume() is None
    assert len(harness.controllers[0].actions) == 2
    completed = harness.authorize_and_resume(approved=True)
    assert _latest_status(completed) == "completed"
    assert harness.controllers[0].actions[2] == (
        "click", {"type": "role", "role": "button", "name": "Submit"}
    )
    assert len(harness.controllers[0].actions) == 3
    assert [item[1].step_id for item in harness.policy_events] == [
        "workflow.start", "fill-email", "submit-form"
    ]
    assert harness.policy_decisions[-1] == ("submit-form", True)


@pytest.mark.parametrize("kind", ["expired", "mismatched"])
def test_rejects_expired_or_digest_mismatched_authorization_without_dispatch(harness, kind):
    _start(harness)
    context = harness.policy_events[-1][1]
    if kind == "expired":
        now = datetime.now(timezone.utc)
        authorization = harness.issue_authorization(
            approved=False,
            issued_at=now - timedelta(minutes=2),
            expires_at=now - timedelta(minutes=1),
        )
        # Keep the trusted test provider's issued token binding in place.
    else:
        authorization = harness.issue_authorization(
            approved=False,
            action_digest="f" * 64,
        )
    harness.prepare_resume(authorization)
    result = harness.resume(context.task_id)
    assert result.success is False
    assert result.data["error"] == "Replay authorization was rejected"
    assert result.data["status"] == "failed"
    assert harness.controllers[0].actions == []


def test_reused_authorization_cannot_repeat_a_completed_action(harness):
    _start(harness)
    context = harness.policy_events[-1][1]
    auth = harness.issue_authorization(approved=False)
    harness.prepare_resume(auth)
    first = harness.resume(context.task_id)
    assert first.data["pending_action"]["step_id"] == "fill-email"
    count = len(harness.controllers[0].actions)

    # Re-present the old authorization against the current pending action.
    harness.prepare_resume(auth)
    repeated = harness.resume(context.task_id)
    assert len(harness.controllers[0].actions) == count
    assert repeated.success is False
    assert repeated.data["error"] == "Replay authorization was rejected"


def test_checkpoint_survives_engine_restart_without_repeating_actions(harness):
    _start(harness)
    first = harness.authorize_and_resume()
    assert first.data["pending_action"]["step_id"] == "fill-email"
    completed_action_count = len(harness.controllers[0].actions)

    harness.restart_engine()
    second = harness.authorize_and_resume()
    assert second.data["pending_action"]["step_id"] == "submit-form"
    assert len(harness.controllers[0].actions) == completed_action_count + 1
    assert second.data["checkpoints"][0]["state"] == "completed"


def test_cancellation_stops_before_dispatch(harness):
    _start(harness)
    task_id = harness.parent_call.task_id
    harness.cancellation.cancelled_tasks.add(task_id)
    result = harness.authorize_and_resume()
    assert result.data["status"] == "needs_attention"
    assert harness.controllers[0].actions == []
    assert harness.sessions.on_task_cancelled(harness.parent_call) is True
    assert harness.controllers[0].is_started is False
    assert harness.sessions.get_session(task_id) is None


def test_session_loss_requires_attention_without_dispatch(harness):
    _start(harness)
    harness.sessions.close_session(harness.parent_call)
    result = harness.authorize_and_resume()
    assert result.data["status"] == "needs_attention"
    assert "continuity" in result.data["error"].casefold()
    assert harness.controllers[0].actions == []


def test_uncertain_action_is_journaled_and_never_repeated(harness):
    started = _start(harness)
    controller = harness.controllers[0]
    controller.uncertain_action = "navigate"
    result = harness.authorize_and_resume()
    assert result.data["status"] == "needs_attention"
    assert result.data["checkpoints"][0]["state"] == ReplayActionJournalState.OUTCOME_UNKNOWN
    assert len(controller.actions) == 1

    # Retrying with the old one-time continuation is a read of the current
    # attention state, never a second browser dispatch.
    authorization = harness.issue_authorization(approved=False)
    harness.prepare_resume(authorization)
    again = harness.resume(harness.parent_call.task_id)
    assert again.data["status"] == "needs_attention"
    assert len(controller.actions) == 1


def test_session_manager_and_adapter_keep_task_sessions_isolated(harness):
    first_task = uuid4()
    second_task = uuid4()
    first_call = ToolCall(
        task_id=first_task, step_id="nav-1", tool_name="browser.navigate",
        arguments={"url": "https://one.example.test"},
    )
    second_call = ToolCall(
        task_id=second_task, step_id="nav-2", tool_name="browser.navigate",
        arguments={"url": "https://two.example.test"},
    )
    first_result = harness.adapter.navigate(
        BrowserNavigateInput(url="https://one.example.test"), first_call
    )
    second_result = harness.adapter.navigate(
        BrowserNavigateInput(url="https://two.example.test"), second_call
    )
    assert first_result.success and second_result.success
    assert len(harness.controllers) == 2
    assert harness.controllers[0].actions == [("navigate", "https://one.example.test/")]
    assert harness.controllers[1].actions == [("navigate", "https://two.example.test/")]
    first_session = harness.sessions.get_session(first_task)
    second_session = harness.sessions.get_session(second_task)
    assert first_session.session_id != second_session.session_id
    assert harness.sessions.validate_session(first_call, second_session.session_id) is False
    assert harness.sessions.validate_session(second_call, first_session.session_id) is False


def test_session_cleanup_is_idempotent(harness):
    _start(harness)
    call = harness.parent_call
    assert harness.sessions.close_session(call) is True
    assert harness.sessions.close_session(call) is False
    assert harness.controllers[0].is_started is False
    assert harness.sessions.get_session(call.task_id) is None


def test_public_results_context_and_registered_names_exclude_secrets(harness):
    started = _start(harness)
    public = str(started.model_dump())
    context = harness.policy_events[0][1]
    assert SECRET_EMAIL not in public
    assert SECRET_EMAIL not in repr(context)
    assert SECRET_EMAIL not in str(context.model_dump())
    assert {binding.definition.tool_name for binding in harness.adapter.bindings} == {
        "browser.navigate", "browser.click", "browser.fill", "browser.record",
        "browser.replay", "browser.download",
    }
    assert len(harness.adapter.bindings) == 6


def test_trusted_authorization_is_not_accepted_from_replay_input(harness):
    with pytest.raises(ValidationError):
        BrowserReplayInput.model_validate({"operation": "resume", "authorization": "planner"})
