from __future__ import annotations

from datetime import datetime, timezone
import inspect
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.browser import tools as browser_tools
from backend.app.browser.downloads import BrowserDownloadStore
from backend.app.browser.replay import BrowserSessionBinding
from backend.app.browser.session_manager import BrowserSessionManager
from backend.app.browser.replay_state import (
    ReplayContinuation,
    ReplayExecutionResult,
    ReplayPendingAction,
    ReplayStepAuthorization,
)
from backend.app.browser.tools import (
    BrowserRecordInput,
    BrowserNavigateInput,
    BrowserReplayInput,
    BrowserToolAdapter,
    TrustedReplayResumeContext,
    browser_tool_bindings,
)
from backend.app.browser.workflow_store import WorkflowStore
from backend.app.orchestrator.tool_registry import ApprovalRequiredError, ToolRegistry, ToolInputError
from backend.app.schemas.contracts import ToolCall, WorkflowDefinition


class FakeController:
    def __init__(self, download_store=None):
        self.is_started = True
        self.current_url = "https://example.test/home?token=should-not-leak"
        self.download_store = download_store or BrowserDownloadStore()
        self.actions = []
        self.fill_error = None

    def start(self):
        self.is_started = True
        return self

    def close(self):
        self.is_started = False

    def navigate(self, url):
        self.actions.append(("navigate", url))
        self.current_url = url
        return url

    def click(self, target):
        self.actions.append(("click", target))

    def fill(self, target, value):
        if self.fill_error:
            raise RuntimeError(self.fill_error)
        self.actions.append(("fill", target, value))

    def download(self, target):
        self.actions.append(("download", target))
        return self.download_store.save(FakePlaywrightDownload())


class FakePlaywrightDownload:
    suggested_filename = "..\\CON\u0000report.pdf"

    @staticmethod
    def failure():
        return None

    @staticmethod
    def save_as(path):
        Path(path).write_bytes(b"safe report bytes")


class FakeSessionManager:
    def __init__(self, binding=None):
        self.binding = binding
        self.requested_task_ids = []

    def get_session(self, task_id):
        self.requested_task_ids.append(task_id)
        return self.binding


class FakeRecorder:
    def __init__(self, controller):
        self.controller = controller
        self.started = None

    def start_recording(self, name, *, description="", start_url=None):
        self.started = (name, description, start_url)

    def stop_recording(self):
        return WorkflowDefinition(
            name=self.started[0],
            description=self.started[1],
            start_url=self.started[2] or "https://example.test/home",
        )


class FakeHandoff:
    def __init__(self):
        self.published = []

    def publish(self, pending_action, policy_context, *, workflow_digest, explanation):
        self.published.append((pending_action, policy_context, workflow_digest, explanation))


class FakeReplayEngine:
    def __init__(self, session_id):
        self.session_id = session_id
        self.calls = []

    def start_replay(self, workflow_id, parameters, *, tool_call, policy_handoff):
        self.calls.append(("start", workflow_id, parameters, tool_call, policy_handoff))
        run_id = uuid4()
        continuation = ReplayContinuation(
            replay_run_id=run_id,
            continuation_id=uuid4(),
            task_id=tool_call.task_id,
            session_id=self.session_id,
            workflow_id=workflow_id,
            workflow_digest="a" * 64,
            checkpoint_version=0,
            next_action_index=0,
        )
        return ReplayExecutionResult(
            task_id=tool_call.task_id,
            replay_run_id=run_id,
            workflow_id=workflow_id,
            session_id=self.session_id,
            status="pending_action",
            continuation=continuation,
            pending_action=ReplayPendingAction(
                replay_run_id=run_id,
                continuation_id=continuation.continuation_id,
                workflow_id=workflow_id,
                step_id="workflow.start",
                action="navigate",
                action_digest="b" * 64,
                sequence_index=0,
            ),
        )

    def resume_replay(self, continuation, authorization, *, tool_call):
        self.calls.append(("resume", continuation, authorization, tool_call))
        return ReplayExecutionResult(
            task_id=tool_call.task_id,
            replay_run_id=continuation.replay_run_id,
            workflow_id=continuation.workflow_id,
            session_id=continuation.session_id,
            status="completed",
            continuation=continuation,
        )


