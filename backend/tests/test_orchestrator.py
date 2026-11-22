from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, Lock
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from backend.app.llm.provider import LLMConfigurationError, get_provider_from_env
from backend.app.orchestrator.executor import TaskExecutor
from backend.app.orchestrator.intent import TaskIntent, understand_intent
from backend.app.orchestrator.planner import PlanDraft, PlanError, generate_plan
from backend.app.orchestrator.recovery import prepare_resume
from backend.app.orchestrator.task_manager import InvalidApprovalError, TaskManager
from backend.app.orchestrator.tool_registry import ApprovalRequiredError, ToolInputError, ToolRegistry, ToolUnavailableError
from backend.app.safety.permissions import action_requires_approval, requires_approval
from backend.app.schemas.contracts import (
    ApprovalRequest,
    ApprovalStatus,
    EventStatus,
    EvidenceItem,
    EvidenceSourceType,
    StepStatus,
    TaskCreateRequest,
    TaskResponse,
    TaskStatus,
    TaskStep,
    ToolCall,
    ToolDefinition,
    ToolResult,
)


class FakeProvider:
    def __init__(self, results: list[BaseModel]):
        self.results = iter(results)

    def generate_json(self, *, system: str, prompt: str, response_model: type[BaseModel]) -> BaseModel:
        result = next(self.results)
        return response_model.model_validate(result.model_dump())


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str


class ClickInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: dict[str, object]


def successful_search(arguments: BaseModel, call: ToolCall) -> ToolResult:
    evidence = EvidenceItem(
        task_id=call.task_id,
        source_type=EvidenceSourceType.DOCUMENT,
        source_name="indexed report",
        source_url="https://example.test/report",
        excerpt="Quarterly report found",
        supports_claim="The requested report exists",
    )
    return ToolResult(success=True, data={"verified": True, "count": 1}, evidence=[evidence])


def registry(execute=successful_search, *, requires_approval: bool = False, input_schema=None) -> ToolRegistry:
    tools = ToolRegistry()
    schema = input_schema if input_schema is not None else SearchInput.model_json_schema()
    definition = ToolDefinition(
        tool_name="document.search",
        description="Search indexed documents",
        input_schema=schema,
        requires_approval=requires_approval,
    )
    tools.register(definition, SearchInput, execute)
    return tools


def click_registry(execute, *, requires_approval=False) -> ToolRegistry:
    tools = ToolRegistry()
    definition = ToolDefinition(
        tool_name="browser.click",
        description="Click a browser target",
        input_schema=ClickInput.model_json_schema(),
        requires_approval=requires_approval,
    )
    tools.register(definition, ClickInput, execute)
    return tools


def create_task(manager: TaskManager, goal: str, steps: list[TaskStep], *, auto_execute: bool = False, missing_information=None):
    request = TaskCreateRequest(user_request=goal, auto_execute=auto_execute)
    return manager.create(request, steps, missing_information)


def search_step(step_id="s1", description="Search", query="report") -> TaskStep:
    return TaskStep(
        step_id=step_id,
        tool_name="document.search",
        description=description,
        input={"query": query},
        expected_outcome="The requested report is found",
    )


def verifier(tool_name, expected_outcome, result: ToolResult) -> bool:
    return result.success and isinstance(result.data, dict) and result.data.get("verified") is True


def test_intent_parsing_returns_validated_structure():
    expected = TaskIntent(goal="Find the report", required_capabilities=["search"], parameters={"name": "report"})
    intent = understand_intent(TaskCreateRequest(user_request="Find the report"), FakeProvider([expected]))
    assert intent.goal == "Find the report"
    assert intent.parameters["name"] == "report"


def test_plan_uses_registered_shared_tool_and_validates_input():
    intent = TaskIntent(goal="Find a report")
    draft = PlanDraft(steps=[search_step(query="report")])
    plan = generate_plan("Find a report", registry(), FakeProvider([intent, draft]))
    assert [step.tool_name for step in plan.steps] == ["document.search"]
    assert isinstance(plan.steps[0], TaskStep)


def test_planner_carries_registered_approval_requirement_into_step():
    intent = TaskIntent(goal="Search")
    draft = PlanDraft(steps=[search_step()])
    plan = generate_plan("Search", registry(requires_approval=True), FakeProvider([intent, draft]))
    assert plan.steps[0].requires_approval


