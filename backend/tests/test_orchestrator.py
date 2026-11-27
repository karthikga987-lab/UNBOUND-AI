from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Lock
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from backend.app.browser.tools import BrowserClickInput, BrowserNavigateInput
from backend.app.documents.tools import CONTEXT_RETRIEVE_DEFINITION, ContextRetrieveInput
from backend.app.llm.provider import LLMConfigurationError, get_provider_from_env
from backend.app.orchestrator.executor import TaskExecutor, synthesize_result
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

    def generate_json(self, *, system: str, prompt: str, response_model: type[BaseModel], **kwargs) -> BaseModel:
        result = next(self.results)
        return response_model.model_validate(result.model_dump())


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str


class ClickInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: dict[str, object]


def test_synthesized_result_reports_verified_outcome_not_stale_approval_description():
    task = TaskResponse(
        user_request="Read and analyse a selected local file",
        status=TaskStatus.COMPLETED,
        plan=[TaskStep(
            step_id="read_file",
            tool_name="document.read_local_file",
            description="Request permission before opening the selected file",
            expected_outcome="The selected file was extracted and analysed with verified source quotations",
            status=StepStatus.COMPLETED,
            verification={"verified": True, "method": "local_file_operation"},
        )],
    )

    result = synthesize_result(task)

    assert result["actions_completed"] == [
        "The selected file was extracted and analysed with verified source quotations"
    ]
    assert "Request permission" not in result["actions_completed"][0]


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


def browser_retrieval_registry() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            tool_name="browser.navigate",
            description="Navigate to a page and return its bounded visible-content observation.",
            input_schema=BrowserNavigateInput.model_json_schema(),
            requires_approval=False,
        ),
        BrowserNavigateInput,
        lambda _arguments, _call: ToolResult(success=True),
    )
    tools.register(
        CONTEXT_RETRIEVE_DEFINITION,
        ContextRetrieveInput,
        lambda _arguments, _call: ToolResult(success=True),
    )
    return tools


def browser_action_registry(click_execute, navigation_execute=None) -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        ToolDefinition(
            tool_name="browser.navigate",
            description="Navigate to a page and return its bounded visible-content observation.",
            input_schema=BrowserNavigateInput.model_json_schema(),
        ),
        BrowserNavigateInput,
        navigation_execute or (lambda _arguments, _call: ToolResult(success=True, data={"verified": True})),
    )
    tools.register(
        ToolDefinition(
            tool_name="browser.click",
            description="Click one element identified by a structured browser locator.",
            input_schema=BrowserClickInput.model_json_schema(),
        ),
        BrowserClickInput,
        click_execute,
    )
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


def test_simple_readonly_page_analysis_with_classified_intent_uses_navigation_without_click():
    tools = browser_action_registry(lambda _arguments, _call: ToolResult(success=True, data={"verified": True}))
    plan = generate_plan(
        "Open https://github.com/features, analyze the page, and extract the title and main text.",
        tools,
        FakeProvider([]),
        intent=TaskIntent(goal="Analyze the supplied webpage", required_capabilities=["browser.navigate"]),
    )

    assert [step.tool_name for step in plan.steps] == ["browser.navigate"]
    assert plan.steps[0].input == {"url": "https://github.com/features"}


def test_planner_moves_nested_step_metadata_out_of_browser_click_arguments():
    tools = browser_action_registry(lambda _arguments, _call: ToolResult(success=True, data={"verified": True}))
    draft = PlanDraft(steps=[
        TaskStep(
            step_id="navigate",
            tool_name="browser.navigate",
            description="Open the page",
            input={"url": "https://example.com"},
            expected_outcome="Page is loaded",
        ),
        TaskStep(
            step_id="click_details",
            tool_name="browser.click",
            description="Click the requested details link",
            input={
                "target": {"type": "text", "value": "View details"},
                "depends_on": ["navigate"],
                "requires_approval": True,
                "error": "untrusted planner field",
                "verification": {"verified": True},
            },
            expected_outcome="Details are displayed",
        ),
    ])
    plan = generate_plan(
        "Open https://example.com and click View details.",
        tools,
        FakeProvider([draft]),
        intent=TaskIntent(goal="Open the page and click details", required_capabilities=["browser.navigate", "browser.click"]),
    )

    click = plan.steps[1]
    assert click.input == {"target": {"type": "text", "value": "View details"}}
    assert click.depends_on == ["navigate"]
    assert click.requires_approval is True
    assert click.error is None
    assert click.verification is None


