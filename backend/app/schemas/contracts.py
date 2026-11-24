"""Authoritative Pydantic contracts shared across UNBOUND AI modules.

This module is intentionally independent of runtime implementations. In particular,
tool callables belong in the runtime registry, never in these serializable models.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, StrictBool, StringConstraints, UUID4, model_validator
from typing_extensions import Annotated


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
UTCDateTime = Annotated[datetime, AwareDatetime, AfterValidator(lambda value: value.astimezone(timezone.utc))]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ContractModel(BaseModel):
    """Shared strict defaults: reject misspelled fields and validate mutations."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class EvidenceSourceType(StrEnum):
    BROWSER = "browser"
    DOCUMENT = "document"
    CONTEXT = "context"
    TOOL = "tool"
    WORKFLOW = "workflow"
    REPORT = "report"


class EventStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class TaskCreateRequest(ContractModel):
    user_request: NonEmptyString
    auto_execute: StrictBool = False


class EvidenceItem(ContractModel):
    evidence_id: UUID4 = Field(default_factory=uuid4)
    task_id: UUID4
    source_type: EvidenceSourceType
    source_name: NonEmptyString
    source_url: HttpUrl | None = None
    file_path: str | None = None
    excerpt: str | None = None
    retrieved_at: UTCDateTime = Field(default_factory=utc_now)
    supports_claim: NonEmptyString


class TaskStep(ContractModel):
    # step_id is a stable, human-readable plan key (for example, "step_1").
    step_id: NonEmptyString
    tool_name: NonEmptyString
    description: NonEmptyString
    input: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[NonEmptyString] = Field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    requires_approval: StrictBool = False
    expected_outcome: NonEmptyString
    verification: dict[str, Any] | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_dependencies(self) -> "TaskStep":
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("Task step dependencies must be unique")
        if self.step_id in self.depends_on:
            raise ValueError("A task step cannot depend on itself")
        return self


class ApprovalRequest(ContractModel):
    approval_id: UUID4 = Field(default_factory=uuid4)
    task_id: UUID4
    step_id: NonEmptyString
    tool_name: NonEmptyString
    description: NonEmptyString
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: UTCDateTime = Field(default_factory=utc_now)


class ExecutionEvent(ContractModel):
    event_id: UUID4 = Field(default_factory=uuid4)
    task_id: UUID4
    step_id: str | None = None
    event_type: NonEmptyString
    message: NonEmptyString
    status: EventStatus
    timestamp: UTCDateTime = Field(default_factory=utc_now)
    evidence_ids: list[UUID4] = Field(default_factory=list)


class TaskResponse(ContractModel):
    task_id: UUID4 = Field(default_factory=uuid4)
    user_request: NonEmptyString
    status: TaskStatus = TaskStatus.CREATED
    plan: list[TaskStep] = Field(default_factory=list)
    # current_step is the step_id currently being handled; None means no active step.
    current_step: str | None = None
    completed_steps: list[str] = Field(default_factory=list)
    pending_approvals: list[ApprovalRequest] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: UTCDateTime = Field(default_factory=utc_now)
    updated_at: UTCDateTime = Field(default_factory=utc_now)


class ToolDefinition(ContractModel):
    tool_name: NonEmptyString
    description: NonEmptyString
    input_schema: dict[str, Any] = Field(default_factory=dict)
    requires_approval: StrictBool = False


class ToolCall(ContractModel):
    tool_call_id: UUID4 = Field(default_factory=uuid4)
    task_id: UUID4
    step_id: NonEmptyString
    tool_name: NonEmptyString
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(ContractModel):
    success: StrictBool
    data: Any = None
    error: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)


class WorkflowParameter(ContractModel):
    name: NonEmptyString
    type: NonEmptyString
    required: StrictBool = False
    default: Any = None
    description: str = ""


class WorkflowAction(ContractModel):
    # Like TaskStep.step_id, this is a stable workflow-local key, not a UUID.
    step_id: NonEmptyString
    action: NonEmptyString
    target: dict[str, Any] | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    expected_outcome: str | None = None


class WorkflowDefinition(ContractModel):
    workflow_id: UUID4 = Field(default_factory=uuid4)
    name: NonEmptyString
    description: str = ""
    start_url: HttpUrl
    parameters: list[WorkflowParameter] = Field(default_factory=list)
    steps: list[WorkflowAction] = Field(default_factory=list)
    created_at: UTCDateTime = Field(default_factory=utc_now)
    updated_at: UTCDateTime = Field(default_factory=utc_now)
