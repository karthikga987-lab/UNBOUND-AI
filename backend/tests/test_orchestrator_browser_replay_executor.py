from __future__ import annotations

import base64
import os
from threading import Event, Thread

import pytest

from backend.app.browser.replay import WorkflowReplay
from backend.app.browser.replay_state_store import ReplayStateStore
from backend.app.browser.session_manager import BrowserSessionManager
from backend.app.browser.state_encryption import ACTIVE_KEY_ENV, BrowserReplayStateCipher
from backend.app.browser.tools import BrowserToolAdapter
from backend.app.browser.workflow_store import WorkflowStore
from backend.app.orchestrator.browser_replay import BrowserReplayOrchestratorBridge
from backend.app.orchestrator.executor import TaskExecutor
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.orchestrator.tool_registry import ToolInputError, ToolRegistry
from backend.app.schemas.contracts import (
    TaskCreateRequest,
    TaskStep,
    TaskStatus,
    WorkflowAction,
    WorkflowDefinition,
)


class Controller:
    def __init__(self):
        self.is_started = False
        self.actions = []
        self.page = object()
        self.dispatch_started = Event()
        self.release_dispatch = Event()
        self.block_navigation = False
        self.fail_click_after_effect = False

    def start(self):
        self.is_started = True
        return self

    def close(self):
        self.is_started = False

    def navigate(self, url):
        if self.block_navigation:
            self.dispatch_started.set()
            if not self.release_dispatch.wait(timeout=5):
                raise TimeoutError("test controller release timed out")
        self.actions.append(("navigate", url))
        return url

    @property
    def current_url(self):
        return self.actions[-1][1] if self.actions else "https://example.test/"

    def click(self, target):
        self._locator(self.page, target).click()
        self.actions.append(("click", target))
        if self.fail_click_after_effect:
            raise RuntimeError("simulated loss after click side effect")

    def _require_page(self, _operation):
        if not self.is_started:
            raise RuntimeError("controller is not started")
        return self.page

    def _locator(self, _page, _target):
        return Locator()


class Locator:
    def count(self):
        return 1

    def click(self, **_kwargs):
        return None