def test_plan_rejects_unregistered_tool():
    intent = TaskIntent(goal="Do something")
    draft = PlanDraft(steps=[TaskStep(step_id="s1", tool_name="browser.navigate", description="Open", input={}, expected_outcome="Page loaded")])
    with pytest.raises(PlanError, match="unregistered"):
        generate_plan("Do something", registry(), FakeProvider([intent, draft]))


def test_plan_requests_clarification_for_missing_tool_input():
    intent = TaskIntent(goal="Search")
    draft = PlanDraft(steps=[TaskStep(step_id="s1", tool_name="document.search", description="Search", input={}, expected_outcome="Results found")])
    plan = generate_plan("Search", registry(), FakeProvider([intent, draft]))
    assert not plan.steps
    assert "clarify" in plan.missing_information[0]


def test_planner_rejects_duplicate_step_ids():
    intent = TaskIntent(goal="Perform two actions")
    draft = PlanDraft(steps=[search_step("same", query="one"), search_step("same", query="two")])
    with pytest.raises(PlanError, match="duplicate step_id"):
        generate_plan("Perform two actions", registry(), FakeProvider([intent, draft]))


def test_task_creation_rejects_duplicate_step_ids():
    manager = TaskManager()
    with pytest.raises(ValueError, match="Duplicate step_id"):
        create_task(manager, "Search twice", [search_step("same", query="one"), search_step("same", query="two")])


def test_tool_registration_input_validation_and_unavailable():
    tools = registry()
    with pytest.raises(ToolInputError):
        tools.validate_input("document.search", {"wrong": "field"})
    with pytest.raises(ToolUnavailableError):
        tools.get_tool("browser.click")
    assert isinstance(tools.list_tools()[0], ToolDefinition)


def test_tool_registration_rejects_mismatching_input_schema():
    incorrect_schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    tools = ToolRegistry()
    definition = ToolDefinition(tool_name="document.search", description="Search", input_schema=incorrect_schema)
    with pytest.raises(ValueError, match="does not match"):
        tools.register(definition, SearchInput, successful_search)


def test_tool_registration_accepts_matching_model_schema():
    tools = registry(input_schema=SearchInput.model_json_schema())
    assert tools.get_tool("document.search").input_schema == SearchInput.model_json_schema()


def test_permission_policy_detects_sensitive_and_metadata_actions():
    assert requires_approval("Send an email to the vendor")
    assert requires_approval("read dashboard", {"irreversible": True})
    assert not requires_approval("Search public documents")


def test_structured_click_targets_are_classified_without_approving_all_clicks():
    assert action_requires_approval(
        tool_name="browser.click", description="Click target",
        arguments={"target": {"type": "role", "role": "button", "name": "Place order"}},
    )
    assert not action_requires_approval(
        tool_name="browser.click", description="Click target",
        arguments={"target": {"type": "role", "role": "button", "name": "Search"}},
    )
    assert action_requires_approval(
        tool_name="browser.click", description="Click target", arguments={"target": {"selector": "#x"}},
    )
    assert action_requires_approval(
        tool_name="browser.replay", description="Replay saved workflow", arguments={"workflow_id": str(uuid4())},
    )


def test_registry_enforces_approval_at_execution_boundary():
    calls = []
    tools = click_registry(lambda arguments, call: calls.append(call) or ToolResult(success=True, data={}))
    call = ToolCall(task_id=uuid4(), step_id="s1", tool_name="browser.click", arguments={"target": {"name": "Pay now"}})
    with pytest.raises(ApprovalRequiredError):
        tools.execute(call, description="Click target", before_execute=lambda: True)
    assert calls == []


def test_tool_call_and_result_use_shared_contracts():
    tools = registry()
    task_id = UUID("e8dfdc8c-2b4b-46e2-8f78-29651ad8d6c0")
    call = ToolCall(task_id=task_id, step_id="s1", tool_name="document.search", arguments={"query": "report"})
    result = tools.execute(call)
    assert isinstance(result, ToolResult)
    assert result.success
    assert result.evidence[0].task_id == task_id


