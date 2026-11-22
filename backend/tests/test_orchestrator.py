from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from backend.app.llm.provider import LLMConfigurationError, get_provider_from_env
from backend.app.orchestrator.executor import TaskExecutor
from backend.app.orchestrator.intent import TaskIntent, understand_intent
from backend.app.orchestrator.models import StepStatus, TaskStatus, TaskStep
from backend.app.orchestrator.planner import PlanDraft, generate_plan
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.orchestrator.tool_registry import ToolDefinition, ToolInputError, ToolRegistry, ToolUnavailableError
from backend.app.safety.permissions import requires_approval


class FakeProvider:
    def __init__(self, results: list[BaseModel]):
        self.results = iter(results)

    def generate_json(self, *, system: str, prompt: str, response_model: type[BaseModel]) -> BaseModel:
        result = next(self.results)
        return response_model.model_validate(result.model_dump())


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str


def registry(execute=lambda value: {"success": True, "verified": True, "evidence": ["url:https://example.test"]}):
    tools = ToolRegistry()
    tools.register(ToolDefinition("document.search", "Search indexed documents", SearchInput, execute))
    return tools


def test_intent_parsing_returns_validated_structure():
    expected = TaskIntent(goal="Find the report", required_capabilities=["search"], parameters={"name": "report"})
    intent = understand_intent("Find the report", FakeProvider([expected]))
    assert intent.goal == "Find the report"
    assert intent.parameters["name"] == "report"


def test_plan_uses_registered_tool_and_validates_input():
    intent = TaskIntent(goal="Find a report")
    draft = PlanDraft(steps=[TaskStep(step_id="s1", tool_name="document.search", description="Search", input={"query": "report"}, expected_outcome="A matching report is found")])
    plan = generate_plan("Find a report", registry(), FakeProvider([intent, draft]))
    assert [step.tool_name for step in plan.steps] == ["document.search"]


def test_plan_rejects_unregistered_tool():
    intent = TaskIntent(goal="Do something")
    draft = PlanDraft(steps=[TaskStep(step_id="s1", tool_name="browser.navigate", description="Open", input={}, expected_outcome="Page loaded")])
    with pytest.raises(ValueError, match="unregistered"):
        generate_plan("Do something", registry(), FakeProvider([intent, draft]))


def test_plan_requests_clarification_for_missing_tool_input():
    intent = TaskIntent(goal="Search")
    draft = PlanDraft(steps=[TaskStep(step_id="s1", tool_name="document.search", description="Search", input={}, expected_outcome="Results found")])
    plan = generate_plan("Search", registry(), FakeProvider([intent, draft]))
    assert not plan.steps
    assert "clarify" in plan.missing_information[0]


def test_tool_registration_input_validation_and_unavailable():
    tools = registry()
    with pytest.raises(ToolInputError):
        tools.validate_input("document.search", {"wrong": "field"})
    with pytest.raises(ToolUnavailableError):
        tools.get_tool("browser.click")


def test_permission_checks_cover_sensitive_external_actions():
    assert requires_approval("Send an email to the vendor")
    assert requires_approval("read dashboard", {"irreversible": True})
    assert not requires_approval("Search the public web")


def test_task_state_transitions_and_cancellation():
    manager = TaskManager()
    task = manager.create("Find report", [])
    assert task.status == TaskStatus.PENDING
    cancelled = manager.cancel(task.task_id)
    assert cancelled.status == TaskStatus.CANCELLED


def test_failed_tool_execution_marks_task_failed():
    tools = registry(lambda value: (_ for _ in ()).throw(RuntimeError("offline")))
    manager = TaskManager()
    task = manager.create("Find report", [TaskStep(step_id="s1", tool_name="document.search", description="Search", input={"query": "report"}, expected_outcome="Report found")])
    result = TaskExecutor(manager, tools).run(task.task_id)
    assert result.status == TaskStatus.FAILED
    assert "offline" in result.steps[0].error


def test_verification_failure_does_not_mark_action_successful():
    tools = registry(lambda value: {"success": True, "verified": False})
    manager = TaskManager()
    task = manager.create("Find report", [TaskStep(step_id="s1", tool_name="document.search", description="Search", input={"query": "report"}, expected_outcome="Report found")])
    result = TaskExecutor(manager, tools).run(task.task_id)
    assert result.status == TaskStatus.FAILED
    assert result.steps[0].status == StepStatus.FAILED


def test_approval_pauses_and_success_requires_evidence():
    manager = TaskManager()
    task = manager.create("Send message", [TaskStep(step_id="s1", tool_name="document.search", description="Send a message", input={"query": "message"}, expected_outcome="Sent")])
    pending = TaskExecutor(manager, registry()).run(task.task_id)
    assert pending.status == TaskStatus.AWAITING_APPROVAL
    done = TaskExecutor(manager, registry()).run(task.task_id, approved_steps={"s1"})
    assert done.status == TaskStatus.COMPLETED
    assert done.evidence[0].verified


def test_cancellation_during_a_tool_prevents_next_action():
    manager = TaskManager()
    task = manager.create("Search twice", [
        TaskStep(step_id="s1", tool_name="document.search", description="First search", input={"query": "one"}, expected_outcome="First result"),
        TaskStep(step_id="s2", tool_name="document.search", description="Second search", input={"query": "two"}, expected_outcome="Second result"),
    ])
    tools = registry(lambda value: (manager.cancel(task.task_id), {"success": True, "verified": True})[1])
    result = TaskExecutor(manager, tools).run(task.task_id)
    assert result.status == TaskStatus.CANCELLED
    assert result.steps[1].status == StepStatus.PENDING


def test_no_provider_is_a_configuration_error(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    with pytest.raises(LLMConfigurationError, match="No LLM provider configured"):
        get_provider_from_env()
