from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone

import pytest
import sqlite3
from uuid import uuid4

from backend.app.conversation.store import SQLiteConversationStore
from backend.app.reminders.agent import ReminderNotificationAgent, send_test_notification
from backend.app.reminders.agent_status import HEARTBEAT_FILE_NAME, INSTALL_FILE_NAME, agent_directory, notification_agent_status, write_agent_json
from backend.app.reminders.models import ReminderCreateRequest, ReminderUpdateRequest
from backend.app.reminders.notification_store import SQLiteReminderNotificationStore
from backend.app.reminders.store import SQLiteReminderStore


class FakeAdapter:
    def __init__(self, *, fail: bool = False):
        self.calls: list[dict[str, str]] = []
        self.fail = fail

    def show(self, *, title: str, body: str, tag: str) -> None:
        self.calls.append({"title": title, "body": body, "tag": tag})
        if self.fail:
            raise OSError("synthetic toast rejection")


def create_reminder(database: SQLiteConversationStore, *, due: datetime, status_confirmed: bool = True, timezone_name: str = "Asia/Kolkata", title: str = "Synthetic demo reminder"):
    local = due.astimezone(__import__("zoneinfo").ZoneInfo(timezone_name))
    request = ReminderCreateRequest(
        title=title,
        due_date=local.date(),
        due_time=local.time().replace(tzinfo=None),
        timezone=timezone_name,
        explicitly_confirmed=status_confirmed,
    )
    return SQLiteReminderStore(database).create(request)


def build_agent(path, *, adapter=None, interval=15, all_day_time=time(9, 0)):
    fake = adapter or FakeAdapter()
    agent = ReminderNotificationAgent(path, fake, poll_interval_seconds=interval, all_day_time=all_day_time)
    agent.initialize()
    return agent, fake


def test_due_active_reminder_is_submitted_and_not_repeated(tmp_path):
    path = tmp_path / "reminders.sqlite3"
    db = SQLiteConversationStore(path)
    due = datetime.now(timezone.utc) - timedelta(minutes=2)
    reminder = create_reminder(db, due=due)
    agent, adapter = build_agent(path)

    assert agent.run_once(now=datetime.now(timezone.utc)) is True
    assert len(adapter.calls) == 1
    record = SQLiteReminderStore(db).get(reminder["reminder_id"])
    assert record["status"] == "active"
    assert record["notification_status"] == "submitted"
    assert record["notification_attempts"] == 1
    assert agent.run_once(now=datetime.now(timezone.utc)) is False
    assert len(adapter.calls) == 1


def test_future_proposed_completed_and_deleted_reminders_are_not_notified(tmp_path):
    path = tmp_path / "reminders.sqlite3"
    db = SQLiteConversationStore(path)
    now = datetime.now(timezone.utc)
    future = create_reminder(db, due=now + timedelta(hours=1))
    proposed = create_reminder(db, due=now - timedelta(hours=1), status_confirmed=False)
    completed = create_reminder(db, due=now - timedelta(hours=1))
    store = SQLiteReminderStore(db)
    store.complete(completed["reminder_id"])
    deleted = create_reminder(db, due=now - timedelta(hours=1))
    store.delete(deleted["reminder_id"])
    agent, adapter = build_agent(path)

    for _ in range(3):
        assert agent.run_once(now=now) is False
    assert adapter.calls == []
    assert store.get(future["reminder_id"])["status"] == "active"
    assert store.get(proposed["reminder_id"])["notification_status"] == "not_eligible"


def test_notification_state_survives_agent_restart(tmp_path):
    path = tmp_path / "reminders.sqlite3"
    db = SQLiteConversationStore(path)
    create_reminder(db, due=datetime.now(timezone.utc) - timedelta(minutes=1))
    first, first_adapter = build_agent(path)
    assert first.run_once(now=datetime.now(timezone.utc))
    second, second_adapter = build_agent(path)
    assert second.run_once(now=datetime.now(timezone.utc)) is False
    assert len(first_adapter.calls) == 1
    assert second_adapter.calls == []