def test_executor_keeps_step_metadata_and_dispatches_only_registered_click_arguments():
    dispatched = []
    tools = browser_action_registry(
        lambda arguments, _call: dispatched.append(arguments.model_dump(mode="json"))
        or ToolResult(success=True, data={"verified": True}),
    )
    draft = PlanDraft(steps=[
        TaskStep(
            step_id="navigate",
            tool_name="browser.navigate",
            description="Open the page",
            input={"url": "https://example.com"},
            expected_outcome="Page is loaded",
        ),
        TaskStep(
            step_id="click_details",
            tool_name="browser.click",
            description="Click the requested details link",
            input={
                "target": {"type": "text", "value": "View details"},
                "depends_on": ["navigate"],
                "requires_approval": True,
                "error": "untrusted planner field",
                "verification": {"verified": True},
            },
            expected_outcome="Details are displayed",
        ),
    ])
    manager = TaskManager()
    # Exercise the executor-side compatibility boundary directly, as it must
    # also protect persisted plans created before planner normalization.
    task = manager.create(TaskCreateRequest(user_request="Open the page and click details"), draft.steps)
    executor = TaskExecutor(manager, tools, verifier)

    waiting = executor.run(task.task_id)
    assert waiting.status == TaskStatus.AWAITING_APPROVAL
    assert waiting.plan[1].depends_on == ["navigate"]
    assert waiting.plan[1].requires_approval is True
    assert waiting.plan[1].error is None
    approval = waiting.pending_approvals[0]
    manager.resolve_approval(task.task_id, approval.approval_id, ApprovalStatus.APPROVED)
    completed = executor.run(task.task_id)

    assert completed.status == TaskStatus.COMPLETED
    assert dispatched == [{"target": {"type": "text", "value": "View details"}}]
    assert completed.plan[1].depends_on == ["navigate"]
    assert completed.plan[1].requires_approval is True
    assert completed.plan[1].verification["verified"] is True


def test_invalid_browser_click_argument_fails_before_tool_dispatch():
    dispatched = []
    tools = browser_action_registry(
        lambda arguments, _call: dispatched.append(arguments.model_dump(mode="json"))
        or ToolResult(success=True, data={"verified": True}),
    )
    manager = TaskManager()
    task = manager.create(TaskCreateRequest(user_request="Click the link"), [TaskStep(
        step_id="click",
        tool_name="browser.click",
        description="Click the link",
        input={"target": {"type": "text", "value": "View details"}, "unsupported": "value"},
        expected_outcome="The link is opened",
    )])

    result = TaskExecutor(manager, tools, verifier).run(task.task_id)

    assert result.status == TaskStatus.FAILED
    assert result.plan[0].error == "Invalid input for 'browser.click' (fields: unsupported)"
    assert dispatched == []


def test_empty_local_search_is_reported_as_no_results_and_blocks_dependents():
    invoked = []
    tools = registry(
        lambda _arguments, _call: invoked.append("document.search")
        or ToolResult(success=True, data={"query": "AI agents", "results": [], "total": 0})
    )
    tools.register(
        CONTEXT_RETRIEVE_DEFINITION,
        ContextRetrieveInput,
        lambda _arguments, _call: invoked.append("context.retrieve") or ToolResult(success=True, data={"verified": True}),
    )
    manager = TaskManager()
    task = create_task(manager, "Search public AI research", [
        TaskStep(
            step_id="s1", tool_name="document.search", description="Search indexed documents",
            input={"query": "AI agents"}, expected_outcome="Search results are returned",
        ),
        TaskStep(
            step_id="s2", tool_name="context.retrieve", description="Retrieve from search results",
            input={"query": "AI agents", "top_k": 5}, expected_outcome="Matching excerpts are returned",
            depends_on=["s1"],
        ),
    ])

    result = TaskExecutor(manager, tools, verifier).run(task.task_id)

    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.error == "Search completed but found no matching indexed documents; dependent steps were not run."
    assert result.plan[0].status == StepStatus.FAILED
    assert result.plan[1].status == StepStatus.PENDING
    assert invoked == ["document.search"]
    assert "external outcome is unknown" not in result.error


