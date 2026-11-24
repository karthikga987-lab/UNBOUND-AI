from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

from pydantic import UUID4

from backend.app.conversation.store import ConversationStoreError, SQLiteConversationStore
from backend.app.reminders.models import ReminderCreateRequest, ReminderStatus, ReminderUpdateRequest


class ReminderStoreError(RuntimeError):
    """Safe reminder persistence error."""


class ReminderNotFoundError(ReminderStoreError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_due_date(due_date: date | str, timezone_name: str, *, now: datetime | None = None) -> date:
    if isinstance(due_date, date) and not isinstance(due_date, datetime):
        return due_date
    local_now = now.astimezone(ZoneInfo(timezone_name)) if now is not None else datetime.now(ZoneInfo(timezone_name))
    if due_date == "today":
        return local_now.date()
    if due_date == "tomorrow":
        return local_now.date() + timedelta(days=1)
    return date.fromisoformat(str(due_date))


def _due_at(due_date: str, due_time: str | None, timezone_name: str) -> str | None:
    if due_time is None:
        return None
    zone = ZoneInfo(timezone_name)
    local = datetime.fromisoformat(f"{due_date}T{due_time}")
    aware = local.replace(tzinfo=zone, fold=0)
    # Reject nonexistent or ambiguous local wall times at DST boundaries instead
    # of silently changing what the user asked for.
    if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
        raise ValueError("The selected local due time does not exist because of a time-zone clock change")
    if aware.utcoffset() != local.replace(tzinfo=zone, fold=1).utcoffset():
        raise ValueError("The selected local due time is ambiguous because of a time-zone clock change")
    return aware.astimezone(timezone.utc).isoformat()


class SQLiteReminderStore:
    """Reminder tables share the local SQLite file but remain a separate domain."""

    def __init__(self, database: SQLiteConversationStore) -> None:
        self.database = database

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with self.database.transaction() as db:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS reminders (
                        reminder_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        description TEXT,
                        due_date TEXT NOT NULL,
                        due_time TEXT,
                        timezone TEXT NOT NULL,
                        due_at TEXT,
                        status TEXT NOT NULL CHECK(status IN ('proposed','active','completed')),
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    )"""
                )
                db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at, due_date)")
                # Additive, idempotent migration. Existing reminder rows are not
                # rewritten; missing delivery rows mean "waiting" or ineligible.
                db.execute(
                    """CREATE TABLE IF NOT EXISTS reminder_notifications (
                        reminder_id TEXT PRIMARY KEY REFERENCES reminders(reminder_id) ON DELETE CASCADE,
                        state TEXT NOT NULL CHECK(state IN ('waiting','claimed','retry_wait','submitted','not_eligible')),
                        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                        last_attempt_at TEXT,
                        next_attempt_at TEXT,
                        submitted_at TEXT,
                        claim_token TEXT,
                        claim_until TEXT,
                        last_error_code TEXT
                    )"""
                )
                db.execute("CREATE INDEX IF NOT EXISTS idx_reminder_notifications_retry ON reminder_notifications(state,next_attempt_at,claim_until)")
                yield db
        except ConversationStoreError as exc:
            raise ReminderStoreError("Reminder storage operation failed") from exc

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, object]:
        record = dict(row)
        state = record.pop("notification_state", None)
        record["notification_status"] = state or ("waiting" if record.get("status") == ReminderStatus.ACTIVE.value else "not_eligible")
        if record.get("status") != ReminderStatus.ACTIVE.value and record["notification_status"] not in {"submitted", "retry_wait"}:
            record["notification_status"] = "not_eligible"
        record["notification_attempts"] = record.pop("attempts", 0) or 0
        record["notification_last_attempt_at"] = record.pop("last_attempt_at", None)
        record["notification_submitted_at"] = record.pop("submitted_at", None)
        record["notification_error_code"] = record.pop("last_error_code", None)
        return record

    @staticmethod
    def _joined_by_id(db: sqlite3.Connection, reminder_id: str):
        return db.execute(
            """SELECT r.*, n.state AS notification_state, n.attempts, n.last_attempt_at,
                      n.submitted_at, n.last_error_code
               FROM reminders r LEFT JOIN reminder_notifications n ON n.reminder_id=r.reminder_id
               WHERE r.reminder_id=?""",
            (reminder_id,),
        ).fetchone()

    def create(self, request: ReminderCreateRequest) -> dict[str, object]:
        from uuid import uuid4
        reminder_id = str(uuid4())
        created_at = _now()
        due_date = resolve_due_date(request.due_date, request.timezone).isoformat()
        due_time = request.due_time.isoformat() if request.due_time else None
        due_at = _due_at(due_date, due_time, request.timezone)
        status = ReminderStatus.ACTIVE.value if request.explicitly_confirmed else ReminderStatus.PROPOSED.value
        with self._transaction() as db:
            db.execute(
                """INSERT INTO reminders(reminder_id,title,description,due_date,due_time,timezone,due_at,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (reminder_id, request.title, request.description, due_date, due_time, request.timezone, due_at, status, created_at),
            )
            row = self._joined_by_id(db, reminder_id)
            return self._record(row)

    def list(self) -> list[dict[str, object]]:
        with self._transaction() as db:
            rows = db.execute(
                """SELECT r.*, n.state AS notification_state, n.attempts, n.last_attempt_at,
                          n.submitted_at, n.last_error_code
                   FROM reminders r LEFT JOIN reminder_notifications n ON n.reminder_id=r.reminder_id
                   ORDER BY r.due_at IS NULL, r.due_at, r.due_date, r.created_at"""
            ).fetchall()
            return [self._record(row) for row in rows]

    def get(self, reminder_id: UUID4 | str) -> dict[str, object]:
        with self._transaction() as db:
            row = self._joined_by_id(db, str(reminder_id))
            if row is None:
                raise ReminderNotFoundError("Reminder was not found")
            return self._record(row)

    def confirm(self, reminder_id: UUID4 | str) -> dict[str, object]:
        return self._change_status(reminder_id, ReminderStatus.PROPOSED, ReminderStatus.ACTIVE, None)

    def complete(self, reminder_id: UUID4 | str) -> dict[str, object]:
        return self._change_status(reminder_id, ReminderStatus.ACTIVE, ReminderStatus.COMPLETED, _now())

    def _change_status(self, reminder_id, expected: ReminderStatus, target: ReminderStatus, completed_at: str | None):
        with self._transaction() as db:
            cursor = db.execute(
                "UPDATE reminders SET status=?, completed_at=? WHERE reminder_id=? AND status=?",
                (target.value, completed_at, str(reminder_id), expected.value),
            )
            if cursor.rowcount == 0:
                row = db.execute("SELECT status FROM reminders WHERE reminder_id=?", (str(reminder_id),)).fetchone()
                if row is None:
                    raise ReminderNotFoundError("Reminder was not found")
                raise ValueError(f"Reminder must be {expected.value} before it can become {target.value}")
            if target is ReminderStatus.COMPLETED:
                db.execute(
                    """UPDATE reminder_notifications SET state='not_eligible',
                       claim_token=NULL, claim_until=NULL, next_attempt_at=NULL
                       WHERE reminder_id=? AND state IN ('waiting','claimed','retry_wait')""",
                    (str(reminder_id),),
                )
            return self._record(self._joined_by_id(db, str(reminder_id)))

    def update(self, reminder_id: UUID4 | str, request: ReminderUpdateRequest) -> dict[str, object]:
        current = self.get(reminder_id)
        fields = request.model_dump(exclude_unset=True)
        values = {**current, **{key: (value.isoformat() if hasattr(value, "isoformat") else value) for key, value in fields.items()}}
        values["due_date"] = resolve_due_date(values["due_date"], values["timezone"]).isoformat()
        due_at = _due_at(values["due_date"], values["due_time"], values["timezone"])
        schedule_changed = any(
            str(values.get(key)) != str(current.get(key))
            for key in ("due_date", "due_time", "timezone")
        )
        with self._transaction() as db:
            db.execute(
                """UPDATE reminders SET title=?,description=?,due_date=?,due_time=?,timezone=?,due_at=? WHERE reminder_id=?""",
                (values["title"], values["description"], values["due_date"], values["due_time"], values["timezone"], due_at, str(reminder_id)),
            )
            if schedule_changed:
                # An explicit reschedule is a new notification occasion. Clear
                # the old delivery attempt and invalidate any outstanding claim.
                db.execute(
                    """UPDATE reminder_notifications SET state='waiting', attempts=0,
                       last_attempt_at=NULL, next_attempt_at=NULL, submitted_at=NULL,
                       claim_token=NULL, claim_until=NULL, last_error_code=NULL
                       WHERE reminder_id=? AND EXISTS(
                         SELECT 1 FROM reminders WHERE reminder_id=? AND status='active')""",
                    (str(reminder_id), str(reminder_id)),
                )
            return self._record(self._joined_by_id(db, str(reminder_id)))

    def delete(self, reminder_id: UUID4 | str) -> bool:
        with self._transaction() as db:
            cursor = db.execute("DELETE FROM reminders WHERE reminder_id=?", (str(reminder_id),))
            return cursor.rowcount > 0
