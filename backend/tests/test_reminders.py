from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from backend.app.api.runtime import TaskRuntime
from backend.app.conversation.store import SQLiteConversationStore
from backend.app.main import create_app
from backend.app.reminders.store import resolve_due_date


def client_for(path):
    return TestClient(create_app(TaskRuntime(conversation_store=SQLiteConversationStore(path))))


def reminder_payload(**updates):
    return {
        "title": "Finish project presentation",
        "description": "Synthetic test reminder",
        "due_date": "2030-02-02",
        "due_time": "09:30:00",
        "timezone": "America/Los_Angeles",
        **updates,
    }


def test_reminder_requires_confirmation_and_persists(tmp_path):
    path = tmp_path / "local.sqlite3"
    client = client_for(path)
    created = client.post("/api/reminders", json=reminder_payload())
    assert created.status_code == 201
    data = created.json()
    assert data["status"] == "proposed"
    assert data["due_at"].startswith("2030-02-02T17:30:00")
    assert UUID(data["reminder_id"])

    restarted = client_for(path)
    assert restarted.get("/api/reminders").json()[0]["reminder_id"] == data["reminder_id"]
    assert restarted.get(f"/api/reminders/{data['reminder_id']}").json()["title"] == "Finish project presentation"


def test_explicit_reminder_request_can_create_active_record(tmp_path):
    response = client_for(tmp_path / "local.sqlite3").post(
        "/api/reminders", json=reminder_payload(explicitly_confirmed=True)
    )
    assert response.status_code == 201
    assert response.json()["status"] == "active"


def test_reminder_confirm_edit_complete_and_delete(tmp_path):
    client = client_for(tmp_path / "local.sqlite3")
    proposed = client.post("/api/reminders", json=reminder_payload(due_time=None)).json()
    rid = proposed["reminder_id"]
    assert client.post(f"/api/reminders/{rid}/confirm").json()["status"] == "active"
    edited = client.patch(f"/api/reminders/{rid}", json={"title": "Updated title", "due_date": "2030-02-03"})
    assert edited.status_code == 200
    assert edited.json()["title"] == "Updated title"
    assert edited.json()["due_date"] == "2030-02-03"
    completed = client.post(f"/api/reminders/{rid}/complete").json()
    assert completed["status"] == "completed"
    assert completed["completed_at"]
    assert client.delete(f"/api/reminders/{rid}").status_code == 204
    assert client.get(f"/api/reminders/{rid}").status_code == 404


def test_reminder_timezone_validation_and_day_without_time(tmp_path):
    client = client_for(tmp_path / "local.sqlite3")
    response = client.post("/api/reminders", json=reminder_payload(timezone="Not/AZone"))
    assert response.status_code == 422
    no_time = client.post("/api/reminders", json=reminder_payload(due_time=None))
    assert no_time.status_code == 201
    assert no_time.json()["due_at"] is None


def test_relative_reminder_date_uses_the_supplied_local_timezone(tmp_path):
    fixed = datetime(2030, 2, 2, 8, 30, tzinfo=timezone.utc)
    assert str(resolve_due_date("tomorrow", "America/Los_Angeles", now=fixed)) == "2030-02-03"
    response = client_for(tmp_path / "local.sqlite3").post(
        "/api/reminders", json=reminder_payload(due_date="tomorrow", due_time=None)
    )
    assert response.status_code == 201
    expected = (datetime.now(ZoneInfo("America/Los_Angeles")).date()).isoformat()
    # The stored date must be a valid local resolution; its value may advance at midnight.
    assert response.json()["due_date"] >= expected


def test_reminder_creation_does_not_create_tasks(tmp_path):
    runtime = TaskRuntime(conversation_store=SQLiteConversationStore(tmp_path / "local.sqlite3"))
    client = TestClient(create_app(runtime))
    response = client.post("/api/reminders", json=reminder_payload())
    assert response.status_code == 201
    assert runtime.manager.repository._tasks == {}


def test_reminder_state_and_bad_id_errors_are_safe(tmp_path):
    client = client_for(tmp_path / "local.sqlite3")
    proposed = client.post("/api/reminders", json=reminder_payload()).json()
    rid = proposed["reminder_id"]
    assert client.post(f"/api/reminders/{rid}/complete").status_code == 409
    assert client.get("/api/reminders/not-a-uuid").status_code == 422
    assert client.get("/api/reminders/00000000-0000-4000-8000-000000000000").status_code == 404