def test_verified_search_result_document_ids_are_bound_to_dependent_retrieval():
    document_id = "d775f4a3-cc76-4a9a-adf9-2b0bd3e30e33"
    observed_inputs = []
    tools = registry(lambda _arguments, _call: ToolResult(success=True, data={
        "verified": True,
        "query": "AI agents",
        "results": [{"document_id": document_id}],
        "total": 1,
    }))
    tools.register(
        CONTEXT_RETRIEVE_DEFINITION,
        ContextRetrieveInput,
        lambda arguments, _call: observed_inputs.append(arguments.document_ids)
        or ToolResult(success=True, data={"verified": True}),
    )
    manager = TaskManager()
    task = create_task(manager, "Search indexed AI research", [
        TaskStep(
            step_id="s1", tool_name="document.search", description="Search indexed documents",
            input={"query": "AI agents"}, expected_outcome="Search results are returned",
        ),
        TaskStep(
            step_id="s2", tool_name="context.retrieve", description="Retrieve matching search results",
            input={"query": "AI agents", "top_k": 5}, expected_outcome="Matching excerpts are returned",
            depends_on=["s1"],
        ),
    ])

    result = TaskExecutor(
        manager, tools, verifier, document_id_validator=lambda value: value == document_id
    ).run(task.task_id)

    assert result.status == TaskStatus.COMPLETED
    assert observed_inputs == [[document_id]]
    assert result.plan[1].input["document_ids"] == [document_id]


@pytest.mark.parametrize("user_request", [
    "Open https://example.test/product and tell me its version and features.",
    "Navigate to https://example.test/product and summarize the page.",
])
def test_page_observation_requests_drop_redundant_context_retrieval(user_request):
    navigation = TaskStep(
        step_id="navigate", tool_name="browser.navigate", description="Open the requested page",
        input={"url": "https://example.test/product"}, expected_outcome="The page's visible content is observed",
    )
    retrieval = TaskStep(
        step_id="retrieve", tool_name="context.retrieve", description="Retrieve matching context",
        input={"query": "page version and features", "top_k": 5}, expected_outcome="Relevant context is retrieved",
    )
    plan = generate_plan(
        user_request,
        browser_retrieval_registry(),
        FakeProvider([TaskIntent(goal="Answer from the page"), PlanDraft(steps=[navigation, retrieval])]),
    )

    assert [(step.tool_name, step.input) for step in plan.steps] == [
        ("browser.navigate", {"url": "https://example.test/product"}),
    ]
    assert plan.missing_information == []


def test_explicit_browser_url_repairs_qwen_output_hints_without_changing_scope():
    navigation = TaskStep(
        step_id="navigate", tool_name="browser.navigate", description="Extract the page title and text",
        input={"extract_main_text": True}, expected_outcome="The requested page title and visible text are observed",
    )
    plan = generate_plan(
        "Open https://example.test/product, extract the page title and main text.",
        browser_retrieval_registry(),
        FakeProvider([TaskIntent(goal="Read the page"), PlanDraft(steps=[navigation])]),
    )

    assert len(plan.steps) == 1
    assert plan.steps[0].tool_name == "browser.navigate"
    assert plan.steps[0].input == {"url": "https://example.test/product"}
    assert plan.missing_information == []


def test_page_only_plan_drops_redundant_document_search_after_navigation():
    tools = browser_retrieval_registry()
    tools.register(
        ToolDefinition(
            tool_name="document.search",
            description="Search indexed documents",
            input_schema=SearchInput.model_json_schema(),
        ),
        SearchInput,
        lambda _arguments, _call: ToolResult(success=True),
    )
    navigation = TaskStep(
        step_id="navigate", tool_name="browser.navigate", description="Read page",
        input={"url": "https://example.test/product"}, expected_outcome="Page content observed",
    )
    unrelated_search = TaskStep(
        step_id="search_page_text", tool_name="document.search", description="Search page text in documents",
        input={"query": "page title and main text"}, expected_outcome="Document matches returned",
    )

    plan = generate_plan(
        "Open https://example.test/product and extract the page title and main text.",
        tools,
        FakeProvider([TaskIntent(goal="Read the webpage"), PlanDraft(steps=[navigation, unrelated_search])]),
    )

    assert [step.tool_name for step in plan.steps] == ["browser.navigate"]


