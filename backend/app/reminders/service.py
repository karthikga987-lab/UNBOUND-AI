"""Shared reminder operations used by both HTTP routes and conversation."""
from __future__ import annotations

import re
from datetime import date, time

from backend.app.reminders.models import ReminderCreateRequest, ReminderRecord, ReminderStatus, ReminderUpdateRequest
from backend.app.reminders.store import ReminderNotFoundError, SQLiteReminderStore


class ReminderAmbiguousError(ValueError):
    pass


class ReminderService:
    def __init__(self, store: SQLiteReminderStore) -> None:
        self.store = store

    def propose(
        self, *, title: str, due_date: date | str, due_time: time | None, timezone: str, description: str | None = None
    ) -> ReminderRecord:
        request = ReminderCreateRequest(
            title=title, description=description, due_date=due_date, due_time=due_time,
            timezone=timezone, explicitly_confirmed=False,
        )
        return ReminderRecord.model_validate(self.store.create(request))

    def create(self, request: ReminderCreateRequest) -> ReminderRecord:
        return ReminderRecord.model_validate(self.store.create(request))

    def list(self) -> list[ReminderRecord]:
        return [ReminderRecord.model_validate(item) for item in self.store.list()]

    def get(self, reminder_id: str) -> ReminderRecord:
        return ReminderRecord.model_validate(self.store.get(reminder_id))

    def confirm(self, reminder_id: str) -> ReminderRecord:
        return ReminderRecord.model_validate(self.store.confirm(reminder_id))

    def complete(self, reminder_id: str) -> ReminderRecord:
        return ReminderRecord.model_validate(self.store.complete(reminder_id))

    def update(self, reminder_id: str, request: ReminderUpdateRequest) -> ReminderRecord:
        return ReminderRecord.model_validate(self.store.update(reminder_id, request))

    def delete(self, reminder_id: str) -> bool:
        return self.store.delete(reminder_id)

    def find_by_title(self, query: str, *, statuses: set[ReminderStatus] | None = None) -> ReminderRecord:
        ignored = {"a", "an", "the", "my", "me", "reminder", "reminders", "please", "for", "to"}
        terms = {term for term in re.findall(r"[\w-]+", query.casefold()) if term not in ignored}
        if not terms:
            raise ReminderNotFoundError("Reminder title is required")
        matches = []
        for item in self.list():
            if statuses and item.status not in statuses:
                continue
            title_terms = set(re.findall(r"[\w-]+", item.title.casefold()))
            if terms.issubset(title_terms):
                matches.append(item)
        if not matches:
            raise ReminderNotFoundError("Reminder was not found")
        if len(matches) != 1:
            raise ReminderAmbiguousError("More than one reminder matches that title")
        return matches[0]

    def confirm_by_title(self, title: str) -> ReminderRecord:
        reminder = self.find_by_title(title, statuses={ReminderStatus.PROPOSED})
        return self.confirm(str(reminder.reminder_id))

    def complete_by_title(self, title: str) -> ReminderRecord:
        reminder = self.find_by_title(title, statuses={ReminderStatus.ACTIVE})
        return self.complete(str(reminder.reminder_id))

    def delete_by_title(self, title: str) -> ReminderRecord:
        reminder = self.find_by_title(title)
        self.delete(str(reminder.reminder_id))
        return reminder
