from __future__ import annotations

import base64
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest

from backend.app.browser.state_encryption import ACTIVE_KEY_ENV
from backend.app.browser.workflow_store import WorkflowStore
from backend.app.orchestrator.browser_integration import register_browser_integration
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.orchestrator.tool_registry import ToolRegistry
from backend.app.schemas.contracts import (
    TaskCreateRequest,
    TaskStatus,
    TaskStep,
    StepStatus,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowParameter,
    ApprovalStatus,
)
from backend.tests.browser_integration_harness import SimulatedController


SECRET = "integration-private-value-28f7@example.test"


class BlockingController(SimulatedController):
    def __init__(self):
        super().__init__()
        self.submit_entered = Event()
        self.release_submit = Event()
        self.block_submit = False

    def click(self, target):
        super().click(target)
        if target.get("name") == "Submit" and self.block_submit:
            self.submit_entered.set()
            if not self.release_submit.wait(5):
                raise RuntimeError("test dispatch timeout")
            raise RuntimeError("test-only uncertain external outcome")


@pytest.fixture
def integrated(tmp_path, monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    monkeypatch.setenv(ACTIVE_KEY_ENV, f"integration-test:{key}")
    controllers: list[BlockingController] = []

    def controller_factory():
        controller = BlockingController()
        controllers.append(controller)
        return controller

    workflows = WorkflowStore(tmp_path / "workflows")
    workflow = WorkflowDefinition(
        name="Orchestrated browser flow",
        start_url="https://example.test/start",
        parameters=[WorkflowParameter(name="phrase", type="string", required=True)],
        steps=[
            WorkflowAction(
                step_id="fill-search",
                action="fill",
                target={"type": "label", "value": "Search terms"},
                input={"value": "{{phrase}}"},
            ),
            WorkflowAction(
                step_id="open-dashboard",
                action="click",
                target={"type": "role", "role": "button", "name": "Open dashboard"},
            ),
            WorkflowAction(
                step_id="submit-form",
                action="click",
                target={"type": "role", "role": "button", "name": "Submit"},
            ),
        ],
    )
    workflows.save(workflow)
    manager = TaskManager()
    registry = ToolRegistry()
    runtime = register_browser_integration(
        registry,
        manager,
        lambda _tool, expected, result: expected == "Workflow completed" and result.data.get("status") == "completed",
        controller_factory=controller_factory,
        workflow_store=workflows,
        replay_state_directory=tmp_path / "replay-state",
    )
    return {
        "manager": manager,
        "registry": registry,
        "runtime": runtime,
        "workflow": workflow,
        "controllers": controllers,
        "tmp_path": tmp_path,
    }


def _create_replay_task(env, *, parameters=None):
    workflow = env["workflow"]
    task = env["manager"].create(
        TaskCreateRequest(user_request="Run the saved browser workflow", auto_execute=False),
        [TaskStep(
            step_id="browser-replay-step",
            tool_name="browser.replay",
            description="Replay the saved browser workflow",
            input={
                "operation": "start",
                "workflow_id": str(workflow.workflow_id),
                "parameters": parameters or {"phrase": "demo search"},
            },
            expected_outcome="Workflow completed",
        )],
    )
    return task


def _approve_outer_replay(env, task_id):
    pending = env["runtime"].executor.run(task_id)
    assert pending.status == TaskStatus.AWAITING_APPROVAL, (pending.error, [e.message for e in env["manager"].events(task.task_id)], env["controllers"][0].actions)
    assert pending.pending_approvals
    approval = pending.pending_approvals[0]
    assert approval.tool_name == "browser.replay"
    assert approval.arguments == {}
    env["manager"].resolve_approval(task_id, approval.approval_id, ApprovalStatus.APPROVED)


def test_registered_tools_run_replay_through_real_orchestrator_and_complete(integrated, monkeypatch):
    env = integrated
    task = _create_replay_task(env, parameters={"phrase": SECRET})
    private = env["manager"].get_for_execution(task.task_id)
    private.artifacts.append({"file_ref": r"C:\private\browser\downloads\internal.pdf"})
    env["manager"].save_for_execution(private)
    runtime = env["runtime"]
    assert {item.tool_name for item in env["registry"].list_tools()} == {
        "browser.navigate", "browser.click", "browser.fill", "browser.record", "browser.replay", "browser.download",
    }
    assert len(env["registry"].list_tools()) == 6

    # The outer replay approval occurs before session startup or browser work.
    public_before = env["manager"].get(task.task_id)
    assert SECRET not in public_before.model_dump_json()
    assert r"C:\private\browser\downloads\internal.pdf" not in public_before.model_dump_json()
    assert public_before.artifacts[0]["file_ref"] == "[REDACTED]"
    assert public_before.plan[0].input["parameters"] == {"phrase": "[REDACTED]"}
    observed_resume_calls = []
    original_resolve = runtime.replay_bridge.resolve

    def capture_resume(call):
        observed_resume_calls.append(call)
        return original_resolve(call)

    monkeypatch.setattr(runtime.replay_bridge, "resolve", capture_resume)
    registry_calls = []
    registry_action_deltas = []
    original_execute = env["registry"].execute

    def capture_registry_call(call, **kwargs):
        registry_calls.append(call.model_copy(deep=True))
        controller = env["controllers"][0] if env["controllers"] else None
        before = len(controller.actions) if controller is not None else 0
        result = original_execute(call, **kwargs)
        after = len(controller.actions) if controller is not None else 0
        registry_action_deltas.append((call.arguments.get("operation"), after - before))
        return result

    monkeypatch.setattr(env["registry"], "execute", capture_registry_call)
    _approve_outer_replay(env, task.task_id)
    paused = runtime.executor.run(task.task_id)

    assert paused.status == TaskStatus.AWAITING_APPROVAL, (paused.error, [e.message for e in env["manager"].events(task.task_id)], env["controllers"][0].actions)
    assert paused.plan[0].status == StepStatus.AWAITING_APPROVAL
    assert paused.completed_steps == []
    assert [action[0] for action in env["controllers"][0].actions] == ["navigate", "fill", "click"]
    action_approval = paused.pending_approvals[0]
    assert action_approval.arguments == {}
    assert SECRET not in paused.model_dump_json()

    parent_calls = env["manager"].tool_calls(task.task_id)
    assert len(parent_calls) == 1
    parent_call_id = parent_calls[0].tool_call_id
    env["manager"].resolve_approval(task.task_id, action_approval.approval_id, ApprovalStatus.APPROVED)
    completed = runtime.executor.run(task.task_id)

    assert completed.status == TaskStatus.COMPLETED, (completed.error, [e.message for e in env["manager"].events(task.task_id)], env["controllers"][0].actions)
    assert completed.plan[0].status == StepStatus.COMPLETED
    assert completed.completed_steps == ["browser-replay-step"]
    assert len(env["controllers"][0].actions) == 4
    assert len(observed_resume_calls) == 4
    assert all(call.tool_call_id == parent_call_id for call in observed_resume_calls)
    assert all(call.arguments == {"operation": "resume"} for call in observed_resume_calls)
    assert len(registry_calls) == 5
    assert all(call.tool_name == "browser.replay" for call in registry_calls)
    assert [call.arguments.get("operation") for call in registry_calls] == ["start", "resume", "resume", "resume", "resume"]
    assert registry_action_deltas == [("start", 0), ("resume", 1), ("resume", 1), ("resume", 1), ("resume", 1)]
    assert env["manager"].tool_calls(task.task_id)[0].tool_call_id == parent_call_id
    assert env["runtime"].session_manager.get_session(task.task_id) is None
    assert SECRET not in completed.model_dump_json()


def test_cancel_before_resume_dispatch_prevents_action_and_cleans_session(integrated):
    env = integrated
    task = _create_replay_task(env)
    _approve_outer_replay(env, task.task_id)
    pending = env["runtime"].executor.run(task.task_id)
    assert pending.status == TaskStatus.AWAITING_APPROVAL, (pending.error, [e.message for e in env["manager"].events(task.task_id)], env["controllers"][0].actions)
    action_count = len(env["controllers"][0].actions)

    cancelled = env["manager"].cancel(task.task_id)
    result = env["runtime"].executor.run(task.task_id)

    assert cancelled.status == TaskStatus.CANCELLED
    assert result.status == TaskStatus.CANCELLED
    assert len(env["controllers"][0].actions) == action_count
    assert env["runtime"].session_manager.get_session(task.task_id) is None


def test_dispatch_winning_cancel_gate_persists_uncertainty_without_retry(integrated):
    env = integrated
    task = _create_replay_task(env)
    _approve_outer_replay(env, task.task_id)
    pending = env["runtime"].executor.run(task.task_id)
    approval = pending.pending_approvals[0]
    policy = env["runtime"].replay_bridge.pending_for_task(task.task_id)
    assert policy is not None
    replay_run_id = policy[0].replay_run_id
    env["manager"].resolve_approval(task.task_id, approval.approval_id, ApprovalStatus.APPROVED)

    controller = env["controllers"][0]
    controller.block_submit = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        execution = pool.submit(env["runtime"].executor.run, task.task_id)
        assert controller.submit_entered.wait(3), (env["manager"].get(task.task_id).error, [e.message for e in env["manager"].events(task.task_id)], controller.actions)
        cancellation = pool.submit(env["manager"].cancel, task.task_id)
        assert not cancellation.done(), "cancellation must wait for the dispatch/checkpoint gate"
        controller.release_submit.set()
        task_after_cancel = cancellation.result(timeout=3)
        execution.result(timeout=3)

    state = env["runtime"].replay_state_store.load_result(
        replay_run_id,
        current_workflow=env["workflow"],
    )
    assert task_after_cancel.status == TaskStatus.CANCELLED
    assert state.status == "needs_attention"
    assert state.continuation.uncertain_step_id == "submit-form"
    count = len(controller.actions)
    assert env["runtime"].executor.run(task.task_id).status == TaskStatus.CANCELLED
    assert len(controller.actions) == count
    assert env["runtime"].session_manager.get_session(task.task_id) is None


def test_missing_process_local_replay_context_after_restart_fails_closed(integrated):
    env = integrated
    task = _create_replay_task(env)
    _approve_outer_replay(env, task.task_id)
    paused = env["runtime"].executor.run(task.task_id)
    assert paused.status == TaskStatus.AWAITING_APPROVAL
    action_count = len(env["controllers"][0].actions)

    # Simulate process-local bridge loss while TaskManager still has task state.
    env["runtime"].replay_bridge.forget_task(task.task_id)
    env["manager"].resolve_approval(task.task_id, paused.pending_approvals[0].approval_id, ApprovalStatus.APPROVED)
    recovered = env["runtime"].executor.run(task.task_id)

    assert recovered.status == TaskStatus.NEEDS_ATTENTION
    assert len(env["controllers"][0].actions) == action_count
    assert "continuation" in (recovered.error or "").casefold()


def test_failure_and_shutdown_close_task_sessions(integrated):
    env = integrated
    task = _create_replay_task(env)
    _approve_outer_replay(env, task.task_id)
    runtime = env["runtime"]
    # Force an adapter-level failure after the session was created.
    runtime.workflow_store.delete(env["workflow"].workflow_id)
    failed = runtime.executor.run(task.task_id)
    assert failed.status == TaskStatus.FAILED
    assert runtime.session_manager.get_session(task.task_id) is None

    other = _create_replay_task(env)
    env["runtime"].workflow_store.save(env["workflow"])
    _approve_outer_replay(env, other.task_id)
    waiting = runtime.executor.run(other.task_id)
    assert waiting.status == TaskStatus.AWAITING_APPROVAL
    assert runtime.session_manager.get_session(other.task_id) is not None
    runtime.shutdown()
    runtime.shutdown()
    assert runtime.session_manager.get_session(other.task_id) is None
    assert all(not controller.is_started for controller in env["controllers"])


def test_taskmanager_resume_registry_capability_is_one_use(integrated):
    env = integrated
    task = _create_replay_task(env)
    _approve_outer_replay(env, task.task_id)
    pending = env["runtime"].executor.run(task.task_id)
    assert pending.status == TaskStatus.AWAITING_APPROVAL, (pending.error, [e.message for e in env["manager"].events(task.task_id)], env["controllers"][0].actions)
    context = env["runtime"].replay_bridge.pending_for_task(task.task_id)
    assert context is not None
    # Approve through TaskManager, then obtain the bridge-issued action permit.
    env["manager"].resolve_approval(task.task_id, pending.pending_approvals[0].approval_id, ApprovalStatus.APPROVED)
    authorization = env["runtime"].replay_bridge.issue_authorization(*context)
    assert authorization is not None
    parent = env["runtime"].replay_bridge.recover_parent_tool_call(context[1])
    resume = parent.model_copy(update={"arguments": {"operation": "resume"}})
    first = env["manager"].replay_resume_approval(resume, authorization)
    assert first is not None
    assert env["manager"].replay_resume_approval(resume, authorization) is None
    assert env["manager"].claim_replay_resume(resume, authorization, first)
    assert not env["manager"].claim_replay_resume(resume, authorization, first)