def test_explicit_document_search_keeps_context_retrieval_after_navigation():
    navigation = TaskStep(
        step_id="navigate", tool_name="browser.navigate", description="Open the requested page",
        input={"url": "https://example.test/product"}, expected_outcome="The page's visible content is observed",
    )
    retrieval = TaskStep(
        step_id="retrieve", tool_name="context.retrieve", description="Search indexed documents",
        input={"query": "related reports", "top_k": 5}, expected_outcome="Matching document evidence is retrieved",
    )
    plan = generate_plan(
        "Open https://example.test/product and search my indexed documents for related reports.",
        browser_retrieval_registry(),
        FakeProvider([TaskIntent(goal="Compare the page with indexed documents"), PlanDraft(steps=[navigation, retrieval])]),
    )

    assert [step.tool_name for step in plan.steps] == ["browser.navigate", "context.retrieve"]


def test_planner_prompt_includes_exact_plan_schema_and_string_clarifications():
    class PromptProvider(FakeProvider):
        system = ""

        def generate_json(self, *, system, prompt, response_model, **kwargs):
            self.system = system
            return super().generate_json(
                system=system, prompt=prompt, response_model=response_model, **kwargs
            )

    provider = PromptProvider([
        TaskIntent(goal="Find report"),
        PlanDraft(steps=[], missing_information=["Which report period should I search?"]),
    ])
    plan = generate_plan("Find a report", registry(), provider)

    assert plan.steps == []
    assert plan.missing_information == ["Which report period should I search?"]
    assert '"steps"' in provider.system
    assert '"missing_information"' in provider.system
    assert "array of plain strings" in provider.system
    assert "no markdown" in provider.system


def test_planner_exposes_sequential_step_dependencies():
    intent = TaskIntent(goal="Search two related queries")
    draft = PlanDraft(steps=[search_step("s1", query="one"), search_step("s2", query="two")])
    plan = generate_plan("Search two related queries", registry(), FakeProvider([intent, draft]))
    assert plan.steps[0].depends_on == []
    assert plan.steps[1].depends_on == ["s1"]


def test_planner_cannot_supply_server_execution_or_verification_state():
    intent = TaskIntent(goal="Find a report")
    claimed_completed = search_step()
    claimed_completed.status = StepStatus.COMPLETED
    claimed_completed.verification = {"verified": True}
    claimed_completed.error = "stale model text"
    plan = generate_plan("Find a report", registry(), FakeProvider([
        intent, PlanDraft(steps=[claimed_completed]),
    ]))

    assert plan.steps[0].status == StepStatus.PENDING
    assert plan.steps[0].verification is None
    assert plan.steps[0].error is None


@pytest.mark.parametrize(("dependency", "error"), [("missing", "unknown step"), ("s2", "earlier step")])
def test_planner_rejects_unknown_or_forward_step_dependencies(dependency, error):
    intent = TaskIntent(goal="Search")
    draft = PlanDraft(steps=[
        search_step("s1", query="one"),
        search_step("s2", query="two"),
    ])
    draft.steps[0].depends_on = [dependency]
    with pytest.raises(PlanError, match=error):
        generate_plan("Search", registry(), FakeProvider([intent, draft]))


def test_planner_carries_registered_approval_requirement_into_step():
    intent = TaskIntent(goal="Search")
    draft = PlanDraft(steps=[search_step()])
    plan = generate_plan("Search", registry(requires_approval=True), FakeProvider([intent, draft]))
    assert plan.steps[0].requires_approval


def test_planner_marks_sensitive_actions_for_approval_from_structured_plan_context():
    intent = TaskIntent(goal="Send the requested message", sensitive_actions=["send a message"])
    draft = PlanDraft(steps=[search_step(description="Prepare the recipient message")])
    plan = generate_plan("Send the requested message", registry(), FakeProvider([intent, draft]))
    assert plan.steps[0].requires_approval is True


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


def test_task_creation_rejects_unknown_step_dependencies():
    manager = TaskManager()
    dependent = TaskStep(step_id="s2", tool_name="document.search", description="Search", input={"query": "two"}, expected_outcome="Found", depends_on=["missing"])
    with pytest.raises(ValueError, match="unknown step"):
        create_task(manager, "Bad dependency", [search_step("s1"), dependent])


