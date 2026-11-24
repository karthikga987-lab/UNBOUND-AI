from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StringConstraints, UUID4, model_validator
from typing_extensions import Annotated

MemoryText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]


class MemoryCategory(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    PROJECT = "project"
    RECURRING_CONTEXT = "recurring_context"
    INSTRUCTION = "instruction"
    OTHER = "other"


class MemoryStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    DELETED = "deleted"


class MemoryOrigin(StrEnum):
    USER_PROVIDED = "user_provided"
    MODEL_EXTRACTED = "model_extracted"
    USER_EDITED = "user_edited"


class MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryProposalRequest(MemoryModel):
    content: MemoryText
    category: MemoryCategory
    source_session_id: UUID4 | None = None
    source_message_id: UUID4 | None = None
    explicitly_requested: StrictBool = False

    @model_validator(mode="after")
    def source_message_needs_session(self) -> "MemoryProposalRequest":
        if self.source_message_id is not None and self.source_session_id is None:
            raise ValueError("source_session_id is required with source_message_id")
        return self


class MemoryRecord(MemoryModel):
    memory_id: UUID4 = Field(default_factory=uuid4)
    content: MemoryText
    category: MemoryCategory
    status: MemoryStatus
    origin: MemoryOrigin
    source_session_id: UUID4 | None = None
    source_message_id: UUID4 | None = None
    source_excerpt: str | None = None
    user_confirmed: StrictBool
    created_at: AwareDatetime
    updated_at: AwareDatetime
    approved_at: AwareDatetime | None = None


class MemoryExtractionRequest(MemoryModel):
    session_id: UUID4


class ExtractedMemoryCandidate(MemoryModel):
    content: MemoryText
    category: MemoryCategory
    source_message_id: UUID4


class MemoryExtractionOutput(MemoryModel):
    candidates: list[ExtractedMemoryCandidate] = Field(default_factory=list, max_length=10)


class MemoryDecisionResponse(MemoryModel):
    memory_id: UUID4
    status: MemoryStatus
    decided_at: AwareDatetime


class MemoryUpdateRequest(MemoryModel):
    content: MemoryText | None = None
    category: MemoryCategory | None = None

    @model_validator(mode="after")
    def must_change_something(self) -> "MemoryUpdateRequest":
        if self.content is None and self.category is None:
            raise ValueError("Provide content or category to update")
        return self


class MemorySettings(MemoryModel):
    retrieval_enabled: StrictBool


class MemorySettingsUpdate(MemoryModel):
    retrieval_enabled: StrictBool
