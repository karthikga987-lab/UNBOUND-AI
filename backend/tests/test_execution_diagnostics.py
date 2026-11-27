from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from backend.app.browser.session_manager import BrowserSessionManager
from backend.app.browser.tools import BrowserNavigateInput, BrowserToolAdapter
from backend.app.browser.workflow_store import WorkflowStore
from backend.app.documents.tools import (
    CONTEXT_RETRIEVE_DEFINITION,
    DOCUMENT_SEARCH_DEFINITION,
    ContextRetrieveInput,
    DocumentSearchInput,
)
from backend.app.orchestrator.executor import TaskExecutor
from backend.app.orchestrator.task_manager import (
    InMemoryTaskRepository,
    SQLiteTaskRepository,
    TaskManager,
)
from backend.app.orchestrator.tool_registry import ToolRegistry
from backend.app.schemas.contracts import (
    StepStatus,
    TaskCreateRequest,
    TaskStatus,
    TaskStep,
    ToolDefinition,
    ToolCall,
    ToolResult,
)


def _search_plan(*, include_dependent: bool = False) -> list[TaskStep]:
    steps = [TaskStep(
        step_id="s1",
        tool_name="document.search",
        description="Search indexed material",
        input={"query": "AI evolution", "top_k": 5},
        expected_outcome="Matching results are extracted",
    )]
    if include_dependent:
        steps.append(TaskStep(
            step_id="s2",
            tool_name="context.retrieve",
            description="Retrieve context from the verified search sources",
            input={"query": "AI evolution", "top_k": 5},
            depends_on=["s1"],
            expected_outcome="Relevant context is retrieved",
        ))
    return steps


