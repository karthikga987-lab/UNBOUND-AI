"""Browser-local models for encrypted, resumable replay state.

These models are not shared API or planner contracts. Sensitive action arguments
and one-time authorization tokens are intentionally excluded from ordinary model
representations and serialization.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, UUID4, model_validator


class ReplayActionJournalState(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ReplayPendingAction(BaseModel):
    """Public-safe descriptor for the next action, without prepared arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    replay_run_id: UUID4
    continuation_id: UUID4
    workflow_id: UUID4
    step_id: str
    action: str
    action_digest: str
    sequence_index: int = Field(ge=0)


class ReplayPolicyContext(BaseModel):
    """Trusted internal handoff carrying exact action arguments to orchestration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID4
    tool_call_id: UUID4
    replay_run_id: UUID4
    continuation_id: UUID4
    workflow_id: UUID4
    step_id: str
    action: str
    action_digest: str
    sequence_index: int = Field(ge=0)
    exact_arguments: dict[str, Any] = Field(repr=False, exclude=True)


class ReplayContinuation(BaseModel):
    """Opaque checkpoint cursor; parameters and prepared values are not included."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    replay_run_id: UUID4
    continuation_id: UUID4
    task_id: UUID4
    session_id: UUID4
    workflow_id: UUID4
    workflow_digest: str
    checkpoint_version: int = Field(ge=0)
    next_action_index: int = Field(ge=0)
    completed_step_ids: tuple[str, ...] = ()
    uncertain_step_id: str | None = None


class ReplayStepAuthorization(BaseModel):
    """Orchestrator-issued, one-use permit scoped to a single prepared action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_id: UUID4
    task_id: UUID4
    tool_call_id: UUID4
    replay_run_id: UUID4
    continuation_id: UUID4
    workflow_id: UUID4
    step_id: str
    action_digest: str
    decision: Literal["approved", "allowed_without_approval"]
    approval_id: UUID4 | None = None
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    one_time_token: SecretStr = Field(repr=False, exclude=True)

    @model_validator(mode="after")
    def _validate_decision_binding(self) -> "ReplayStepAuthorization":
        if self.decision == "approved" and self.approval_id is None:
            raise ValueError("Approved replay authorization requires an approval ID")
        if self.decision == "allowed_without_approval" and self.approval_id is not None:
            raise ValueError("No-approval replay authorization cannot include an approval ID")
        if not self.one_time_token.get_secret_value():
            raise ValueError("Replay authorization token must be non-empty")
        if self.expires_at <= self.issued_at:
            raise ValueError("Replay authorization expiry must follow issue time")
        return self


class ReplayStepCheckpoint(BaseModel):
    """Non-sensitive journal entry for one workflow action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    action: str
    action_digest: str
    state: ReplayActionJournalState
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    result_digest: str | None = None


class ReplayExecutionResult(BaseModel):
    """Safe replay status intended for callers; it never includes action values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID4
    replay_run_id: UUID4
    workflow_id: UUID4
    session_id: UUID4
    status: Literal["pending_action", "completed", "failed", "needs_attention"]
    continuation: ReplayContinuation | None = None
    pending_action: ReplayPendingAction | None = None
    checkpoints: tuple[ReplayStepCheckpoint, ...] = ()
    error: str | None = None


class PreparedReplayAction(BaseModel):
    """Validated internal action payload supplied by browser replay preparation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    action: str
    arguments: dict[str, Any] = Field(repr=False)


class ReplayActionOutcome(BaseModel):
    """Trusted internal access to a persisted action result for verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    replay_run_id: UUID4
    step_id: str
    action_digest: str
    result: dict[str, Any] = Field(repr=False, exclude=True)