def test_two_agent_instances_cannot_claim_the_same_reminder(tmp_path):
    path = tmp_path / "reminders.sqlite3"
    db = SQLiteConversationStore(path)
    create_reminder(db, due=datetime.now(timezone.utc) - timedelta(minutes=1))
    first, adapter1 = build_agent(path)
    second, adapter2 = build_agent(path)
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda agent: agent.run_once(now=now), [first, second]))
    assert sum(results) == 1
    assert len(adapter1.calls) + len(adapter2.calls) == 1


def test_failed_delivery_is_recorded_and_retried(tmp_path):
    path = tmp_path / "reminders.sqlite3"
    db = SQLiteConversationStore(path)
    reminder = create_reminder(db, due=datetime.now(timezone.utc) - timedelta(minutes=1))
    failing, adapter = build_agent(path, adapter=FakeAdapter(fail=True))
    now = datetime.now(timezone.utc)
    assert failing.run_once(now=now) is False
    record = SQLiteReminderStore(db).get(reminder["reminder_id"])
    assert record["notification_status"] == "retry_wait"
    assert record["notification_error_code"] == "OSError"
    succeeding, success_adapter = build_agent(path)
    assert succeeding.run_once(now=now + timedelta(seconds=31)) is True
    assert len(success_adapter.calls) == 1
    assert SQLiteReminderStore(db).get(reminder["reminder_id"])["notification_status"] == "submitted"


def test_completion_cancels_waiting_retry(tmp_path):
    path = tmp_path / "reminders.sqlite3"
    db = SQLiteConversationStore(path)
    reminder = create_reminder(db, due=datetime.now(timezone.utc) - timedelta(minutes=1))
    now = datetime.now(timezone.utc)
    failing, _ = build_agent(path, adapter=FakeAdapter(fail=True))
    assert failing.run_once(now=now) is False
    store = SQLiteReminderStore(db)
    store.complete(reminder["reminder_id"])
    completed = store.get(reminder["reminder_id"])
    assert completed["notification_status"] == "not_eligible"
    later, adapter = build_agent(path)
    assert later.run_once(now=now + timedelta(hours=1)) is False
    assert adapter.calls == []


def test_overdue_reminders_are_delivered_once_per_poll_without_storm(tmp_path):
    path = tmp_path / "reminders.sqlite3"
    db = SQLiteConversationStore(path)
    now = datetime.now(timezone.utc)
    create_reminder(db, due=now - timedelta(days=2), title="Synthetic overdue one")
    create_reminder(db, due=now - timedelta(days=1), title="Synthetic overdue two")
    agent, adapter = build_agent(path)
    assert agent.run_once(now=now)
    assert len(adapter.calls) == 1
    assert agent.run_once(now=now) is True
    assert len(adapter.calls) == 2
    assert agent.run_once(now=now) is False


def test_all_day_reminder_uses_local_nine_am_and_timezone(tmp_path):
    path = tmp_path / "reminders.sqlite3"
    db = SQLiteConversationStore(path)
    store = SQLiteReminderStore(db)
    proposal = store.create(ReminderCreateRequest(
        title="Synthetic all day", due_date=date(2026, 1, 2), due_time=None,
        timezone="Asia/Kolkata", explicitly_confirmed=True,
    ))
    agent, adapter = build_agent(path)
    not_due = datetime(2026, 1, 2, 3, 29, tzinfo=timezone.utc)  # 08:59 IST
    due = datetime(2026, 1, 2, 3, 30, tzinfo=timezone.utc)  # 09:00 IST
    assert agent.run_once(now=not_due) is False
    assert agent.run_once(now=due) is True
    assert len(adapter.calls) == 1
    assert store.get(proposal["reminder_id"])["notification_status"] == "submitted"


