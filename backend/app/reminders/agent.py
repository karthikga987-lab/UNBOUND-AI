"""Standalone local notification worker for confirmed reminders.

Run with ``python -m backend.app.reminders.agent``. This module does not import
FastAPI and can operate while the API and browser are stopped.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import time as time_module
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Callable, Protocol
from uuid import uuid4
from logging.handlers import RotatingFileHandler

from backend.app.conversation.store import SQLiteConversationStore, default_database_path
from backend.app.reminders.agent_status import HEARTBEAT_FILE_NAME, agent_directory, write_agent_json
from backend.app.reminders.notification_store import NotificationStoreError, SQLiteReminderNotificationStore
from backend.app.reminders.store import ReminderStoreError, SQLiteReminderStore

APP_ID = "UNBOUND.AI.Reminders"
LOGGER = logging.getLogger("unbound.reminder_agent")


class NotificationAdapter(Protocol):
    def show(self, *, title: str, body: str, tag: str) -> None: ...


class WindowsToastAdapter:
    """Submit a toast to Windows; API success is not proof it was displayed."""

    def show(self, *, title: str, body: str, tag: str) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows toast notifications require Windows")
        try:
            from win11toast import notify
        except ImportError as exc:
            raise RuntimeError("win11toast dependency is not installed") from exc
        notify(
            title,
            body,
            app_id=APP_ID,
            tag=tag[:16],
            group="unbound-reminders",
        )


def send_test_notification(adapter: NotificationAdapter | None = None) -> None:
    """Submit a clearly labeled test toast without reading or changing reminders."""
    (adapter or WindowsToastAdapter()).show(
        title="UNBOUND test notification",
        body="Windows desktop notification test. No reminder data was changed.",
        tag=f"test-{uuid4().hex}",
    )


class ReminderNotificationAgent:
    def __init__(
        self,
        database_path: str | Path,
        adapter: NotificationAdapter,
        *,
        poll_interval_seconds: int = 60,
        all_day_time: time = time(9, 0),
    ) -> None:
        if poll_interval_seconds < 15:
            raise ValueError("poll_interval_seconds must be at least 15")
        self.database_path = Path(database_path)
        self.adapter = adapter
        self.poll_interval_seconds = poll_interval_seconds
        self.all_day_time = all_day_time
        self.owner = str(uuid4())
        self.store = SQLiteReminderNotificationStore(self.database_path)
        self.reminders = SQLiteReminderStore(SQLiteConversationStore(self.database_path))
        self._initialized = False

    def initialize(self) -> None:
        # Reading the reminder store creates/migrates its existing schema first;
        # notification state is an additive table in that same database.
        self.reminders.list()
        self.store.initialize()
        self._initialized = True

    @staticmethod
    def _notification_text(reminder: dict[str, object]) -> tuple[str, str]:
        title = str(reminder.get("title") or "UNBOUND reminder").strip()[:200]
        due_date = str(reminder.get("due_date") or "")
        due_time = reminder.get("due_time")
        timezone_name = str(reminder.get("timezone") or "")
        when = f"{due_date} {str(due_time)[:5]} {timezone_name}" if due_time else f"{due_date} (all day, {timezone_name})"
        description = str(reminder.get("description") or "").strip()
        body = f"{description[:500]} — due {when}" if description else f"Due {when}"
        return title, body

    def run_once(self, *, now: datetime | None = None) -> bool:
        if not self._initialized:
            self.initialize()
        instant = now or datetime.now(timezone.utc)
        claim = self.store.claim_next_due(now=instant, owner=self.owner, all_day_time=self.all_day_time)
        if claim is None:
            return False
        reminder_id = str(claim["reminder_id"])
        token = str(claim["claim_token"])
        try:
            current = self.store.get_claimed_reminder(reminder_id, token)
            if current is None:
                return False
            # Recheck due-time after claiming so an edit made while queued cannot
            # cause an early toast. Claim still serializes competing instances.
            due = self.store.due_instant(current, self.all_day_time)
            if due > instant.astimezone(timezone.utc):
                self.store.defer_claim(reminder_id, token, still_active=True)
                return False
            title, body = self._notification_text(current)
            # A stable tag makes OS retries replace, rather than stack, the same
            # reminder toast where the Windows notification platform honors tags.
            self.adapter.show(title=title, body=body, tag=reminder_id.replace("-", ""))
            if not self.store.mark_submitted(reminder_id, token, now=instant):
                LOGGER.warning("Toast submission state was not recorded for reminder id=%s", reminder_id)
            return True
        except Exception as exc:
            try:
                self.store.mark_failed(
                    reminder_id,
                    token,
                    now=instant,
                    error_code=type(exc).__name__,
                    attempts=int(claim.get("attempts", 1)),
                )
            except NotificationStoreError:
                LOGGER.error("Could not persist notification failure state: error_class=NotificationStoreError reminder_id=%s", reminder_id)
            LOGGER.error("Notification submission failed: error_class=%s reminder_id=%s", type(exc).__name__, reminder_id)
            return False

    def write_heartbeat(self) -> None:
        write_agent_json(
            agent_directory() / HEARTBEAT_FILE_NAME,
            {
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "poll_interval_seconds": self.poll_interval_seconds,
            },
        )

    def run_forever(self, stop: Callable[[], bool]) -> None:
        self.initialize()
        while not stop():
            try:
                self.run_once()
                self.write_heartbeat()
            except (NotificationStoreError, ReminderStoreError) as exc:
                LOGGER.error("Reminder notification storage unavailable: error_class=%s", type(exc).__name__)
            except Exception as exc:
                LOGGER.error("Reminder notification cycle failed: error_class=%s", type(exc).__name__)
            # Short sleeps permit clean shutdown without a busy loop.
            for _ in range(self.poll_interval_seconds):
                if stop():
                    break
                time_module.sleep(1)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UNBOUND local reminder notification agent")
    parser.add_argument("--database-path", type=Path, default=default_database_path())
    parser.add_argument("--poll-interval", type=int, default=int(os.getenv("UNBOUND_REMINDER_POLL_SECONDS", "60")))
    parser.add_argument("--all-day-time", default=os.getenv("UNBOUND_ALL_DAY_REMINDER_TIME", "09:00"))
    parser.add_argument("--test-notification", action="store_true", help="submit a test toast without accessing reminder data")
    return parser.parse_args()


def main() -> int:
    level = os.getenv("UNBOUND_AGENT_LOG_LEVEL", "INFO").upper()
    try:
        log_dir = agent_directory()
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        file_handler = RotatingFileHandler(log_dir / "notification-agent.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
        logging.basicConfig(
            level=getattr(logging, level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            handlers=[file_handler],
        )
    except OSError:
        logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse_args()
    if args.test_notification:
        try:
            send_test_notification()
            LOGGER.info("Test toast submitted to Windows notification API; check Windows notifications for display")
            print("Test toast submitted to the Windows notification API. Check Windows notifications; API acceptance does not guarantee visible display.")
            return 0
        except Exception as exc:
            LOGGER.error("Test toast submission failed: error_class=%s", type(exc).__name__)
            print(f"Test toast submission failed: {type(exc).__name__}")
            return 1
    try:
        all_day_time = time.fromisoformat(args.all_day_time)
        if all_day_time.tzinfo is not None:
            raise ValueError
        agent = ReminderNotificationAgent(
            args.database_path,
            WindowsToastAdapter(),
            poll_interval_seconds=args.poll_interval,
            all_day_time=all_day_time,
        )
    except (ValueError, NotificationStoreError, ReminderStoreError) as exc:
        LOGGER.error("Reminder notification agent configuration failed: error_class=%s", type(exc).__name__)
        return 2
    stopped = False

    def stop_handler(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)
    try:
        agent.run_forever(lambda: stopped)
    except (NotificationStoreError, ReminderStoreError) as exc:
        LOGGER.error("Reminder notification agent could not start: error_class=%s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