def test_registry_normalizes_json_safe_values_and_rejects_unsupported_objects():
    call = ToolCall(task_id=uuid4(), step_id="s1", tool_name="document.search", arguments={"query": "report"})
    safe = registry(lambda arguments, context: ToolResult(success=True, data={"retrieved_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})).execute(call)
    assert safe.success
    assert safe.data["retrieved_at"] == "2026-01-01T00:00:00Z"
    unsafe = registry(lambda arguments, context: ToolResult(success=True, data={"handle": object()})).execute(call)
    assert not unsafe.success
    assert "JSON serializable" in unsafe.error


def test_task_response_remains_json_serializable_after_tool_execution():
    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])
    completed = TaskExecutor(manager, registry(), verifier).run(task.task_id)
    encoded = completed.model_dump_json()
    restored = TaskResponse.model_validate_json(encoded)
    assert restored.status == TaskStatus.COMPLETED
    assert str(restored.evidence[0].source_url) == "https://example.test/report"


def test_task_state_transitions_and_uuid4_cancellation_event():
    manager = TaskManager()
    task = create_task(manager, "Find report", [])
    assert isinstance(task, TaskResponse)
    assert task.task_id.version == 4
    assert task.status == TaskStatus.CREATED
    cancelled = manager.cancel(task.task_id)
    assert cancelled.status == TaskStatus.CANCELLED
    events = manager.events(task.task_id)
    assert events[-1].status == EventStatus.CANCELLED
    assert events[-1].task_id == task.task_id


def test_failed_tool_execution_marks_task_failed():
    def fail(arguments, call):
        raise RuntimeError("offline")

    tools = registry(fail)
    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])
    result = TaskExecutor(manager, tools, verifier).run(task.task_id)
    assert result.status == TaskStatus.FAILED
    assert result.plan[0].error == "offline"
    assert result.error == "offline"
    assert manager.events(task.task_id)[-1].status == EventStatus.FAILED


def test_verification_failure_does_not_mark_action_successful():
    tools = registry(successful_search)
    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])
    result = TaskExecutor(manager, tools, lambda tool, expected, output: False).run(task.task_id)
    assert result.status == TaskStatus.FAILED
    assert result.plan[0].status == StepStatus.FAILED
    assert result.plan[0].verification["verified"] is False


def test_verification_failure_retains_evidence_and_links_event():
    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])
    result = TaskExecutor(manager, registry(), lambda tool, expected, output: False).run(task.task_id)
    assert result.status == TaskStatus.FAILED
    assert result.plan[0].status == StepStatus.FAILED
    assert len(result.evidence) == 1
    assert result.evidence[0].task_id == result.task_id
    evidence_event = next(event for event in manager.events(task.task_id) if event.event_type == "tool.evidence_received")
    assert evidence_event.evidence_ids == [result.evidence[0].evidence_id]
    assert evidence_event.status != EventStatus.COMPLETED


def test_verifier_exception_fails_task_and_retains_tool_evidence():
    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])

    def broken_verifier(tool_name, expected, result):
        raise RuntimeError("verification backend unavailable")

    result = TaskExecutor(manager, registry(), broken_verifier).run(task.task_id)
    assert result.status == TaskStatus.FAILED
    assert result.plan[0].status == StepStatus.FAILED
    assert result.plan[0].verification["verified"] is False
    assert len(result.evidence) == 1
    assert result.evidence[0].task_id == result.task_id
    assert manager.events(task.task_id)[-1].event_type == "verification.failed"


def test_missing_verifier_leaves_task_needing_attention_without_calling_tool():
    calls = []
    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])
    result = TaskExecutor(manager, registry(lambda arguments, call: calls.append(call) or successful_search(arguments, call))).run(task.task_id)
    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.plan[0].status == StepStatus.PENDING
    assert calls == []
    resumed = TaskExecutor(manager, registry(lambda arguments, call: calls.append(call) or successful_search(arguments, call)), verifier).run(task.task_id)
    assert resumed.status == TaskStatus.COMPLETED
    assert len(calls) == 1


def test_approval_resolution_is_bound_to_approval_id_and_audited():
    manager = TaskManager()
    task = create_task(manager, "Send report", [search_step(description="Send an email with the report")], auto_execute=True)
    pending = TaskExecutor(manager, registry(), verifier).run(task.task_id)
    approval_id = pending.pending_approvals[0].approval_id
    resolved = manager.resolve_approval(task.task_id, approval_id, ApprovalStatus.APPROVED)
    assert resolved.pending_approvals == []
    assert manager.approval_history(task.task_id)[0].status == ApprovalStatus.APPROVED
    assert manager.events(task.task_id)[-1].event_type == "approval.approved"
    with pytest.raises(InvalidApprovalError, match="No pending approval"):
        manager.resolve_approval(task.task_id, approval_id, ApprovalStatus.APPROVED)