def test_migration_preserves_existing_reminder_rows(tmp_path):
    path = tmp_path / "reminders.sqlite3"
    reminder_id = str(uuid4())
    created = datetime.now(timezone.utc).isoformat()
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    # Simulate the pre-notification schema as found on an existing installation.
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE reminders (
            reminder_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT,
            due_date TEXT NOT NULL, due_time TEXT, timezone TEXT NOT NULL, due_at TEXT,
            status TEXT NOT NULL CHECK(status IN ('proposed','active','completed')),
            created_at TEXT NOT NULL, completed_at TEXT)""")
        connection.execute(
            "INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?,?,NULL)",
            (reminder_id, "Existing synthetic reminder", None, "2030-01-01", "09:00:00", "Asia/Kolkata", due, "active", created),
        )
    notification_store = SQLiteReminderNotificationStore(path)
    notification_store.initialize()
    after = SQLiteReminderStore(SQLiteConversationStore(path)).get(reminder_id)
    assert {key: after[key] for key in ("reminder_id", "title", "due_date", "due_time", "timezone", "status", "created_at")} == {
        "reminder_id": reminder_id,
        "title": "Existing synthetic reminder",
        "due_date": "2030-01-01",
        "due_time": "09:00:00",
        "timezone": "Asia/Kolkata",
        "status": "active",
        "created_at": created,
    }
    assert after["notification_status"] == "waiting"


def test_agent_reads_shared_database_without_fastapi_or_browser(tmp_path):
    path = tmp_path / "reminders.sqlite3"
    db = SQLiteConversationStore(path)
    create_reminder(db, due=datetime.now(timezone.utc) - timedelta(minutes=1))
    agent, adapter = build_agent(path)
    assert agent.run_once(now=datetime.now(timezone.utc)) is True
    assert len(adapter.calls) == 1


def test_agent_rejects_unreasonable_poll_interval(tmp_path):
    with pytest.raises(ValueError):
        ReminderNotificationAgent(tmp_path / "db.sqlite3", FakeAdapter(), poll_interval_seconds=1)


def test_test_notification_does_not_read_or_modify_reminder_database():
    adapter = FakeAdapter()
    send_test_notification(adapter)
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["title"] == "UNBOUND test notification"
    assert "No reminder data was changed" in adapter.calls[0]["body"]


def test_rescheduling_submitted_reminder_rearms_notification(tmp_path):
    path = tmp_path / "reminders.sqlite3"
    db = SQLiteConversationStore(path)
    store = SQLiteReminderStore(db)
    old_due = datetime.now(timezone.utc) - timedelta(minutes=1)
    reminder = create_reminder(db, due=old_due)
    agent, adapter = build_agent(path)
    assert agent.run_once(now=datetime.now(timezone.utc))
    changed_date = (datetime.now(timezone.utc) + timedelta(days=2)).date()
    updated = store.update(reminder["reminder_id"], ReminderUpdateRequest(due_date=changed_date))
    assert updated["notification_status"] == "waiting"
    assert updated["notification_attempts"] == 0
    assert agent.run_once(now=datetime.now(timezone.utc)) is False
    future = datetime.fromisoformat(updated["due_at"])
    assert agent.run_once(now=future + timedelta(seconds=1)) is True
    assert len(adapter.calls) == 2


def test_agent_health_uses_task_registration_and_fresh_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    import backend.app.reminders.agent_status as status_module

    now = datetime.now(timezone.utc)
    directory = agent_directory()
    write_agent_json(directory / INSTALL_FILE_NAME, {"app_id": "UNBOUND.AI.Reminders"})
    write_agent_json(directory / HEARTBEAT_FILE_NAME, {"heartbeat_at": now.isoformat(), "poll_interval_seconds": 60})
    monkeypatch.setattr(status_module, "_scheduled_task_exists", lambda: True)
    assert notification_agent_status(now=now) == {"installed": True, "running": True}
    monkeypatch.setattr(status_module, "_scheduled_task_exists", lambda: False)
    assert notification_agent_status(now=now) == {"installed": False, "running": True}
    monkeypatch.setattr(status_module, "_scheduled_task_exists", lambda: None)
    assert notification_agent_status(now=now) == {"installed": None, "running": True}
