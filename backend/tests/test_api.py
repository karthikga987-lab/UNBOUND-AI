from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
from threading import Event
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
import pytest

from backend.app.api.runtime import TaskRuntime
from backend.app.assistant.intent import AssistantIntent, AssistantIntentCategory
from backend.app.llm.provider import LLMConfigurationError, LLMProviderError, LLMResponseFormatError
from backend.app.main import create_app
from backend.app.orchestrator.intent import TaskIntent
from backend.app.orchestrator.planner import PlanDraft
from backend.app.orchestrator.task_manager import SQLiteTaskRepository, TaskManager
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

    def generate_json(self, *, system, prompt, response_model, **kwargs):
        value = next(self.results)
        return response_model.model_validate(value.model_dump())


def task_execution_intent():
    return AssistantIntent(
        category=AssistantIntentCategory.ACTION_EXECUTION,
        current_turn_authorizes_execution=True,
    )


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
    runtime = TaskRuntime(provider=FakeProvider([task_execution_intent(), intent, draft]), verifier=verifier)
    calls = []
    register_search(runtime, calls, verified=verified)
    return runtime, calls


def test_application_starts_without_llm_credentials_and_health_is_honest(monkeypatch):
    from backend.app.api.routes.health import _chromium_binary_identity

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("UNBOUND_S3_SYNC_ENABLED", "0")
    client = TestClient(create_app())
    response = client.get("/api/health")
    assert response.status_code == 200
    chromium_path, chromium_hash = _chromium_binary_identity()
    backend_executable = str(Path(sys.executable).resolve())
    assert response.json() == {
        "status": "ok",
        "llm_configured": False,
        "tools_registered": True,
        "registered_tools_count": 4,
        "notification_agent_installed": False,
        "notification_agent_running": False,
        "browser_public_execution_enabled": False,
        "browser_integration_status": "not_initialized",
        "browser_local_demo_enabled": False,
        "browser_egress_proxy_active": False,
        "browser_egress_proxy_endpoint": None,
        "browser_egress_proxy_pid": None,
        "browser_egress_proxy_executable": None,
        "browser_egress_proxy_sha256": None,
        "browser_egress_proxy_identity_verified": False,
        "browser_egress_proxy_validation_working": False,
        "browser_chromium_executable": chromium_path,
        "browser_chromium_sha256": chromium_hash,
        "browser_backend_pid": os.getpid(),
        "browser_backend_executable": backend_executable,
        "browser_backend_identity_verified": Path(backend_executable).is_file(),
        "browser_wfp_policy_installed": False,
        "browser_chromium_identity_verified": False,
        "browser_ipv4_containment_verified": False,
        "browser_ipv6_containment_verified": False,
        "browser_direct_bypass_blocked": False,
        "browser_process_containment_verified": False,
        "browser_public_execution_status": "blocked_missing_process_containment",
        "s3_sync": {
            "enabled": False,
            "status": "disabled",
            "failure_code": None,
            "pending_count": 0,
            "retry_count": 0,
            "last_success_at": None,
            "last_error_code": None,
        },
    }


def test_s3_sync_routes_require_trusted_origin_confirmation_and_opt_in(monkeypatch):
    monkeypatch.setenv("UNBOUND_S3_SYNC_ENABLED", "0")
    client = TestClient(create_app())

    status = client.get("/api/storage/s3/status")
    assert status.status_code == 200
    assert status.json()["status"] == "disabled"
    assert client.get("/api/storage/s3/backups").json() == []

    no_origin = client.post("/api/storage/s3/sync", json={"confirm_sync": True})
    assert no_origin.status_code == 403
    unconfirmed = client.post(
        "/api/storage/s3/sync", json={"confirm_sync": False},
        headers={"Origin": "http://127.0.0.1:5173"},
    )
    assert unconfirmed.status_code == 400
    disabled = client.post(
        "/api/storage/s3/sync", json={"confirm_sync": True},
        headers={"Origin": "http://127.0.0.1:5173"},
    )
    assert disabled.status_code == 503