def test_rejected_approval_never_executes_and_is_audited():
    calls = []
    manager = TaskManager()
    task = create_task(manager, "Send report", [search_step(description="Send a message")])
    pending = TaskExecutor(manager, registry(lambda arguments, call: calls.append(call) or successful_search(arguments, call)), verifier).run(task.task_id)
    approval_id = pending.pending_approvals[0].approval_id
    rejected = manager.resolve_approval(task.task_id, approval_id, ApprovalStatus.REJECTED)
    assert rejected.status == TaskStatus.NEEDS_ATTENTION
    assert rejected.plan[0].status == StepStatus.SKIPPED
    assert manager.approval_history(task.task_id)[0].status == ApprovalStatus.REJECTED
    assert manager.events(task.task_id)[-1].event_type == "approval.rejected"
    assert TaskExecutor(manager, registry(lambda arguments, call: calls.append(call) or successful_search(arguments, call)), verifier).run(task.task_id).status == TaskStatus.NEEDS_ATTENTION
    assert calls == []


def test_cancellation_closes_pending_approval_requests():
    manager = TaskManager()
    task = create_task(manager, "Send report", [search_step(description="Send an email")])
    pending = TaskExecutor(manager, registry(), verifier).run(task.task_id)
    assert pending.pending_approvals
    cancelled = manager.cancel(task.task_id)
    assert cancelled.status == TaskStatus.CANCELLED
    assert cancelled.pending_approvals == []
    assert manager.approval_history(task.task_id)[0].status == ApprovalStatus.CANCELLED


def test_approval_id_must_belong_to_task_and_be_a_valid_uuid():
    manager = TaskManager()
    task = create_task(manager, "Send report", [search_step(description="Send email")])
    pending = TaskExecutor(manager, registry(), verifier).run(task.task_id)
    other = create_task(manager, "Other task", [])
    with pytest.raises(InvalidApprovalError, match="No pending approval"):
        manager.resolve_approval(other.task_id, pending.pending_approvals[0].approval_id, ApprovalStatus.APPROVED)
    with pytest.raises(InvalidApprovalError, match="Invalid approval UUID"):
        manager.resolve_approval(task.task_id, "not-a-uuid", ApprovalStatus.APPROVED)


def test_one_approval_does_not_authorize_another_step():
    calls = []

    def execute(arguments, call):
        calls.append(call.step_id)
        return successful_search(arguments, call)

    manager = TaskManager()
    task = create_task(manager, "Send two reports", [
        search_step("s1", description="Send email one", query="one"),
        search_step("s2", description="Send email two", query="two"),
    ])
    executor = TaskExecutor(manager, registry(execute), verifier)
    first_pending = executor.run(task.task_id)
    manager.resolve_approval(task.task_id, first_pending.pending_approvals[0].approval_id, ApprovalStatus.APPROVED)
    second_pending = executor.run(task.task_id)
    assert calls == ["s1"]
    assert second_pending.status == TaskStatus.AWAITING_APPROVAL
    assert second_pending.pending_approvals[0].step_id == "s2"
    assert second_pending.pending_approvals[0].approval_id != first_pending.pending_approvals[0].approval_id


def test_auto_execute_never_bypasses_tool_mandated_approval():
    manager = TaskManager()
    task = create_task(manager, "Search", [search_step()], auto_execute=True)
    pending = TaskExecutor(manager, registry(requires_approval=True), verifier).run(task.task_id)
    assert manager.auto_execute(task.task_id) is True
    assert pending.status == TaskStatus.AWAITING_APPROVAL
    assert pending.plan[0].status == StepStatus.AWAITING_APPROVAL


def test_cancellation_during_a_tool_prevents_next_action():
    manager = TaskManager()

    def cancel_while_running(arguments, call):
        manager.cancel(call.task_id)
        return successful_search(arguments, call)

    task = create_task(manager, "Search twice", [search_step("s1", query="one"), search_step("s2", query="two")])
    result = TaskExecutor(manager, registry(cancel_while_running), verifier).run(task.task_id)
    assert result.status == TaskStatus.CANCELLED
    assert result.plan[0].status == StepStatus.COMPLETED
    assert result.plan[1].status == StepStatus.PENDING


