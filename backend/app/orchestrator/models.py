"""Provisional internal models used until shared contracts are integrated."""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    NEEDS_CLARIFICATION = "needs_clarification"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class EvidenceItem(BaseModel):
    source_type: str = "tool"
    reference: str
    summary: str = ""
    verified: bool = False


class ExecutionEvent(BaseModel):
    timestamp: str
    status: str
    message: str
    step_id: str | None = None


class TaskStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    tool_name: str
    description: str
    input: dict[str, Any] = Field(default_factory=dict)
    expected_outcome: str
    requires_approval: bool = False
    status: StepStatus = StepStatus.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)


class TaskCreateRequest(BaseModel):
    goal: str


class TaskResponse(BaseModel):
    task_id: str
    goal: str
    status: TaskStatus
    steps: list[TaskStep]
    current_step: int = 0
    completed_steps: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    events: list[ExecutionEvent] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    final_result: dict[str, Any] | None = None
    missing_information: list[str] = Field(default_factory=list)