def test_task_manager_persists_explicit_sequential_dependencies():
    manager = TaskManager()
    task = create_task(manager, "Search twice", [search_step("s1", query="one"), search_step("s2", query="two")])
    assert task.plan[0].depends_on == []
    assert task.plan[1].depends_on == ["s1"]


def test_tool_registration_input_validation_and_unavailable():
    tools = registry()
    with pytest.raises(ToolInputError):
        tools.validate_input("document.search", {"wrong": "field"})
    with pytest.raises(ToolUnavailableError):
        tools.get_tool("browser.click")
    assert isinstance(tools.list_tools()[0], ToolDefinition)


def test_tool_input_validation_diagnostic_does_not_echo_submitted_value():
    with pytest.raises(ToolInputError) as error:
        registry().validate_input("document.search", {"query": "private-token-value", "extra": "secret-value"})
    assert "private-token-value" not in str(error.value)
    assert "secret-value" not in str(error.value)


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


def test_executor_blocks_step_with_unverified_dependency():
    calls = []
    manager = TaskManager()
    prerequisite = search_step("s1", query="one")
    prerequisite.status = StepStatus.SKIPPED
    dependent = search_step("s2", query="two")
    dependent.depends_on = ["s1"]
    task = create_task(manager, "Run dependent task", [prerequisite, dependent])
    executor = TaskExecutor(manager, registry(lambda arguments, call: calls.append(call) or successful_search(arguments, call)), verifier)

    result = executor.run(task.task_id)

    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.plan[1].status == StepStatus.SKIPPED
    assert "s1" in result.plan[1].error
    assert calls == []


def test_executor_verifies_evidence_step_before_running_dependent_step():
    calls = []
    manager = TaskManager()
    first = search_step("s1", query="one")
    second = search_step("s2", query="two")
    second.depends_on = ["s1"]
    task = create_task(manager, "Run sequential evidence search", [first, second])
    executor = TaskExecutor(
        manager,
        registry(lambda arguments, call: calls.append(call.step_id) or successful_search(arguments, call)),
        verifier,
    )

    result = executor.run(task.task_id)

    assert result.status == TaskStatus.COMPLETED
    assert [step.status for step in result.plan] == [StepStatus.COMPLETED, StepStatus.COMPLETED]
    assert result.completed_steps == ["s1", "s2"]
    assert calls == ["s1", "s2"]
    assert len(result.evidence) == 2


def test_task_state_transitions_and_uuid4_cancellation_event():
    manager = TaskManager()
    task = create_task(manager, "Find report", [])
    assert isinstance(task, TaskResponse)
    assert task.task_id.version == 4
    assert task.status == TaskStatus.NEEDS_ATTENTION
    assert manager.missing_information(task.task_id)
    cancelled = manager.cancel(task.task_id)
    assert cancelled.status == TaskStatus.CANCELLED
    events = manager.events(task.task_id)
    assert events[-1].status == EventStatus.CANCELLED
    assert events[-1].task_id == task.task_id


def test_empty_plan_never_completes_or_invokes_a_tool():
    calls = []
    manager = TaskManager()
    task = create_task(manager, "Do the requested action", [])
    result = TaskExecutor(
        manager,
        registry(lambda arguments, call: calls.append(call) or successful_search(arguments, call)),
        verifier,
    ).run(task.task_id)

    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.plan == []
    assert result.result["missing_information"]
    assert manager.events(task.task_id)[0].status == EventStatus.NEEDS_ATTENTION
    assert manager.events(task.task_id)[0].event_type == "task.needs_attention"
    assert calls == []


def test_failed_tool_execution_marks_unknown_outcome_needing_attention_without_exposing_detail():
    def fail(arguments, call):
        raise RuntimeError("offline")

    tools = registry(fail)
    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])
    result = TaskExecutor(manager, tools, verifier).run(task.task_id)
    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.plan[0].error == "Search submission failed before a result was confirmed"
    assert result.error == "Search submission failed before a result was confirmed"
    assert "offline" not in result.error
    assert manager.events(task.task_id)[-1].event_type == "tool.outcome_unknown"
    assert manager.events(task.task_id)[-1].status == EventStatus.NEEDS_ATTENTION
    assert TaskExecutor(manager, tools, verifier).run(task.task_id).status == TaskStatus.NEEDS_ATTENTION


