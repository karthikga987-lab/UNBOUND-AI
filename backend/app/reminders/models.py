from __future__ import annotations

from datetime import date, time
from enum import StrEnum
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StringConstraints, UUID4, model_validator
from typing_extensions import Annotated


ReminderTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ReminderDescription = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class ReminderStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    COMPLETED = "completed"


class ReminderNotificationStatus(StrEnum):
    NOT_ELIGIBLE = "not_eligible"
    WAITING = "waiting"
    CLAIMED = "claimed"
    RETRY_WAIT = "retry_wait"
    SUBMITTED = "submitted"


class ReminderModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be a valid IANA time zone") from exc
    return value


class ReminderCreateRequest(ReminderModel):
    title: ReminderTitle
    description: ReminderDescription | None = None
    due_date: date | Literal["today", "tomorrow"]
    due_time: time | None = None
    timezone: str
    explicitly_confirmed: StrictBool = False

    @model_validator(mode="after")
    def validate_local_due_time(self) -> "ReminderCreateRequest":
        validate_timezone(self.timezone)
        if self.due_time is not None and self.due_time.tzinfo is not None:
            raise ValueError("due_time must be a local wall-clock time; timezone is supplied separately")
        return self


class ReminderUpdateRequest(ReminderModel):
    title: ReminderTitle | None = None
    description: ReminderDescription | None = None
    due_date: date | Literal["today", "tomorrow"] | None = None
    due_time: time | None = None
    timezone: str | None = None

    @model_validator(mode="after")
    def validate_change(self) -> "ReminderUpdateRequest":
        changed = self.model_fields_set
        if not changed:
            raise ValueError("Provide at least one reminder field to update")
        if "title" in changed and self.title is None:
            raise ValueError("title cannot be null")
        if "due_date" in changed and self.due_date is None:
            raise ValueError("due_date cannot be null")
        if "timezone" in changed and self.timezone is None:
            raise ValueError("timezone cannot be null")
        if self.timezone is not None:
            validate_timezone(self.timezone)
        if self.due_time is not None and self.due_time.tzinfo is not None:
            raise ValueError("due_time must be a local wall-clock time; timezone is supplied separately")
        return self


class ReminderRecord(ReminderModel):
    reminder_id: UUID4 = Field(default_factory=uuid4)
    title: ReminderTitle
    description: ReminderDescription | None = None
    due_date: date
    due_time: time | None = None
    timezone: str
    due_at: AwareDatetime | None = None
    status: ReminderStatus
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    notification_status: ReminderNotificationStatus = ReminderNotificationStatus.NOT_ELIGIBLE
    notification_attempts: int = Field(default=0, ge=0)
    notification_last_attempt_at: AwareDatetime | None = None
    notification_submitted_at: AwareDatetime | None = None
    notification_error_code: str | None = None