def test_cancellation_during_argument_validation_prevents_callable():
    entered_validation, release_validation = Event(), Event()
    calls = []
    tools = registry(lambda arguments, call: calls.append(call) or successful_search(arguments, call))
    original_validate = tools.validate_input

    def blocking_validate(name, value):
        entered_validation.set()
        assert release_validation.wait(3)
        return original_validate(name, value)

    tools.validate_input = blocking_validate
    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(TaskExecutor(manager, tools, verifier).run, task.task_id)
        assert entered_validation.wait(3)
        manager.cancel(task.task_id)
        release_validation.set()
        result = future.result(timeout=3)
    assert result.status == TaskStatus.CANCELLED
    assert calls == []


def test_concurrent_task_runs_invoke_pending_step_once():
    entered_tool, release_tool, second_entered = Event(), Event(), Event()
    calls = []
    calls_lock = Lock()
    manager = TaskManager()

    def blocking_tool(arguments, call):
        with calls_lock:
            calls.append(call.tool_call_id)
        persisted = manager.get(call.task_id)
        assert persisted.status == TaskStatus.EXECUTING
        assert persisted.current_step == call.step_id
        assert persisted.plan[0].status == StepStatus.RUNNING
        entered_tool.set()
        assert release_tool.wait(3)
        return successful_search(arguments, call)

    task = create_task(manager, "Find report", [search_step()])
    executor = TaskExecutor(manager, registry(blocking_tool), verifier)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(executor.run, task.task_id)
        assert entered_tool.wait(3)
        def second_run():
            second_entered.set()
            return executor.run(task.task_id)
        second = pool.submit(second_run)
        assert second_entered.wait(3)
        release_tool.set()
        first_result = first.result(timeout=3)
        second_result = second.result(timeout=3)
    assert first_result.status == TaskStatus.COMPLETED
    assert second_result.status == TaskStatus.COMPLETED
    assert len(calls) == 1


def test_recovery_resets_failed_step_and_preserves_completed_work():
    task = TaskResponse(
        user_request="Find two reports",
        status=TaskStatus.FAILED,
        plan=[
            search_step("s1"),
            TaskStep(step_id="s2", tool_name="document.search", description="Search next", input={"query": "next"}, expected_outcome="Found", status=StepStatus.FAILED, error="temporary failure"),
        ],
        completed_steps=["s1"],
        current_step="s2",
        error="temporary failure",
    )
    resumed = prepare_resume(task)
    assert resumed.status == TaskStatus.EXECUTING
    assert resumed.completed_steps == ["s1"]
    assert resumed.plan[1].status == StepStatus.PENDING
    assert resumed.plan[1].error is None
    assert resumed.current_step is None


def test_executor_resumes_failed_task_without_repeating_completed_steps():
    attempts = {"count": 0}

    def fail_once(arguments, call):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return ToolResult(success=False, error="temporary outage")
        return successful_search(arguments, call)

    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])
    executor = TaskExecutor(manager, registry(fail_once), verifier)
    failed = executor.run(task.task_id)
    assert failed.status == TaskStatus.FAILED
    assert executor.run(task.task_id).status == TaskStatus.FAILED
    recovered = executor.run(task.task_id, resume=True)
    assert recovered.status == TaskStatus.COMPLETED
    assert attempts["count"] == 2


def test_needs_attention_task_retains_missing_information():
    manager = TaskManager()
    task = create_task(manager, "Search", [], missing_information=["Which year?"])
    assert task.status == TaskStatus.NEEDS_ATTENTION
    assert manager.missing_information(task.task_id) == ["Which year?"]
    assert TaskExecutor(manager, registry(), verifier).run(task.task_id).status == TaskStatus.NEEDS_ATTENTION


def test_no_provider_is_a_configuration_error(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    with pytest.raises(LLMConfigurationError, match="No LLM provider configured"):
        get_provider_from_env()


@pytest.mark.parametrize("invalid_timeout", ["abc", "0", "-1", "nan", "inf"])
def test_invalid_llm_timeout_is_a_configuration_error(monkeypatch, invalid_timeout):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", invalid_timeout)
    with pytest.raises(LLMConfigurationError, match="LLM_TIMEOUT_SECONDS"):
        get_provider_from_env()
