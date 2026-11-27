from __future__ import annotations

import base64
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.browser.tools import BrowserNavigateInput, BrowserReplayInput
from backend.app.browser.worker_dispatch import WorkerDispatcher
from backend.app.browser.worker_protocol import (
    Click,
    ExtractVisible,
    Navigate,
    Screenshot,
    WorkerResponse,
    WorkerResult,
)
from backend.app.orchestrator.executor import TaskExecutor
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.orchestrator.tool_registry import ToolRegistry
from backend.app.schemas.contracts import (
    ApprovalStatus,
    EvidenceItem,
    EvidenceSourceType,
    TaskCreateRequest,
    TaskStatus,
    TaskStep,
    ToolCall,
    ToolDefinition,
    ToolResult,
)


PNG = b"\x89PNG\r\n\x1a\nsynthetic png bytes"


def approved(call: ToolCall):
    from backend.app.schemas.contracts import ApprovalRequest

    return ApprovalRequest(
        task_id=call.task_id,
        step_id=call.step_id,
        tool_name=call.tool_name,
        description="Approved exact browser action",
        arguments=call.arguments,
        status=ApprovalStatus.APPROVED,
    )


class FakeWorkerClient:
    enabled = True
    worker_id = "synthetic-worker"

    def __init__(self, *, fail_on=None, action_success=True):
        self.requests = []
        self.fail_on = fail_on
        self.action_success = action_success

    def observe(self, request):
        self.requests.append(request)
        if self.fail_on == request.operation.operation:
            raise OSError("synthetic worker disconnect")
        source = "https://example.com/products?private=value"
        operation = request.operation
        if isinstance(operation, Navigate):
            source = operation.url
            result = WorkerResult(success=self.action_success, current_url=source, title="Example") if self.action_success else WorkerResult(success=False, failure_code="worker_operation_failed")
        elif isinstance(operation, Click):
            result = WorkerResult(success=True, current_url=source, title="Example details")
        elif isinstance(operation, ExtractVisible):
            result = WorkerResult(success=True, current_url=source, title="Example details", visible_text="Observed synthetic page text")
        elif isinstance(operation, Screenshot):
            result = WorkerResult(success=True, current_url=source, title="Example details", screenshot_base64=base64.b64encode(PNG).decode("ascii"))
        else:
            result = WorkerResult(success=True)
        response = WorkerResponse(
            request_sha256="a" * 64,
            request_id=request.request_id,
            task_id=request.task_id,
            step_id=request.step_id,
            session_id=request.session_id,
            worker_id=request.worker_id,
            completed_at=request.issued_at,
            result=result,
        )
        return SimpleNamespace(
            response=response,
            receipt_id=uuid4(),
            request_sha256="a" * 64,
            response_sha256="b" * 64,
            worker_key_id="c" * 64,
            received_at=request.issued_at,
        )


def browser_registry(tool_name="browser.navigate", *, execute=None, input_model=None):
    if input_model is None:
        input_model = BrowserNavigateInput
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            tool_name=tool_name,
            description="Browser action",
            input_schema=input_model.model_json_schema(),
            requires_approval=False,
        ),
        input_model,
        execute or (lambda *_args: ToolResult(success=True, data={"local": True})),
    )
    return registry


def task_for(manager, tool="browser.navigate", arguments=None, input_model=None):
    if arguments is None:
        arguments = {"url": "https://example.com/products?private=value"}
    return manager.create(
        TaskCreateRequest(user_request="Use the remote browser"),
        [TaskStep(
            step_id="step_1",
            tool_name=tool,
            description="Perform the planned browser operation",
            input=arguments,
            expected_outcome="The page observation is independently verified",
        )],
    )


def test_dispatch_requires_exact_approval_and_binds_all_observations():
    client = FakeWorkerClient()
    dispatcher = WorkerDispatcher(client, enabled=True)
    call = ToolCall(
        task_id=uuid4(), step_id="step_1", tool_name="browser.navigate",
        arguments={"url": "https://example.com/products?private=value"},
    )
    exact = approved(call)
    claimed = []
    result = dispatcher.execute(call, exact, before_execute=lambda: claimed.append(True) or True)

    assert result.success is True
    assert claimed == [True]
    assert len(client.requests) == 3
    assert all(request.task_id == call.task_id for request in client.requests)
    assert all(request.step_id == call.step_id for request in client.requests)
    assert all(request.session_id == client.requests[0].session_id for request in client.requests)
    assert all(request.approval_id == exact.approval_id for request in client.requests)
    assert [request.operation.operation for request in client.requests] == ["navigate", "extract_visible", "screenshot"]
    assert result.data["page_observation"]["url"] == "https://example.com/products"
    assert "private=value" not in result.data["page_observation"]["url"]
    assert result.data["page_observation"]["screenshot_sha256"]
    assert result.data["page_observation"]["content_trust"] == "untrusted_webpage_data"
    assert len(result.evidence) == 2
    assert all(item.task_id == call.task_id for item in result.evidence)
    assert result.evidence[0].source_type == EvidenceSourceType.BROWSER
    assert result.evidence[1].excerpt.startswith("PNG SHA-256:")
    assert dispatcher.verify_result("browser.navigate", "anything", result) is False