def _registry(search_callable, context_callable=None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(DOCUMENT_SEARCH_DEFINITION, DocumentSearchInput, search_callable)
    if context_callable is not None:
        registry.register(CONTEXT_RETRIEVE_DEFINITION, ContextRetrieveInput, context_callable)
    return registry


def _make_task(manager: TaskManager, *, include_dependent: bool = False):
    return manager.create(
        TaskCreateRequest(user_request="Search the uploaded material and summarize it"),
        _search_plan(include_dependent=include_dependent),
    )


def test_successful_search_checkpoints_real_results_and_gates_dependent_step(tmp_path):
    document_id = str(uuid4())
    calls: list[tuple[str, dict]] = []

    def search(arguments, call):
        calls.append((call.step_id, arguments.model_dump()))
        return ToolResult(success=True, data={
            "query": arguments.query,
            "total": 1,
            "results": [{
                "document_id": document_id,
                "filename": "research.pdf",
                "page": 3,
                "excerpt": "Relevant source material",
                "relevance_score": 0.9,
            }],
        })

    def retrieve(arguments, call):
        calls.append((call.step_id, arguments.model_dump()))
        return ToolResult(success=True, data={"results": [{"document_id": document_id}], "total": 1})

    database = tmp_path / "tasks.sqlite3"
    manager = TaskManager(repository=SQLiteTaskRepository(database))
    task = _make_task(manager, include_dependent=True)
    result = TaskExecutor(
        manager,
        _registry(search, retrieve),
        lambda _tool, _expected, output: output.success,
        document_id_validator=lambda value: value == document_id,
    ).run(task.task_id)

    assert result.status == TaskStatus.COMPLETED
    assert calls[0][0] == "s1" and calls[1][0] == "s2"
    assert calls[1][1]["document_ids"] == [document_id]
    diagnostic = manager.tool_diagnostic(task.task_id, "s1")
    assert diagnostic["document_ids"] == [document_id]
    assert [item["stage"] for item in diagnostic["checkpoints"]] == ["raw_response", "normalized_response"]
    events = manager.events(task.task_id)
    assert any(event.event_type == "search.submission_started" for event in events)
    assert any(event.event_type == "search.results_extracted" and event.result_count == 1 for event in events)
    assert all(event.timestamp.tzinfo is not None for event in events)

    restored = TaskManager(repository=SQLiteTaskRepository(database))
    assert restored.tool_diagnostic(task.task_id, "s1")["document_ids"] == [document_id]
    assert [event.event_type for event in restored.events(task.task_id)] == [event.event_type for event in events]


def test_search_data_is_preserved_when_verification_fails_and_dependent_is_not_run():
    document_id = str(uuid4())
    calls: list[str] = []

    def search(arguments, call):
        calls.append(call.step_id)
        return ToolResult(success=True, data={
            "query": arguments.query,
            "total": 1,
            "results": [{"document_id": document_id, "filename": "source.pdf", "excerpt": "Recovered result"}],
        })

    def retrieve(arguments, call):
        calls.append(call.step_id)
        return ToolResult(success=True, data={"results": []})

    manager = TaskManager()
    task = _make_task(manager, include_dependent=True)
    result = TaskExecutor(
        manager,
        _registry(search, retrieve),
        lambda _tool, _expected, _output: False,
        document_id_validator=lambda _value: True,
    ).run(task.task_id)

    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.plan[0].status == StepStatus.FAILED
    assert result.plan[0].verification["tool_result_persisted"] is True
    assert "tool_result" not in result.plan[0].verification
    assert result.plan[0].verification["diagnostic"]["document_ids"] == [document_id]
    assert manager.tool_diagnostic(task.task_id, "s1")["document_ids"] == [document_id]
    assert calls == ["s1"]
    failure = next(event for event in manager.events(task.task_id) if event.event_type == "verification.failed")
    assert failure.error_category == "verification_failure"


def test_empty_search_is_reported_as_known_no_results_and_s2_is_not_dispatched():
    calls: list[str] = []
    manager = TaskManager()
    task = _make_task(manager, include_dependent=True)

    def search(_arguments, call):
        calls.append(call.step_id)
        return ToolResult(success=True, data={"query": "AI evolution", "total": 0, "results": []})

    def retrieve(_arguments, call):
        calls.append(call.step_id)
        return ToolResult(success=True, data={"results": []})

    result = TaskExecutor(
        manager, _registry(search, retrieve), lambda _tool, _expected, output: bool(output.data.get("total")),
    ).run(task.task_id)

    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.plan[0].verification["diagnostic"]["result_count"] == 0
    assert result.plan[1].status == StepStatus.PENDING
    assert calls == ["s1"]
    event = next(event for event in manager.events(task.task_id) if event.event_type == "search.completed_empty")
    assert event.error_category == "search_no_results"


def test_malformed_search_results_are_classified_as_extraction_failure():
    manager = TaskManager()
    task = _make_task(manager)
    registry = _registry(lambda arguments, _call: ToolResult(
        success=True,
        data={"query": arguments.query, "total": 2, "results": [{"document_id": str(uuid4())}]},
    ))

    result = TaskExecutor(manager, registry, lambda *_args: False).run(task.task_id)

    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.plan[0].verification["diagnostic"]["result_count"] == 2
    event = next(event for event in manager.events(task.task_id) if event.event_type == "search.extraction_blocked")
    assert event.error_category == "search_extraction_failure"


def test_unpersisted_document_id_blocks_dependent_execution():
    document_id = str(uuid4())
    calls: list[str] = []

    def search(_arguments, call):
        calls.append(call.step_id)
        return ToolResult(success=True, data={
            "query": "AI evolution", "total": 1,
            "results": [{"document_id": document_id, "excerpt": "result"}],
        })

    def retrieve(_arguments, call):
        calls.append(call.step_id)
        return ToolResult(success=True, data={"results": [{"document_id": document_id}]})

    manager = TaskManager()
    task = _make_task(manager, include_dependent=True)
    result = TaskExecutor(
        manager, _registry(search, retrieve), lambda _tool, _expected, output: output.success,
        document_id_validator=lambda _value: False,
    ).run(task.task_id)

    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.plan[0].status == StepStatus.COMPLETED
    assert result.plan[1].status == StepStatus.FAILED
    assert calls == ["s1"]
    assert manager.events(task.task_id)[-1].error_category == "dependency_result_unavailable"


def test_tool_failure_records_submission_failure_without_inventing_results():
    manager = TaskManager()
    task = _make_task(manager)

    def fail(_arguments, _call):
        raise RuntimeError("provider failure detail must not be returned")

    result = TaskExecutor(manager, _registry(fail), lambda *_args: True).run(task.task_id)

    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert result.plan[0].verification["verified"] is None
    assert result.plan[0].verification["error_category"] == "search_submission_failure"
    assert manager.tool_diagnostic(task.task_id, "s1") is None
    event = manager.events(task.task_id)[-1]
    assert event.error_category == "search_submission_failure"
    assert "provider failure detail" not in event.message


def test_non_search_exception_is_classified_as_unknown_external_outcome():
    class ReportInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        report_name: str

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            tool_name="report.generate",
            description="Generate report",
            input_schema=ReportInput.model_json_schema(),
        ),
        ReportInput,
        lambda *_args: (_ for _ in ()).throw(TimeoutError("external timeout")),
    )
    manager = TaskManager()
    task = manager.create(
        TaskCreateRequest(user_request="Generate report"),
        [TaskStep(
            step_id="s1", tool_name="report.generate", description="Generate report",
            input={"report_name": "summary"}, expected_outcome="Report generated",
        )],
    )

    result = TaskExecutor(manager, registry, lambda *_args: True).run(task.task_id)

    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert manager.events(task.task_id)[-1].error_category == "unknown_external_outcome"