def test_verification_failure_does_not_mark_action_successful():
    tools = registry(successful_search)
    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])
    result = TaskExecutor(manager, tools, lambda tool, expected, output: False).run(task.task_id)
    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.plan[0].status == StepStatus.FAILED
    assert result.plan[0].verification["verified"] is False


def test_verification_failure_retains_evidence_and_links_event():
    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])
    result = TaskExecutor(manager, registry(), lambda tool, expected, output: False).run(task.task_id)
    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.plan[0].status == StepStatus.FAILED
    assert len(result.evidence) == 1
    assert result.evidence[0].task_id == result.task_id
    evidence_event = next(event for event in manager.events(task.task_id) if event.event_type == "tool.evidence_received")
    assert evidence_event.evidence_ids == [result.evidence[0].evidence_id]
    assert evidence_event.status != EventStatus.COMPLETED


def test_verifier_exception_needs_attention_and_retains_tool_evidence():
    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])

    def broken_verifier(tool_name, expected, result):
        raise RuntimeError("verification backend unavailable")

    result = TaskExecutor(manager, registry(), broken_verifier).run(task.task_id)
    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.plan[0].status == StepStatus.FAILED
    assert result.plan[0].verification["verified"] is False
    assert len(result.evidence) == 1
    assert result.evidence[0].task_id == result.task_id
    assert manager.events(task.task_id)[-1].event_type == "verification.failed"
    assert manager.events(task.task_id)[-1].status == EventStatus.NEEDS_ATTENTION
    assert "verification backend unavailable" not in str(result.model_dump())


def test_changed_action_parameters_invalidate_pending_approval():
    manager = TaskManager()
    step = search_step(query="original")
    step.requires_approval = True
    task = create_task(manager, "Search approved report", [step])
    pending = TaskExecutor(manager, registry(), verifier).run(task.task_id)
    approval = pending.pending_approvals[0]

    changed = manager.get(task.task_id)
    changed.plan[0].input["query"] = "changed after approval"
    manager.save(changed)

    with pytest.raises(InvalidApprovalError):
        manager.resolve_approval(task.task_id, approval.approval_id, ApprovalStatus.APPROVED)


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
    calls = []
    tools = registry(lambda arguments, call: calls.append(call) or successful_search(arguments, call))
    manager = TaskManager()
    task = create_task(manager, "Send report", [search_step(description="Send an email with the report")], auto_execute=True)
    pending = TaskExecutor(manager, tools, verifier).run(task.task_id)
    approval_id = pending.pending_approvals[0].approval_id
    resolved = manager.resolve_approval(task.task_id, approval_id, ApprovalStatus.APPROVED)
    assert resolved.pending_approvals == []
    assert resolved.status == TaskStatus.CREATED
    assert manager.approval_history(task.task_id)[0].status == ApprovalStatus.APPROVED
    assert manager.events(task.task_id)[-1].event_type == "approval.approved"
    assert calls == []
    with pytest.raises(InvalidApprovalError, match="No pending approval"):
        manager.resolve_approval(task.task_id, approval_id, ApprovalStatus.APPROVED)
    assert TaskExecutor(manager, tools, verifier).run(task.task_id).status == TaskStatus.COMPLETED
    assert len(calls) == 1


def test_expired_approval_is_rejected_and_requires_a_fresh_approval():
    calls = []
    tools = registry(lambda arguments, call: calls.append(call) or successful_search(arguments, call))
    manager = TaskManager(approval_ttl_seconds=1)
    task = create_task(manager, "Send report", [search_step(description="Send an email")])
    pending = TaskExecutor(manager, tools, verifier).run(task.task_id)
    stale = manager.get(task.task_id)
    stale.pending_approvals[0].created_at -= timedelta(seconds=2)
    manager.save(stale)

    with pytest.raises(InvalidApprovalError, match="expired"):
        manager.resolve_approval(task.task_id, pending.pending_approvals[0].approval_id, ApprovalStatus.APPROVED)

    refreshed = manager.get(task.task_id)
    assert refreshed.status == TaskStatus.CREATED
    assert refreshed.pending_approvals == []
    assert manager.approval_history(task.task_id)[-1].status == ApprovalStatus.CANCELLED
    assert TaskExecutor(manager, tools, verifier).run(task.task_id).status == TaskStatus.AWAITING_APPROVAL
    assert calls == []