def test_application_snapshot_restore_accepts_only_the_configured_sync_prefix(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("UNBOUND_TASK_DB", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setenv("UNBOUND_S3_SYNC_ENABLED", "0")
    runtime = TaskRuntime(manager=TaskManager(SQLiteTaskRepository(tmp_path / "tasks.sqlite3")))
    app = create_app(runtime)
    calls = []

    class FakeSyncService:
        sync_prefix = "unbound/phase5-verification"
        state = SimpleNamespace(path=tmp_path / "sync-state.sqlite3")

        def stop(self):
            pass

        def restore_snapshot_to_new_directory(self, key, destination, *, explicitly_approved):
            calls.append((key, explicitly_approved))
            return {"verified": True, "object_key": key, "destination": str(destination)}

    runtime.s3_sync_service = FakeSyncService()
    key = "unbound/phase5-verification/snapshot-" + ("a" * 64) + ".zip"
    with TestClient(app) as client:
        headers = {"Origin": "http://127.0.0.1:5173"}
        rejected = client.post(
            "/api/storage/s3/restore",
            json={"object_key": "unbound/other/snapshot-" + ("a" * 64) + ".zip", "confirm_restore": True},
            headers=headers,
        )
        assert rejected.status_code == 400
        restored = client.post(
            "/api/storage/s3/restore",
            json={"object_key": key, "confirm_restore": True},
            headers=headers,
        )
    assert restored.status_code == 200, restored.text
    assert restored.json()["verified"] is True
    assert calls == [(key, True)]


def test_unavailable_s3_profile_does_not_prevent_application_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("UNBOUND_TASK_DB", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setenv("UNBOUND_S3_SYNC_ENABLED", "1")
    monkeypatch.setenv("UNBOUND_S3_PROFILE", "unbound-profile-that-does-not-exist")
    with TestClient(create_app()) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["s3_sync"]["enabled"] is False
        assert response.json()["s3_sync"]["status"] == "unavailable"


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


def test_task_history_endpoint_returns_public_task_snapshots():
    runtime, _ = planned_runtime()
    client = TestClient(create_app(runtime))
    created = client.post("/api/tasks", json={"user_request": "Find the quarterly report"})
    assert created.status_code == 200

    history = client.get("/api/tasks?limit=10")
    assert history.status_code == 200
    assert [task["task_id"] for task in history.json()] == [created.json()["task_id"]]
    assert history.json()[0]["user_request"] == "Find the quarterly report"


def test_conversational_question_is_rejected_by_task_endpoint_without_creating_task():
    runtime = TaskRuntime(provider=FakeProvider([
        AssistantIntent(category=AssistantIntentCategory.INFORMATION),
    ]))
    client = TestClient(create_app(runtime))

    response = client.post(
        "/api/tasks",
        json={"user_request": "Hello, UNBOUND. Are you connected to my local model?"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "not_a_task_request"
    assert "POST /api/assistant/requests" in response.json()["detail"]["message"]
    assert runtime.manager.repository._tasks == {}


def test_actionable_request_is_classified_then_planned_by_existing_workflow():
    runtime, _ = planned_runtime()
    response = TestClient(create_app(runtime)).post(
        "/api/tasks", json={"user_request": "Find the quarterly report"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert [step["tool_name"] for step in response.json()["plan"]] == ["document.search"]


def test_manual_task_creation_does_not_execute_until_explicit_execute_request():
    runtime, calls = planned_runtime()
    client = TestClient(create_app(runtime))

    created = client.post(
        "/api/tasks", json={"user_request": "Find the quarterly report", "auto_execute": False}
    )

    assert created.status_code == 200
    assert created.json()["status"] == "created"
    assert calls == []
    executed = client.post(f"/api/tasks/{created.json()['task_id']}/execute")
    assert executed.status_code == 200
    assert executed.json()["status"] == "completed"
    assert len(calls) == 1


def test_auto_execute_runs_only_after_a_valid_plan_is_created():
    runtime, calls = planned_runtime()
    response = TestClient(create_app(runtime)).post(
        "/api/tasks", json={"user_request": "Find the quarterly report", "auto_execute": True}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert len(response.json()["plan"]) == 1
    assert len(calls) == 1


def test_provider_connection_failure_is_explicit_and_creates_no_task():
    class FailingProvider:
        def generate_json(self, **kwargs):
            raise LLMProviderError(
                "transport failed internally",
                public_message="Cannot connect to the language model service. Check that Ollama is running.",
            )

    runtime = TaskRuntime(provider=FailingProvider())
    response = TestClient(create_app(runtime)).post(
        "/api/tasks", json={"user_request": "Find the quarterly report"}
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "llm_provider_error"
    assert "Ollama is running" in response.json()["detail"]["message"]
    assert "transport failed internally" not in response.text
    assert runtime.manager.repository._tasks == {}


def test_provider_connection_failure_during_plan_generation_is_explicit():
    class FailingPlanProvider:
        def generate_json(self, *, response_model, **kwargs):
            if response_model is AssistantIntent:
                return task_execution_intent()
            if response_model is TaskIntent:
                return TaskIntent(goal="Find the quarterly report")
            raise LLMProviderError(
                "connection reset while generating plan",
                public_message="Cannot connect to the language model service. Check that Ollama is running.",
            )

    runtime = TaskRuntime(provider=FailingPlanProvider())
    register_search(runtime, [])
    response = TestClient(create_app(runtime)).post(
        "/api/tasks", json={"user_request": "Find the quarterly report"}
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "llm_provider_error"
    assert "Ollama is running" in response.json()["detail"]["message"]
    assert runtime.manager.repository._tasks == {}


@pytest.mark.parametrize(
    "draft_payload",
    [
        {"missing_information": ["Which report?"]},  # required top-level steps is absent
        {"steps": [{"step_id": "s1", "tool_name": "document.search"}], "missing_information": []},  # incomplete TaskStep
        {"steps": [], "missing_information": [{"field": "report period"}]},  # entries must be strings
    ],
)
def test_malformed_planner_response_is_explicit_not_an_empty_clarification_task(draft_payload):
    class MalformedPlannerProvider:
        def generate_json(self, *, response_model, **kwargs):
            if response_model is AssistantIntent:
                return task_execution_intent()
            if response_model is TaskIntent:
                return TaskIntent(goal="Find the quarterly report")
            if response_model is PlanDraft:
                # Simulate strict provider-side Pydantic validation. Invalid
                # output must never become an executable or empty clarification task.
                return response_model.model_validate(draft_payload)
            raise AssertionError(f"Unexpected structured response model: {response_model!r}")

    runtime = TaskRuntime(provider=MalformedPlannerProvider())
    calls = []
    register_search(runtime, calls)
    response = TestClient(create_app(runtime)).post(
        "/api/tasks", json={"user_request": "Find the quarterly report", "auto_execute": True}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_plan"
    assert runtime.manager.repository._tasks == {}
    assert calls == []


def test_assistant_request_maps_invalid_structured_plan_to_safe_error_without_creating_task():
    class InvalidPlanProvider:
        def generate_json(self, *, response_model, **kwargs):
            if response_model is AssistantIntent:
                return AssistantIntent(category=AssistantIntentCategory.ACTION_PLANNING)
            if response_model is TaskIntent:
                return TaskIntent(goal="Search the synthetic report")
            if response_model is PlanDraft:
                raise LLMResponseFormatError("private schema detail")
            raise AssertionError(f"Unexpected structured response model: {response_model!r}")

    runtime = TaskRuntime(provider=InvalidPlanProvider())
    calls = []
    register_search(runtime, calls)
    response = TestClient(create_app(runtime)).post(
        "/api/assistant/requests", json={"message": "Search the synthetic report"}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_plan"
    assert "private schema detail" not in response.text
    assert runtime.manager.repository._tasks == {}
    assert calls == []


def test_assistant_request_valid_plan_is_prepared_without_execution():
    provider = FakeProvider([
        AssistantIntent(category=AssistantIntentCategory.ACTION_PLANNING),
        TaskIntent(goal="Search a synthetic report"),
        PlanDraft(steps=[TaskStep(
            step_id="step_1", tool_name="document.search", description="Search synthetic reports",
            input={"query": "synthetic quarterly report"}, expected_outcome="A matching report is found",
        )], missing_information=[]),
    ])
    runtime = TaskRuntime(provider=provider)
    calls = []
    register_search(runtime, calls)

    response = TestClient(create_app(runtime)).post(
        "/api/assistant/requests", json={"message": "Search synthetic quarterly reports"}
    )

    assert response.status_code == 200
    assert response.json()["task"]["status"] == "created"
    assert response.json()["task"]["plan"][0]["tool_name"] == "document.search"
    assert calls == []


def test_assistant_request_missing_plan_information_asks_clarification_without_task():
    provider = FakeProvider([
        AssistantIntent(category=AssistantIntentCategory.ACTION_PLANNING),
        TaskIntent(goal="Search the report"),
        PlanDraft(steps=[], missing_information=["Which report period should I search?"]),
    ])
    runtime = TaskRuntime(provider=provider)
    calls = []
    register_search(runtime, calls)

    response = TestClient(create_app(runtime)).post(
        "/api/assistant/requests", json={"message": "Search for the report"}
    )

    assert response.status_code == 200
    assert response.json()["task"] is None
    assert response.json()["intent"]["category"] == "clarification_required"
    assert "Which report period should I search?" in response.json()["reply"]
    assert runtime.manager.repository._tasks == {}
    assert calls == []


def test_task_plan_records_intent_and_plan_clarification_diagnostics_separately():
    runtime = TaskRuntime(provider=FakeProvider([
        AssistantIntent(category=AssistantIntentCategory.ACTION_PLANNING),
        TaskIntent(goal="Find a report", missing_information=["report period"]),
        PlanDraft(steps=[], missing_information=["target document collection"]),
    ]))
    register_search(runtime, [])
    client = TestClient(create_app(runtime))

    created = client.post("/api/tasks", json={"user_request": "Plan a report search"})

    assert created.status_code == 200
    assert created.json()["status"] == "needs_attention"
    events = client.get(f"/api/tasks/{created.json()['task_id']}/events").json()
    by_type = {event["event_type"]: event["message"] for event in events}
    assert by_type["planning.intent_needs_information"] == (
        "Intent extraction requested 1 clarification item(s)."
    )
    assert by_type["planning.plan_needs_information"] == (
        "Plan generation requested 1 clarification item(s)."
    )


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
    assert result["status"] == "needs_attention"
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
    assert second_result.status.value == "needs_attention"
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
        task_execution_intent(),
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
    assert approved.json()["status"] == "created"
    assert calls == []
    assert approved.json()["plan"][0]["status"] == "pending"
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
        task_execution_intent(),
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
    assert created.json()["status"] == "needs_attention"
    assert created.json()["plan"][0]["status"] == "failed"
    assert len(calls) == 1


def test_missing_information_is_returned_without_execution():
    runtime = TaskRuntime(provider=FakeProvider([
        task_execution_intent(),
        TaskIntent(goal="Find a report", missing_information=["Which year?"]),
        PlanDraft(steps=[], missing_information=[]),
    ]))
    response = TestClient(create_app(runtime)).post(
        "/api/tasks", json={"user_request": "Find a report", "auto_execute": True}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "needs_attention"
    assert response.json()["result"]["missing_information"] == [
        "Which year?", "No tools are currently registered to perform this task."
    ]


def test_unavailable_tools_never_produce_false_success():
    runtime = TaskRuntime(provider=FakeProvider([task_execution_intent(), TaskIntent(goal="Open a website")]))
    client = TestClient(create_app(runtime))
    created = client.post("/api/tasks", json={"user_request": "Open a website"})
    assert created.status_code == 200
    task_id = created.json()["task_id"]
    executed = client.post(f"/api/tasks/{task_id}/execute")
    assert executed.status_code == 200
    assert executed.json()["status"] == "needs_attention"
    assert executed.json()["status"] != "completed"