class FakeResumeContextProvider:
    def __init__(self, context):
        self.context = context
        self.calls = []

    def resolve(self, tool_call):
        self.calls.append(tool_call)
        return self.context


@pytest.fixture
def adapter_env(tmp_path):
    task_id = uuid4()
    session_id = uuid4()
    controller = FakeController(BrowserDownloadStore(tmp_path / "downloads"))
    sessions = FakeSessionManager(BrowserSessionBinding(session_id, controller))
    workflows = WorkflowStore(tmp_path / "workflows")
    handoff = FakeHandoff()
    replay = FakeReplayEngine(session_id)
    adapter = BrowserToolAdapter(
        workflow_store=workflows,
        session_manager=sessions,
        replay_engine=replay,
        policy_handoff=handoff,
    )
    call = lambda name, arguments: ToolCall(
        task_id=task_id,
        step_id="planned-step",
        tool_name=name,
        arguments=arguments,
    )
    return {
        "adapter": adapter,
        "task_id": task_id,
        "session_id": session_id,
        "controller": controller,
        "sessions": sessions,
        "workflows": workflows,
        "handoff": handoff,
        "replay": replay,
        "call": call,
        "tmp_path": tmp_path,
    }


def test_exposes_exact_six_registry_compatible_definitions(adapter_env):
    adapter = adapter_env["adapter"]
    bindings = browser_tool_bindings(adapter)
    expected = {
        "browser.navigate", "browser.click", "browser.fill", "browser.record",
        "browser.replay", "browser.download",
    }
    assert {binding.definition.tool_name for binding in bindings} == expected
    assert {definition.tool_name for definition in adapter.definitions} == expected
    assert len(bindings) == 6
    for binding in bindings:
        assert binding.definition.input_schema == binding.input_model.model_json_schema()
        assert len(inspect.signature(binding.execute).parameters) == 2


def test_all_bindings_register_and_execute_with_actual_registry(adapter_env):
    registry = ToolRegistry()
    for binding in adapter_env["adapter"].bindings:
        registry.register(binding.definition, binding.input_model, binding.execute)

    call = adapter_env["call"]("browser.navigate", {"url": "https://example.test/path"})
    result = registry.execute(call)
    assert result.success is True
    assert adapter_env["controller"].actions == [("navigate", "https://example.test/path")]
    assert adapter_env["sessions"].requested_task_ids == [adapter_env["task_id"]]


def test_input_validation_rejects_unsupported_urls_unknown_fields_and_bad_targets(adapter_env):
    registry = ToolRegistry()
    for binding in adapter_env["adapter"].bindings:
        registry.register(binding.definition, binding.input_model, binding.execute)

    with pytest.raises(ToolInputError):
        registry.validate_input("browser.navigate", {"url": "file:///etc/passwd"})
    with pytest.raises(ToolInputError):
        registry.validate_input("browser.navigate", {"url": "https://example.test", "task_id": str(uuid4())})
    with pytest.raises(ValidationError):
        BrowserRecordInput.model_validate({"operation": "start"})
    with pytest.raises(ValidationError):
        BrowserReplayInput.model_validate({"operation": "resume", "authorization": "planner-controlled"})
    with pytest.raises(ValidationError):
        BrowserReplayInput.model_validate({"operation": "start"})
    selector_secret = "private-selector-string-834"
    with pytest.raises(ValidationError) as error:
        BrowserReplayInput.model_validate({"operation": "resume", "selector": selector_secret})
    assert selector_secret not in str(error.value)
    with pytest.raises(ValidationError) as target_error:
        browser_tools.BrowserClickInput.model_validate({"target": {"type": selector_secret, "value": selector_secret}})
    assert selector_secret not in str(target_error.value)


def test_missing_session_dependency_returns_safe_unavailable(adapter_env):
    adapter = BrowserToolAdapter(workflow_store=adapter_env["workflows"])
    result = adapter.navigate(
        browser_tools.BrowserNavigateInput(url="https://example.test"),
        adapter_env["call"]("browser.navigate", {"url": "https://example.test"}),
    )
    assert result.success is False
    assert result.error == "Task-scoped browser session is unavailable"

    replay_result = adapter.replay(
        BrowserReplayInput(operation="resume"),
        adapter_env["call"]("browser.replay", {"operation": "resume"}),
    )
    assert replay_result.success is False
    assert replay_result.error == "Task-scoped browser session is unavailable"