def test_cancellation_racing_with_approval_never_leaves_executable_approval():
    manager = TaskManager()
    task = create_task(manager, "Send report", [search_step(description="Send an email")])
    pending = TaskExecutor(manager, registry(), verifier).run(task.task_id)
    approval_id = pending.pending_approvals[0].approval_id
    start = Barrier(3)

    def approve():
        start.wait()
        try:
            return manager.resolve_approval(task.task_id, approval_id, ApprovalStatus.APPROVED)
        except InvalidApprovalError:
            return None

    def cancel():
        start.wait()
        return manager.cancel(task.task_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        approval_future = pool.submit(approve)
        cancel_future = pool.submit(cancel)
        start.wait()
        approval_future.result(timeout=3)
        cancel_future.result(timeout=3)

    final = manager.get(task.task_id)
    assert final.status == TaskStatus.CANCELLED
    assert final.pending_approvals == []
    assert manager.approval_history(task.task_id)[-1].status in {
        ApprovalStatus.APPROVED,
        ApprovalStatus.CANCELLED,
    }


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


def test_cancelled_before_execution_never_invokes_the_tool():
    calls = []
    manager = TaskManager()
    task = create_task(manager, "Find report", [search_step()])
    manager.cancel(task.task_id)

    result = TaskExecutor(
        manager,
        registry(lambda arguments, call: calls.append(call) or successful_search(arguments, call)),
        verifier,
    ).run(task.task_id)

    assert result.status == TaskStatus.CANCELLED
    assert calls == []


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


def test_changed_arguments_invalidate_prior_approval():
    calls = []
    manager = TaskManager()
    task = create_task(manager, "Send report", [search_step("s1", description="Send report by email", query="original")])
    executor = TaskExecutor(manager, registry(lambda arguments, call: calls.append(call) or successful_search(arguments, call)), verifier)
    awaiting = executor.run(task.task_id)
    approval_id = awaiting.pending_approvals[0].approval_id
    manager.resolve_approval(task.task_id, approval_id, ApprovalStatus.APPROVED)

    changed = manager.get(task.task_id)
    changed.plan[0].input = {"query": "changed after approval"}
    manager.save(changed)
    result = executor.run(task.task_id)

    assert result.status == TaskStatus.AWAITING_APPROVAL
    assert result.pending_approvals[0].arguments == {"query": "changed after approval"}
    assert result.pending_approvals[0].approval_id != approval_id
    assert calls == []


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


def test_cancellation_during_tool_exception_keeps_unknown_outcome_visible():
    manager = TaskManager()
    entered, release = Event(), Event()

    def cancel_then_raise(arguments, call):
        entered.set()
        assert release.wait(3)
        raise RuntimeError("connection lost after remote action may have started")

    task = create_task(manager, "Find a report", [search_step(description="Search a report")])
    executor = TaskExecutor(manager, registry(cancel_then_raise), verifier)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(executor.run, task.task_id)
        assert entered.wait(3)
        manager.cancel(task.task_id)
        release.set()
        result = future.result(timeout=3)

    assert result.status == TaskStatus.CANCELLED
    assert result.plan[0].verification["error_category"] == "search_submission_failure"
    assert result.plan[0].status == StepStatus.FAILED
    assert manager.events(task.task_id)[-1].event_type == "tool.outcome_unknown"
    assert manager.events(task.task_id)[-1].status == EventStatus.NEEDS_ATTENTION
    assert executor.run(task.task_id).status == TaskStatus.CANCELLED


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


def test_recovery_preserves_completed_work_and_blocks_ambiguous_failed_step_replay():
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
    assert resumed.status == TaskStatus.NEEDS_ATTENTION
    assert resumed.completed_steps == ["s1"]
    assert resumed.plan[1].status == StepStatus.FAILED
    assert resumed.plan[1].error == "temporary failure"
    assert resumed.current_step is None


def test_executor_does_not_replay_failed_external_tool_on_resume():
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
    assert failed.error == "Registered tool reported unsuccessful execution"
    assert executor.run(task.task_id).status == TaskStatus.FAILED
    recovered = executor.run(task.task_id, resume=True)
    assert recovered.status == TaskStatus.NEEDS_ATTENTION
    assert "external effect" in recovered.error
    assert attempts["count"] == 1


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
