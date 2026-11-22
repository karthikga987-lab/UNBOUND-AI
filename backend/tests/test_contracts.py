from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from backend.app.schemas.contracts import (
    ApprovalRequest,
    EvidenceItem,
    EvidenceSourceType,
    EventStatus,
    ExecutionEvent,
    TaskCreateRequest,
    TaskResponse,
    TaskStatus,
    TaskStep,
    StepStatus,
    ToolCall,
    ToolDefinition,
    ToolResult,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowParameter,
)


def test_task_request_validates_required_text_and_default():
    request = TaskCreateRequest(user_request=" Find latest report ")
    assert request.user_request == "Find latest report"
    assert request.auto_execute is False
    assert TaskCreateRequest(user_request="compare files", auto_execute=True).auto_execute
    with pytest.raises(ValidationError):
        TaskCreateRequest()
    with pytest.raises(ValidationError):
        TaskCreateRequest(user_request="  ")


def test_status_values_are_enums_and_invalid_values_rejected():
    step = TaskStep(step_id="step_1", tool_name="document.search", description="Search", expected_outcome="Results")
    assert step.status == StepStatus.PENDING
    with pytest.raises(ValidationError):
        TaskResponse(user_request="find", status="not-a-status")
    with pytest.raises(ValidationError):
        TaskStep(step_id="s", tool_name="x", description="x", expected_outcome="x", status="not-a-status")


def test_task_step_required_fields_defaults_and_nested_task_response():
    task_id = uuid4()
    step = TaskStep(step_id="step_1", tool_name="browser.navigate", description="Open report", expected_outcome="Report page visible")
    assert step.input == {}
    assert step.requires_approval is False
    task = TaskResponse(user_request="Open report", task_id=task_id, plan=[step])
    assert task.plan[0].step_id == "step_1"
    assert task.status == TaskStatus.CREATED
    assert task.pending_approvals == []
    assert task.created_at.tzinfo == timezone.utc
    with pytest.raises(ValidationError):
        TaskStep(tool_name="x", description="x", expected_outcome="x")


def test_identifiers_are_uuid4_and_datetimes_are_normalized_to_utc():
    task = TaskResponse(user_request="find")
    assert task.task_id.version == 4
    assert isinstance(task.task_id, UUID)
    offset_time = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    event = ExecutionEvent(task_id=task.task_id, event_type="step.started", message="Starting", status=EventStatus.RUNNING, timestamp=offset_time)
    assert event.event_id.version == 4
    assert event.timestamp.utcoffset() == timedelta(0)
    # UUID version 1 is not accepted for IDs that the contract defines as UUID4.
    version_one = UUID("00000000-0000-1000-8000-000000000001")
    with pytest.raises(ValidationError):
        TaskResponse(user_request="find", task_id=version_one)
    with pytest.raises(ValidationError):
        ExecutionEvent(task_id=task.task_id, event_type="created", message="Created", status="pending", timestamp=datetime(2026, 1, 1))


def test_evidence_url_validation_and_task_provenance():
    task_id = uuid4()
    evidence = EvidenceItem(task_id=task_id, source_type=EvidenceSourceType.BROWSER, source_name="Annual report", source_url="https://example.test/report", excerpt="Revenue increased", supports_claim="Revenue increased")
    assert evidence.source_url is not None
    assert evidence.evidence_id.version == 4
    with pytest.raises(ValidationError):
        EvidenceItem(task_id=task_id, source_type="browser", source_name="Page", source_url="javascript:alert(1)", supports_claim="claim")
    with pytest.raises(ValidationError):
        EvidenceItem(task_id=task_id, source_type="browser", source_name="Page", source_url="ftp://example.test", supports_claim="claim")


def test_tool_and_approval_contracts_have_no_executable_fields():
    definition = ToolDefinition(tool_name="browser.click", description="Click an element", input_schema={"type": "object"})
    assert not hasattr(definition, "execute")
    task_id = uuid4()
    call = ToolCall(task_id=task_id, step_id="step_1", tool_name=definition.tool_name, arguments={"selector": "#go"})
    approval = ApprovalRequest(task_id=task_id, step_id="step_1", tool_name="browser.click", description="Submit form", arguments={"selector": "#submit"})
    result = ToolResult(success=True, data={"clicked": True})
    assert call.tool_call_id.version == approval.approval_id.version == 4
    assert result.error is None and result.evidence == []


def test_workflow_models_validate_nested_parameters_actions_and_url():
    workflow = WorkflowDefinition(
        name="Download report",
        start_url="https://example.test/reports",
        parameters=[WorkflowParameter(name="year", type="integer", required=True, description="Report year")],
        steps=[WorkflowAction(step_id="step_1", action="click", target={"type": "role", "role": "button", "name": "Download report", "text": "Download"}, input={"format": "pdf"}, expected_outcome="PDF downloaded")],
    )
    assert workflow.workflow_id.version == 4
    assert workflow.parameters[0].required
    assert workflow.steps[0].action == "click"
    restored = WorkflowDefinition.model_validate_json(workflow.model_dump_json())
    assert restored.steps[0].target == {
        "type": "role",
        "role": "button",
        "name": "Download report",
        "text": "Download",
    }
    with pytest.raises(ValidationError):
        WorkflowDefinition(name="bad", start_url="file:///tmp/page", steps=[])


def test_json_serialization_round_trip_preserves_nested_contracts():
    task = TaskResponse(
        user_request="Find the report",
        plan=[TaskStep(step_id="step_1", tool_name="document.search", description="Search", expected_outcome="Report found", verification={"verified": True})],
        evidence=[EvidenceItem(task_id=uuid4(), source_type="document", source_name="report.pdf", file_path="docs/report.pdf", supports_claim="report date")],
    )
    encoded = task.model_dump_json()
    decoded = TaskResponse.model_validate_json(encoded)
    assert decoded.task_id == task.task_id
    assert decoded.plan[0].verification == {"verified": True}
    assert decoded.evidence[0].source_type == EvidenceSourceType.DOCUMENT


def test_execution_event_evidence_references_are_uuid4():
    task_id = uuid4()
    evidence = EvidenceItem(task_id=task_id, source_type="tool", source_name="Search", supports_claim="source found")
    event = ExecutionEvent(task_id=task_id, event_type="evidence.captured", message="Evidence captured", status="completed", evidence_ids=[evidence.evidence_id])
    assert event.evidence_ids == [evidence.evidence_id]
    with pytest.raises(ValidationError):
        ToolResult(success="yes")