def test_adapter_creates_and_reuses_isolated_task_sessions(adapter_env):
    env = adapter_env
    controllers = []

    def factory():
        controller = FakeController()
        controllers.append(controller)
        return controller

    manager = BrowserSessionManager(factory)
    adapter = BrowserToolAdapter(workflow_store=env["workflows"], session_manager=manager)
    first_call = env["call"]("browser.navigate", {"url": "https://one.example.test"})
    second_call = ToolCall(
        task_id=uuid4(), step_id="other-task", tool_name="browser.navigate",
        arguments={"url": "https://two.example.test"},
    )

    first = adapter.navigate(BrowserNavigateInput(url="https://one.example.test"), first_call)
    adapter.navigate(BrowserNavigateInput(url="https://one.example.test/again"), first_call)
    second = adapter.navigate(BrowserNavigateInput(url="https://two.example.test"), second_call)

    assert first.success and second.success
    assert len(controllers) == 2
    assert controllers[0].actions == [
        ("navigate", "https://one.example.test/"),
        ("navigate", "https://one.example.test/again"),
    ]
    assert controllers[1].actions == [("navigate", "https://two.example.test/")]


def test_navigation_click_and_fill_delegate_without_echoing_sensitive_values(adapter_env):
    env = adapter_env
    controller = env["controller"]
    adapter = env["adapter"]
    nav = adapter.navigate(
        browser_tools.BrowserNavigateInput(url="https://example.test/new"),
        env["call"]("browser.navigate", {"url": "https://example.test/new"}),
    )
    click = adapter.click(
        browser_tools.BrowserClickInput(target={"type": "role", "role": "button", "name": "Search"}),
        env["call"]("browser.click", {"target": {"type": "role", "role": "button", "name": "Search"}}),
    )
    secret = "do-not-return-this-password"
    controller.fill_error = f"diagnostic includes {secret}"
    filled = adapter.fill(
        browser_tools.BrowserFillInput(target={"type": "label", "value": "Password"}, value=secret),
        env["call"]("browser.fill", {"target": {"type": "label", "value": "Password"}, "value": secret}),
    )

    assert nav.success and click.success
    assert filled.success is False
    assert secret not in str(filled.model_dump())
    assert "token=should-not-leak" not in str(nav.model_dump())
    assert controller.actions[1] == ("click", {"type": "role", "role": "button", "name": "Search"})


def test_registry_keeps_click_and_replay_approval_boundaries(adapter_env):
    registry = ToolRegistry()
    for binding in adapter_env["adapter"].bindings:
        registry.register(binding.definition, binding.input_model, binding.execute)

    dangerous_click = adapter_env["call"](
        "browser.click", {"target": {"type": "role", "role": "button", "name": "Submit order"}}
    )
    assert registry.action_requires_approval(dangerous_click)
    with pytest.raises(ApprovalRequiredError):
        registry.execute(dangerous_click)
    replay_call = adapter_env["call"]("browser.replay", {"operation": "start", "workflow_id": str(uuid4())})
    assert registry.action_requires_approval(replay_call)
    with pytest.raises(ApprovalRequiredError):
        registry.execute(replay_call)
    assert adapter_env["controller"].actions == []


def test_record_start_and_stop_save_shared_workflow(adapter_env, monkeypatch):
    env = adapter_env
    monkeypatch.setattr(browser_tools, "BrowserRecorder", FakeRecorder)
    start_args = BrowserRecordInput(operation="start", name="Weekly report", start_url="https://example.test/start")
    started = env["adapter"].record(start_args, env["call"]("browser.record", start_args.model_dump(mode="json")))
    assert started.success and started.data == {"recording": True}

    stop_args = BrowserRecordInput(operation="stop")
    stopped = env["adapter"].record(stop_args, env["call"]("browser.record", stop_args.model_dump(mode="json")))
    assert stopped.success
    workflow_id = stopped.data["workflow_id"]
    saved = env["workflows"].load(workflow_id)
    assert saved.name == "Weekly report"
    assert stopped.data["recording"] is False