def test_raw_tool_diagnostics_remove_sensitive_content_and_credentials():
    secret = "do-not-log-this-token"
    manager = TaskManager()
    task = _make_task(manager)
    tool_registry = _registry(lambda _arguments, _call: ToolResult(
        success=True,
        data={
            "query": secret,
            "total": 1,
            "results": [{
                "document_id": str(uuid4()),
                "excerpt": f"private text {secret}",
                "url": f"https://example.test/path?token={secret}",
            }],
            "authorization": f"Bearer {secret}",
            "cookies": secret,
        },
    ))
    TaskExecutor(manager, tool_registry, lambda *_args: False).run(task.task_id)

    encoded = str(manager.tool_diagnostic(task.task_id, "s1")) + str(manager.events(task.task_id))
    assert secret not in encoded
    assert "private text" not in encoded
    assert "token=" not in encoded
    assert secret not in str(manager.get(task.task_id).plan[0].verification)


def test_diagnostic_checkpoint_failure_stops_dependent_step():
    class FailingDiagnosticRepository(InMemoryTaskRepository):
        def save_tool_diagnostic(self, task_id, step_id, tool_name, payload):
            raise OSError("diagnostic storage is unavailable")

    calls: list[str] = []
    manager = TaskManager(repository=FailingDiagnosticRepository())

    def search(arguments, call):
        calls.append(call.step_id)
        return ToolResult(success=True, data={"query": arguments.query, "total": 1, "results": [{"document_id": str(uuid4())}]})

    def retrieve(_arguments, call):
        calls.append(call.step_id)
        return ToolResult(success=True, data={"results": []})

    task = _make_task(manager, include_dependent=True)
    result = TaskExecutor(
        manager, _registry(search, retrieve), lambda _tool, _expected, output: output.success,
    ).run(task.task_id)

    assert result.status == TaskStatus.NEEDS_ATTENTION
    assert calls == ["s1"]
    assert result.plan[0].verification["error_category"] == "persistence_failure"
    assert any(event.error_category == "persistence_failure" for event in manager.events(task.task_id))


def test_browser_launch_failure_is_a_durable_categorized_event(tmp_path):
    events = []

    def fail_controller_start():
        raise RuntimeError("private provider detail")

    adapter = BrowserToolAdapter(
        workflow_store=WorkflowStore(tmp_path / "workflows"),
        session_manager=BrowserSessionManager(controller_factory=fail_controller_start),
        execution_event_publisher=lambda *args, **kwargs: events.append((args, kwargs)),
    )
    call = ToolCall(
        task_id=uuid4(), step_id="navigate", tool_name="browser.navigate",
        arguments={"url": "https://example.test/"},
    )

    result = adapter.navigate(BrowserNavigateInput(url="https://example.test/"), call)

    assert result.success is False
    assert [item[0][1] for item in events] == ["browser.launch_started", "browser.launch_failed"]
    assert events[-1][1]["error_category"] == "browser_launch_failure"
    assert "private provider detail" not in str(events)