def test_dispatch_rejects_missing_or_mismatched_approval_without_worker_call():
    client = FakeWorkerClient()
    dispatcher = WorkerDispatcher(client, enabled=True)
    call = ToolCall(task_id=uuid4(), step_id="step_1", tool_name="browser.navigate", arguments={"url": "https://example.com/"})
    wrong = approved(call.model_copy(update={"step_id": "step_2"}))

    missing = dispatcher.execute(call, None)
    mismatched = dispatcher.execute(call, wrong)

    assert missing.success is False
    assert missing.data["failure_code"] == "approval_required"
    assert mismatched.success is False
    assert mismatched.data["failure_code"] == "approval_required"
    assert client.requests == []


@pytest.mark.parametrize("url", [
    "http://example.com/",
    "https://example.com.evil.invalid/",
    "https://evil.example.com/",
    "https://127.0.0.1/",
    "https://example.com:8443/",
])
def test_dispatch_rejects_non_allowlisted_destinations_before_claim(url):
    client = FakeWorkerClient()
    dispatcher = WorkerDispatcher(client, enabled=True)
    call = ToolCall(task_id=uuid4(), step_id="step_1", tool_name="browser.navigate", arguments={"url": url})
    claimed = []

    result = dispatcher.execute(call, approved(call), before_execute=lambda: claimed.append(True) or True)

    assert result.success is False
    assert result.data["failure_code"] == "invalid_destination"
    assert claimed == []
    assert client.requests == []


def test_dispatch_failure_after_action_is_unknown_and_not_retried():
    client = FakeWorkerClient(fail_on="extract_visible")
    dispatcher = WorkerDispatcher(client, enabled=True)
    call = ToolCall(task_id=uuid4(), step_id="step_1", tool_name="browser.navigate", arguments={"url": "https://example.com/"})

    with pytest.raises(Exception, match="outcome is unknown"):
        dispatcher.execute(call, approved(call))

    assert [request.operation.operation for request in client.requests] == ["navigate", "extract_visible"]


def test_worker_disabled_fails_closed_before_claim():
    client = FakeWorkerClient()
    dispatcher = WorkerDispatcher(client, enabled=False)
    call = ToolCall(task_id=uuid4(), step_id="step_1", tool_name="browser.navigate", arguments={"url": "https://example.com/"})
    claimed = []

    result = dispatcher.execute(call, approved(call), before_execute=lambda: claimed.append(True) or True)

    assert result.success is False
    assert result.data["failure_code"] == "worker_disabled"
    assert not claimed
    assert client.requests == []


def test_remote_selected_task_executor_requires_approval_and_never_uses_local_callable():
    manager = TaskManager()
    local_calls = []
    task = task_for(manager)
    worker_client = FakeWorkerClient()
    dispatcher = WorkerDispatcher(
        worker_client,
        verifier=lambda _tool, _expected, _result: False,
        enabled=True,
    )
    executor = TaskExecutor(manager, browser_registry(execute=lambda *_args: local_calls.append(True)), remote_browser_selected=True)
    executor.configure_browser_dispatcher(dispatcher, remote_selected=True)

    awaiting = executor.run(task.task_id)
    assert awaiting.status == TaskStatus.AWAITING_APPROVAL
    assert worker_client.requests == []
    assert local_calls == []
    approval = awaiting.pending_approvals[0]
    manager.resolve_approval(task.task_id, approval.approval_id, ApprovalStatus.APPROVED)

    result = executor.run(task.task_id)

    assert result.status == TaskStatus.NEEDS_ATTENTION
    # Navigate, independent text/screenshot observation, then terminal cleanup.
    assert len(worker_client.requests) == 4
    assert worker_client.requests[-1].operation.operation == "close"
    assert local_calls == []
    assert len(result.evidence) == 2
    assert result.plan[0].verification["verified"] is False
    assert result.plan[0].verification["method"] == "independent_worker_verifier"


def test_remote_selected_but_missing_dispatcher_never_falls_back_to_local():
    manager = TaskManager()
    local_calls = []
    task = task_for(manager)
    executor = TaskExecutor(manager, browser_registry(execute=lambda *_args: local_calls.append(True)), remote_browser_selected=True)

    awaiting = executor.run(task.task_id)
    approval = awaiting.pending_approvals[0]
    manager.resolve_approval(task.task_id, approval.approval_id, ApprovalStatus.APPROVED)
    result = executor.run(task.task_id)

    assert result.status == TaskStatus.FAILED
    assert result.error == "Remote browser execution is selected, but no worker is configured."
    assert local_calls == []


def test_remote_replay_is_intercepted_and_never_calls_local_replay():
    manager = TaskManager()
    local_calls = []
    task = task_for(
        manager,
        "browser.replay",
        {"operation": "resume"},
        BrowserReplayInput,
    )
    executor = TaskExecutor(
        manager,
        browser_registry("browser.replay", execute=lambda *_args: local_calls.append(True), input_model=BrowserReplayInput),
        remote_browser_selected=True,
    )

    awaiting = executor.run(task.task_id)
    assert awaiting.status == TaskStatus.AWAITING_APPROVAL
    approval = awaiting.pending_approvals[0]
    manager.resolve_approval(task.task_id, approval.approval_id, ApprovalStatus.APPROVED)
    result = executor.run(task.task_id)

    assert result.status == TaskStatus.FAILED
    assert local_calls == []