def test_replay_start_and_resume_use_trusted_context_only(adapter_env):
    env = adapter_env
    workflow = WorkflowDefinition(name="Replay", start_url="https://example.test")
    env["workflows"].save(workflow)
    start_args = BrowserReplayInput(operation="start", workflow_id=workflow.workflow_id)
    start_call = env["call"](
        "browser.replay",
        {"operation": "start", "workflow_id": str(workflow.workflow_id), "parameters": {}},
    )
    started = env["adapter"].replay(start_args, start_call)
    assert started.success
    assert started.data["status"] == "pending_action"
    assert env["replay"].calls[0][0] == "start"
    assert env["replay"].calls[0][-1] is env["handoff"]

    parent_call = start_call
    continuation = env["replay"].calls[0][0] and ReplayContinuation(
        replay_run_id=uuid4(),
        continuation_id=uuid4(),
        task_id=env["task_id"],
        session_id=env["session_id"],
        workflow_id=workflow.workflow_id,
        workflow_digest="c" * 64,
        checkpoint_version=0,
        next_action_index=0,
    )
    now = datetime.now(timezone.utc)
    authorization = ReplayStepAuthorization(
        authorization_id=uuid4(), task_id=env["task_id"],
        tool_call_id=parent_call.tool_call_id, replay_run_id=continuation.replay_run_id,
        continuation_id=continuation.continuation_id, workflow_id=workflow.workflow_id,
        step_id="workflow.start", action_digest="d" * 64,
        decision="allowed_without_approval", issued_at=now,
        expires_at=now.replace(year=now.year + 1), one_time_token="trusted-only-token",
    )
    provider = FakeResumeContextProvider(TrustedReplayResumeContext(continuation, authorization, parent_call))
    env["adapter"].resume_context_provider = provider
    resume_args = BrowserReplayInput(operation="resume")
    resume_call = env["call"]("browser.replay", {"operation": "resume"})
    resumed = env["adapter"].replay(resume_args, resume_call)
    assert resumed.success and resumed.data["status"] == "completed"
    assert provider.calls == [resume_call]
    assert env["replay"].calls[-1][0] == "resume"
    assert env["replay"].calls[-1][2] is authorization
    assert "trusted-only-token" not in str(resumed.model_dump())


def test_replay_resume_rejects_trusted_context_from_another_task(adapter_env):
    env = adapter_env
    workflow = WorkflowDefinition(name="Replay", start_url="https://example.test")
    continuation = ReplayContinuation(
        replay_run_id=uuid4(), continuation_id=uuid4(), task_id=uuid4(),
        session_id=env["session_id"], workflow_id=workflow.workflow_id,
        workflow_digest="c" * 64, checkpoint_version=0, next_action_index=0,
    )
    now = datetime.now(timezone.utc)
    parent = env["call"]("browser.replay", {"operation": "start"})
    authorization = ReplayStepAuthorization(
        authorization_id=uuid4(), task_id=continuation.task_id,
        tool_call_id=parent.tool_call_id, replay_run_id=continuation.replay_run_id,
        continuation_id=continuation.continuation_id, workflow_id=workflow.workflow_id,
        step_id="workflow.start", action_digest="d" * 64,
        decision="allowed_without_approval", issued_at=now,
        expires_at=now.replace(year=now.year + 1), one_time_token="trusted-token",
    )
    env["adapter"].resume_context_provider = FakeResumeContextProvider(
        TrustedReplayResumeContext(continuation, authorization, parent)
    )
    result = env["adapter"].replay(
        BrowserReplayInput(operation="resume"),
        env["call"]("browser.replay", {"operation": "resume"}),
    )
    assert result.success is False
    assert "does not belong" in result.error
    assert not env["replay"].calls


def test_download_saves_and_verifies_generated_file(adapter_env):
    env = adapter_env
    result = env["adapter"].download(
        browser_tools.BrowserDownloadInput(target={"type": "text", "value": "Download"}),
        env["call"]("browser.download", {"target": {"type": "text", "value": "Download"}}),
    )
    assert result.success is True
    assert result.data["downloaded"] is True
    assert result.data["verification"]["success"] is True
    assert Path(env["tmp_path"] / "downloads" / result.data["file_ref"]).is_file()
    assert "CON" not in result.data["file_name"]