@pytest.fixture
def replay_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv(
        ACTIVE_KEY_ENV,
        "executor-test:" + base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("="),
    )
    manager = TaskManager()
    workflow = WorkflowDefinition(
        name="Executor replay workflow",
        start_url="https://example.test/start",
        steps=[WorkflowAction(
            step_id="submit",
            action="click",
            target={"type": "role", "role": "button", "name": "Submit"},
        )],
    )
    task = manager.create(
        TaskCreateRequest(user_request="Run the saved browser workflow", auto_execute=False),
        [TaskStep(
            step_id="browser_step",
            tool_name="browser.replay",
            description="Replay the approved saved workflow",
            input={"operation": "start", "workflow_id": str(workflow.workflow_id), "parameters": {}},
            expected_outcome="The saved workflow completed",
        )],
    )
    workflow_store = WorkflowStore(tmp_path / "workflows")
    workflow_store.save(workflow)
    controller = Controller()
    sessions = BrowserSessionManager(lambda: controller)
    bridge = BrowserReplayOrchestratorBridge(manager, workflow_store, sessions)
    state_store = ReplayStateStore(
        tmp_path / "replay-state",
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
    adapter = BrowserToolAdapter(
        workflow_store=workflow_store,
        session_manager=sessions,
        replay_engine=engine,
        policy_handoff=bridge,
        resume_context_provider=bridge,
    )
    registry = ToolRegistry()
    for binding in adapter.bindings:
        registry.register(binding.definition, binding.input_model, binding.execute)
    executor = TaskExecutor(
        manager,
        registry,
        verifier=lambda tool, expected, result: (
            tool == "browser.replay"
            and isinstance(result.data, dict)
            and result.data.get("status") == "completed"
        ),
        browser_replay_bridge=bridge,
    )
    return manager, workflow, task, controller, sessions, bridge, state_store, executor, registry


def _approve_pending(manager, task_id):
    task = manager.get(task_id)
    assert len(task.pending_approvals) == 1
    return manager.resolve_approval(task_id, task.pending_approvals[0].approval_id, "approved")


def test_executor_requires_outer_approval_then_separate_run_and_one_action_per_resume(replay_runtime):
    manager, workflow, task, controller, _sessions, bridge, state_store, executor, registry = replay_runtime
    assert {tool.tool_name for tool in registry.list_tools()} == {
        "browser.navigate", "browser.click", "browser.fill", "browser.record", "browser.replay", "browser.download"
    }

    prepared = executor.run(task.task_id)
    assert prepared.status == TaskStatus.AWAITING_APPROVAL
    assert controller.actions == []
    approved = _approve_pending(manager, task.task_id)
    assert approved.status == TaskStatus.CREATED
    assert controller.actions == []

    started = executor.run(task.task_id)
    assert started.status == TaskStatus.CREATED
    assert bridge.pending_for_task(task.task_id) is not None
    assert controller.actions == []  # start only creates the first checkpoint

    first_resume = executor.run(task.task_id)
    assert first_resume.status == TaskStatus.CREATED
    assert controller.actions == [("navigate", "https://example.test/start")]
    pending = bridge.pending_for_task(task.task_id)
    assert pending is not None and pending[0].step_id == "submit"
    assert state_store.load_result(
        pending[0].replay_run_id,
        current_workflow=workflow,
    ).status == "pending_action"

    waiting_for_sensitive_approval = executor.run(task.task_id)
    assert waiting_for_sensitive_approval.status == TaskStatus.AWAITING_APPROVAL
    assert len(controller.actions) == 1
    approved = _approve_pending(manager, task.task_id)
    assert approved.status == TaskStatus.CREATED
    assert len(controller.actions) == 1  # approval alone does not dispatch

    completed = executor.run(task.task_id)
    assert completed.status == TaskStatus.COMPLETED
    assert controller.actions == [
        ("navigate", "https://example.test/start"),
        ("click", {"type": "role", "role": "button", "name": "Submit"}),
    ]
    assert completed.plan[0].verification["verified"] is True
    assert bridge.pending_for_task(task.task_id) is None


def test_cancel_waits_for_shared_replay_dispatch_lock_and_does_not_replay(replay_runtime):
    manager, _workflow, task, controller, _sessions, bridge, _state_store, executor, _registry = replay_runtime
    assert executor.run(task.task_id).status == TaskStatus.AWAITING_APPROVAL
    _approve_pending(manager, task.task_id)
    assert executor.run(task.task_id).status == TaskStatus.CREATED
    controller.block_navigation = True

    run_result = []
    run_thread = Thread(target=lambda: run_result.append(executor.run(task.task_id)))
    run_thread.start()
    assert controller.dispatch_started.wait(timeout=3)

    cancel_finished = Event()
    cancel_thread = Thread(target=lambda: (manager.cancel(task.task_id), cancel_finished.set()))
    cancel_thread.start()
    assert not cancel_finished.wait(timeout=0.1)
    controller.release_dispatch.set()
    run_thread.join(timeout=3)
    cancel_thread.join(timeout=3)

    assert not run_thread.is_alive() and not cancel_thread.is_alive()
    assert cancel_finished.is_set()
    assert len(controller.actions) == 1
    assert manager.get(task.task_id).status == TaskStatus.CANCELLED
    assert bridge.pending_for_task(task.task_id) is None


def test_task_response_redacts_replay_parameters_but_executor_keeps_private_input(replay_runtime):
    manager, workflow, task, _controller, _sessions, _bridge, _state_store, executor, _registry = replay_runtime
    private_value = "private-query-value"
    task.plan[0].input["parameters"] = {"search": private_value}
    manager.save_for_execution(task)

    public = manager.get(task.task_id)
    internal = manager.get_for_execution(task.task_id)
    assert public.plan[0].input["parameters"] == {"search": "[REDACTED]"}
    assert internal.plan[0].input["parameters"] == {"search": private_value}
    assert private_value not in public.model_dump_json()


def test_planner_cannot_supply_replay_authorization_or_continuation(replay_runtime):
    _manager, _workflow, _task, _controller, _sessions, _bridge, _state_store, _executor, registry = replay_runtime
    with pytest.raises(ToolInputError):
        registry.validate_input("browser.replay", {
            "operation": "resume",
            "authorization": {"one_time_token": "untrusted"},
            "continuation": {"replay_run_id": "00000000-0000-4000-8000-000000000001"},
        })


def test_uncertain_browser_action_is_not_retried(replay_runtime):
    manager, _workflow, task, controller, _sessions, _bridge, _state_store, executor, _registry = replay_runtime
    assert executor.run(task.task_id).status == TaskStatus.AWAITING_APPROVAL
    _approve_pending(manager, task.task_id)
    assert executor.run(task.task_id).status == TaskStatus.CREATED  # replay preparation
    assert executor.run(task.task_id).status == TaskStatus.CREATED  # workflow.start navigation
    assert executor.run(task.task_id).status == TaskStatus.AWAITING_APPROVAL  # sensitive click approval
    _approve_pending(manager, task.task_id)

    controller.fail_click_after_effect = True
    attention = executor.run(task.task_id)
    assert attention.status == TaskStatus.NEEDS_ATTENTION
    assert len([action for action in controller.actions if action[0] == "click"]) == 1

    executor.run(task.task_id, resume=True)
    assert len([action for action in controller.actions if action[0] == "click"]) == 1


def test_missing_process_local_bridge_state_after_restart_fails_closed(replay_runtime):
    manager, _workflow, task, controller, _sessions, bridge, _state_store, executor, _registry = replay_runtime
    assert executor.run(task.task_id).status == TaskStatus.AWAITING_APPROVAL
    _approve_pending(manager, task.task_id)
    assert executor.run(task.task_id).status == TaskStatus.CREATED
    assert manager.tool_calls(task.task_id)

    # Model a restart that retained the TaskManager record but lost the trusted
    # in-process replay association. No new replay may be started automatically.
    bridge.forget_task(task.task_id)
    result = executor.run(task.task_id)
    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert controller.actions == []
    assert len(manager.tool_calls(task.task_id)) == 1
