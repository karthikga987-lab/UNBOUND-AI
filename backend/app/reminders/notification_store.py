"""Atomic SQLite outbox for the standalone Windows notification agent."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


class NotificationStoreError(RuntimeError):
    """Safe notification persistence failure."""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class SQLiteReminderNotificationStore:
    """Notification attempt journal sharing the reminder database file."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        if os.name != "nt":
            os.chmod(self.database_path, 0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    def initialize(self) -> None:
        try:
            with self._connection() as db:
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
        except sqlite3.Error as exc:
            raise NotificationStoreError("Notification state storage is unavailable") from exc

    @staticmethod
    def due_instant(reminder: dict[str, Any], all_day_time: time) -> datetime:
        due_at = reminder.get("due_at")
        if due_at:
            parsed = datetime.fromisoformat(str(due_at))
            if parsed.tzinfo is None:
                raise ValueError("Stored reminder due_at must be timezone-aware")
            return parsed.astimezone(timezone.utc)
        due_date = date.fromisoformat(str(reminder["due_date"]))
        local_time = time.fromisoformat(str(reminder["due_time"])) if reminder.get("due_time") else all_day_time
        zone = ZoneInfo(str(reminder["timezone"]))
        local = datetime.combine(due_date, local_time)
        aware = local.replace(tzinfo=zone, fold=0)
        if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
            raise ValueError("Stored reminder local notification time does not exist")
        if aware.utcoffset() != local.replace(tzinfo=zone, fold=1).utcoffset():
            raise ValueError("Stored reminder local notification time is ambiguous")
        return aware.astimezone(timezone.utc)

    def claim_next_due(
        self,
        *,
        now: datetime,
        owner: str,
        lease_seconds: int = 120,
        all_day_time: time = time(9, 0),
    ) -> dict[str, Any] | None:
        """Atomically claim at most one due reminder across all agent processes."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now_utc = now.astimezone(timezone.utc)
        token = f"{owner}:{uuid4()}"
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute(
                    """SELECT r.* FROM reminders r
                       WHERE r.status='active'
                       ORDER BY COALESCE(r.due_at, r.due_date || 'T09:00:00+00:00'), r.created_at"""
                ).fetchall()
                eligible: list[tuple[datetime, sqlite3.Row]] = []
                for row in rows:
                    reminder = dict(row)
                    try:
                        due = self.due_instant(reminder, all_day_time)
                    except (ValueError, KeyError):
                        # Invalid legacy rows are safely skipped and surfaced as
                        # a diagnostic by the agent without exposing their text.
                        continue
                    if due <= now_utc:
                        eligible.append((due, row))
                eligible.sort(key=lambda pair: pair[0])
                for _, row in eligible:
                    reminder_id = str(row["reminder_id"])
                    db.execute(
                        "INSERT OR IGNORE INTO reminder_notifications(reminder_id,state) VALUES(?, 'waiting')",
                        (reminder_id,),
                    )
                    state = db.execute(
                        "SELECT * FROM reminder_notifications WHERE reminder_id=?", (reminder_id,)
                    ).fetchone()
                    if state is None or state["state"] == "submitted" or state["state"] == "not_eligible":
                        continue
                    if state["state"] == "claimed" and state["claim_until"] and state["claim_until"] > _iso(now_utc):
                        continue
                    if state["state"] == "retry_wait" and state["next_attempt_at"] and state["next_attempt_at"] > _iso(now_utc):
                        continue
                    claim_until = now_utc + timedelta(seconds=lease_seconds)
                    cursor = db.execute(
                        """UPDATE reminder_notifications
                           SET state='claimed', attempts=attempts+1, last_attempt_at=?,
                               claim_token=?, claim_until=?, next_attempt_at=NULL, last_error_code=NULL
                           WHERE reminder_id=? AND state IN ('waiting','retry_wait','claimed')
                           AND (claim_until IS NULL OR claim_until<=? OR state!='claimed')
                           AND (next_attempt_at IS NULL OR next_attempt_at<=?)""",
                        (_iso(now_utc), token, _iso(claim_until), reminder_id, _iso(now_utc), _iso(now_utc)),
                    )
                    if cursor.rowcount:
                        result = dict(row)
                        result.update({"claim_token": token, "attempts": int(state["attempts"]) + 1})
                        db.commit()
                        return result
                db.commit()
                return None
        except sqlite3.Error as exc:
            raise NotificationStoreError("Notification state storage is unavailable") from exc

    def get_claimed_reminder(self, reminder_id: str, claim_token: str) -> dict[str, Any] | None:
        try:
            with self._connection() as db:
                row = db.execute(
                    """SELECT r.*, n.state AS notification_status FROM reminders r
                       JOIN reminder_notifications n ON n.reminder_id=r.reminder_id
                       WHERE r.reminder_id=? AND r.status='active' AND n.state='claimed' AND n.claim_token=?""",
                    (reminder_id, claim_token),
                ).fetchone()
                return dict(row) if row else None
        except sqlite3.Error as exc:
            raise NotificationStoreError("Notification state storage is unavailable") from exc

    def mark_submitted(self, reminder_id: str, claim_token: str, *, now: datetime) -> bool:
        try:
            with self._connection() as db:
                cursor = db.execute(
                    """UPDATE reminder_notifications SET state='submitted', submitted_at=?,
                       claim_token=NULL, claim_until=NULL, next_attempt_at=NULL, last_error_code=NULL
                       WHERE reminder_id=? AND state='claimed' AND claim_token=?
                       AND EXISTS(SELECT 1 FROM reminders WHERE reminder_id=? AND status='active')""",
                    (_iso(now), reminder_id, claim_token, reminder_id),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise NotificationStoreError("Notification state storage is unavailable") from exc

    def mark_failed(
        self,
        reminder_id: str,
        claim_token: str,
        *,
        now: datetime,
        error_code: str,
        retry_base_seconds: int = 30,
        retry_max_seconds: int = 21600,
        attempts: int,
    ) -> bool:
        delay = min(retry_max_seconds, retry_base_seconds * (2 ** min(max(attempts - 1, 0), 10)))
        retry_at = now.astimezone(timezone.utc) + timedelta(seconds=delay)
        safe_code = "".join(char for char in error_code if char.isalnum() or char in "_.-")[:80] or "NotificationError"
        try:
            with self._connection() as db:
                cursor = db.execute(
                    """UPDATE reminder_notifications SET state='retry_wait', next_attempt_at=?,
                       claim_token=NULL, claim_until=NULL, last_error_code=?
                       WHERE reminder_id=? AND state='claimed' AND claim_token=?""",
                    (_iso(retry_at), safe_code, reminder_id, claim_token),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise NotificationStoreError("Notification state storage is unavailable") from exc

    def defer_claim(self, reminder_id: str, claim_token: str, *, still_active: bool) -> bool:
        state = "waiting" if still_active else "not_eligible"
        try:
            with self._connection() as db:
                cursor = db.execute(
                    """UPDATE reminder_notifications SET state=?, claim_token=NULL, claim_until=NULL,
                       next_attempt_at=NULL WHERE reminder_id=? AND state='claimed' AND claim_token=?""",
                    (state, reminder_id, claim_token),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise NotificationStoreError("Notification state storage is unavailable") from exc

    def agent_status(self, reminder_id: str) -> dict[str, Any]:
        self.initialize()
        try:
            with self._connection() as db:
                row = db.execute(
                    "SELECT state, attempts, last_attempt_at, submitted_at, last_error_code FROM reminder_notifications WHERE reminder_id=?",
                    (reminder_id,),
                ).fetchone()
                return dict(row) if row else {"state": "waiting", "attempts": 0}
        except sqlite3.Error as exc:
            raise NotificationStoreError("Notification state storage is unavailable") from exc
