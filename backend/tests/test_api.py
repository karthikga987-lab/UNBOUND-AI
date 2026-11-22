from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import BaseModel
import pytest

from backend.app.api.runtime import TaskRuntime
from backend.app.llm.provider import LLMConfigurationError
from backend.app.main import create_app
from backend.app.orchestrator.intent import TaskIntent
from backend.app.orchestrator.planner import PlanDraft
from backend.app.schemas.contracts import (
    ApprovalStatus,
    TaskCreateRequest,
    TaskStep,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from backend.app.orchestrator.tool_registry import ToolUnavailableError


class FakeProvider:
    def __init__(self, results):
        self.results = iter(results)

    def generate_json(self, *, system, prompt, response_model):
        value = next(self.results)
        return response_model.model_validate(value.model_dump())


class SearchInput(BaseModel):
    query: str


def register_search(runtime: TaskRuntime, calls: list, *, verified: bool = True):
    def execute(arguments: SearchInput, call: ToolCall):
        calls.append(call)
        return ToolResult(success=True, data={"verified": verified, "query": arguments.query})

    runtime.registry.register(
        ToolDefinition(
            tool_name="document.search",
            description="Search documents",
            input_schema=SearchInput.model_json_schema(),
        ),
        SearchInput,
        execute,
    )


def planned_runtime(*, verifier=lambda tool, expected, result: True, verified=True):
    intent = TaskIntent(goal="Find the quarterly report", required_capabilities=["document.search"])
    draft = PlanDraft(steps=[TaskStep(
        step_id="search_report",
        tool_name="document.search",
        description="Search for the quarterly report",
        input={"query": "quarterly report"},
        expected_outcome="The quarterly report is found",
    )])
    runtime = TaskRuntime(provider=FakeProvider([intent, draft]), verifier=verifier)
    calls = []
    register_search(runtime, calls, verified=verified)
    return runtime, calls


def test_application_starts_without_llm_credentials_and_health_is_honest(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    client = TestClient(create_app())
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "llm_configured": False,
        "tools_registered": False,
        "registered_tools_count": 0,
    }


def test_task_creation_retrieval_and_shared_runtime_state():
    runtime, _ = planned_runtime()
    client = TestClient(create_app(runtime))
    created = client.post("/api/tasks", json={"user_request": "Find the quarterly report"})
    assert created.status_code == 200
    task_id = created.json()["task_id"]
    retrieved = client.get(f"/api/tasks/{task_id}")
    assert retrieved.status_code == 200
    assert retrieved.json()["task_id"] == task_id
    assert len(retrieved.json()["plan"]) == 1
    assert client.get("/api/health").json()["registered_tools_count"] == 1


def test_runtime_allows_late_tool_and_verifier_registration():
    runtime = TaskRuntime()
    calls = []
    client = TestClient(create_app(runtime))

    class SearchInput(BaseModel):
        query: str

    task = runtime.manager.create(TaskCreateRequest(user_request="Find the quarterly report"), [TaskStep(
        step_id="search_report", tool_name="document.search", description="Search report",
        input={"query": "quarterly report"}, expected_outcome="Report found",
    )])
    task_id = str(task.task_id)
    runtime.register_tool(
        ToolDefinition(tool_name="document.search", description="Search docs", input_schema=SearchInput.model_json_schema()),
        SearchInput,
        lambda arguments, call: calls.append(call) or ToolResult(success=True, data={"result_count": 1}),
    )

    blocked = client.post(f"/api/tasks/{task_id}/execute").json()
    assert blocked["status"] == "needs_attention"
    assert calls == []

    runtime.configure_verifier("document.search", lambda tool, expected, result: result.data["result_count"] == 1)
    completed = client.post(f"/api/tasks/{task_id}/execute").json()
    assert completed["status"] == "completed"
    assert len(calls) == 1
    assert completed["plan"][0]["verification"]["verified"] is True


def test_runtime_tool_specific_failed_verifier_prevents_completion():
    runtime, calls = planned_runtime()
    # Replace the executor verifier with a tool-specific strategy after initialization.
    runtime.configure_verifier("document.search", lambda tool, expected, result: False)
    client = TestClient(create_app(runtime))
    task_id = client.post("/api/tasks", json={"user_request": "Find the quarterly report"}).json()["task_id"]
    result = client.post(f"/api/tasks/{task_id}/execute").json()
    assert result["status"] == "failed"
    assert result["plan"][0]["status"] == "failed"
    assert len(calls) == 1


def test_tool_specific_verifier_precedes_shared_verifier():
    runtime, calls = planned_runtime(verifier=lambda tool, expected, result: False)
    runtime.configure_verifier("document.search", lambda tool, expected, result: True)
    client = TestClient(create_app(runtime))
    task_id = client.post("/api/tasks", json={"user_request": "Find the quarterly report"}).json()["task_id"]
    result = client.post(f"/api/tasks/{task_id}/execute").json()
    assert result["status"] == "completed"
    assert len(calls) == 1


def test_verifier_cannot_be_configured_for_unregistered_tool():
    runtime = TaskRuntime(verifier=lambda tool, expected, result: True)
    with pytest.raises(ToolUnavailableError):
        runtime.configure_verifier("browser.navigate", lambda tool, expected, result: True)


def test_concurrent_verifier_update_waits_until_active_run_finishes():
    entered_tool, release_tool, update_started, update_finished = Event(), Event(), Event(), Event()
    runtime = TaskRuntime()

    class SearchInput(BaseModel):
        query: str

    calls = []

    def execute(arguments, call):
        calls.append(call)
        if len(calls) == 1:
            entered_tool.set()
            assert release_tool.wait(3)
        return ToolResult(success=True, data={"count": 1})

    runtime.register_tool(
        ToolDefinition(tool_name="document.search", description="Search", input_schema=SearchInput.model_json_schema()),
        SearchInput,
        execute,
        verifier=lambda tool, expected, result: True,
    )

    def new_task():
        return runtime.manager.create(TaskCreateRequest(user_request="Find a report"), [TaskStep(
            step_id="find", tool_name="document.search", description="Find report",
            input={"query": "report"}, expected_outcome="Report found",
        )])

    first = new_task()

    def configure_failed_verifier():
        update_started.set()
        runtime.configure_verifier("document.search", lambda tool, expected, result: False)
        update_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        execution = pool.submit(runtime.executor.run, first.task_id)
        assert entered_tool.wait(3)
        update = pool.submit(configure_failed_verifier)
        assert update_started.wait(3)
        assert not update_finished.wait(0.05)
        release_tool.set()
        first_result = execution.result(timeout=3)
        update.result(timeout=3)

    second_result = runtime.executor.run(new_task().task_id)
    assert first_result.status.value == "completed"
    assert second_result.status.value == "failed"
    assert len(calls) == 2


def test_unknown_task_and_unknown_task_events_return_404():
    client = TestClient(create_app())
    task_id = str(uuid4())
    assert client.get(f"/api/tasks/{task_id}").status_code == 404
    assert client.get(f"/api/tasks/{task_id}/events").status_code == 404
    assert client.post(f"/api/tasks/{task_id}/execute").status_code == 404


def test_invalid_task_ids_and_request_bodies_return_422():
    client = TestClient(create_app())
    assert client.get("/api/tasks/not-a-uuid").status_code == 422
    assert client.post("/api/tasks", json={}).status_code == 422


def test_execute_and_event_retrieval_use_shared_executor():
    runtime, calls = planned_runtime()
    client = TestClient(create_app(runtime))
    task_id = client.post("/api/tasks", json={"user_request": "Find the quarterly report"}).json()["task_id"]
    executed = client.post(f"/api/tasks/{task_id}/execute")
    assert executed.status_code == 200
    assert executed.json()["status"] == "completed"
    assert len(calls) == 1
    events = client.get(f"/api/tasks/{task_id}/events")
    assert events.status_code == 200
    assert any(event["event_type"] == "step.verified" for event in events.json())


def test_cancel_task_updates_state():
    runtime, _ = planned_runtime()
    client = TestClient(create_app(runtime))
    task_id = client.post("/api/tasks", json={"user_request": "Find the quarterly report"}).json()["task_id"]
    cancelled = client.post(f"/api/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_approval_resolution_requires_explicit_decision_and_does_not_auto_execute():
    class ClickInput(BaseModel):
        target: dict

    calls = []
    runtime = TaskRuntime(provider=FakeProvider([
        TaskIntent(goal="Send the message"),
        PlanDraft(steps=[TaskStep(
            step_id="send_message", tool_name="browser.click", description="Send the message",
            input={"target": {"role": "button", "name": "Send message"}},
            expected_outcome="Message sent", requires_approval=True,
        )]),
    ]), verifier=lambda tool, expected, result: True)
    runtime.registry.register(
        ToolDefinition(tool_name="browser.click", description="Click", input_schema=ClickInput.model_json_schema()),
        ClickInput,
        lambda arguments, call: calls.append(call) or ToolResult(success=True, data={}),
    )
    client = TestClient(create_app(runtime))
    task_id = client.post("/api/tasks", json={"user_request": "Send the message"}).json()["task_id"]
    pending = client.post(f"/api/tasks/{task_id}/execute").json()
    approval_id = pending["pending_approvals"][0]["approval_id"]
    approved = client.post(f"/api/tasks/{task_id}/approvals/{approval_id}", json={"decision": "APPROVED"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "executing"
    assert calls == []
    already_resolved = client.post(f"/api/tasks/{task_id}/approvals/{approval_id}", json={"decision": "APPROVED"})
    assert already_resolved.status_code == 404
    assert client.post(f"/api/tasks/{task_id}/execute").json()["status"] == "completed"
    assert len(calls) == 1


def test_invalid_approval_decision_and_unknown_approval():
    runtime, _ = planned_runtime()
    client = TestClient(create_app(runtime))
    task_id = client.post("/api/tasks", json={"user_request": "Find the quarterly report"}).json()["task_id"]
    approval_id = str(uuid4())
    invalid = client.post(f"/api/tasks/{task_id}/approvals/{approval_id}", json={"decision": "MAYBE"})
    assert invalid.status_code == 422
    unknown = client.post(f"/api/tasks/{task_id}/approvals/{approval_id}", json={"decision": "APPROVED"})
    assert unknown.status_code == 404


def test_rejected_approval_is_audited_and_never_executes():
    class ClickInput(BaseModel):
        target: dict

    calls = []
    runtime = TaskRuntime(provider=FakeProvider([
        TaskIntent(goal="Send a message"),
        PlanDraft(steps=[TaskStep(
            step_id="send_message", tool_name="browser.click", description="Send a message",
            input={"target": {"role": "button", "name": "Send message"}},
            expected_outcome="Message sent", requires_approval=True,
        )]),
    ]), verifier=lambda tool, expected, result: True)
    runtime.registry.register(
        ToolDefinition(tool_name="browser.click", description="Click", input_schema=ClickInput.model_json_schema()),
        ClickInput,
        lambda arguments, call: calls.append(call) or ToolResult(success=True, data={}),
    )
    client = TestClient(create_app(runtime))
    task_id = client.post("/api/tasks", json={"user_request": "Send a message"}).json()["task_id"]
    pending = client.post(f"/api/tasks/{task_id}/execute").json()
    approval_id = pending["pending_approvals"][0]["approval_id"]
    rejected = client.post(f"/api/tasks/{task_id}/approvals/{approval_id}", json={"decision": "REJECTED"})
    assert rejected.status_code == 200
    assert rejected.json()["plan"][0]["status"] == "skipped"
    assert client.get(f"/api/tasks/{task_id}/events").json()[-1]["event_type"] == "approval.rejected"
    assert client.post(f"/api/tasks/{task_id}/execute").json()["status"] == "needs_attention"
    assert calls == []


def test_missing_llm_configuration_returns_safe_503(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    runtime = TaskRuntime(provider_factory=lambda: (_ for _ in ()).throw(LLMConfigurationError("No provider configured")))
    response = TestClient(create_app(runtime)).post("/api/tasks", json={"user_request": "Do something"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "llm_unavailable"
    assert "key" not in response.text.lower()


def test_auto_execute_cannot_bypass_missing_verifier_or_tool_verification():
    runtime, calls = planned_runtime(verifier=None)
    client = TestClient(create_app(runtime))
    created = client.post("/api/tasks", json={"user_request": "Find the quarterly report", "auto_execute": True})
    assert created.status_code == 200
    assert created.json()["status"] == "needs_attention"
    assert calls == []

    runtime, calls = planned_runtime(verifier=lambda tool, expected, result: False)
    client = TestClient(create_app(runtime))
    created = client.post("/api/tasks", json={"user_request": "Find the quarterly report", "auto_execute": True})
    assert created.status_code == 200
    assert created.json()["status"] == "failed"
    assert created.json()["plan"][0]["status"] == "failed"
    assert len(calls) == 1


def test_missing_information_is_returned_without_execution():
    runtime = TaskRuntime(provider=FakeProvider([
        TaskIntent(goal="Find a report", missing_information=["Which year?"]),
        PlanDraft(steps=[], missing_information=[]),
    ]))
    response = TestClient(create_app(runtime)).post("/api/tasks", json={"user_request": "Find a report"})
    assert response.status_code == 200
    assert response.json()["status"] == "needs_attention"
    assert response.json()["result"]["missing_information"] == [
        "Which year?", "No tools are currently registered to perform this task."
    ]


def test_unavailable_tools_never_produce_false_success():
    runtime = TaskRuntime(provider=FakeProvider([TaskIntent(goal="Open a website")]))
    client = TestClient(create_app(runtime))
    created = client.post("/api/tasks", json={"user_request": "Open a website"})
    assert created.status_code == 200
    task_id = created.json()["task_id"]
    executed = client.post(f"/api/tasks/{task_id}/execute")
    assert executed.status_code == 200
    assert executed.json()["status"] == "needs_attention"
    assert executed.json()["status"] != "completed"

