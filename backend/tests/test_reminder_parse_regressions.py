from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.api.runtime import TaskRuntime
from backend.app.conversation.store import SQLiteConversationStore
from backend.app.main import create_app


class NoModel:
    def generate_text(self, **kwargs):
        raise AssertionError("explicit reminder conversation must not call the model")


def _client(tmp_path):
    runtime = TaskRuntime(
        provider=NoModel(),
        conversation_store=SQLiteConversationStore(tmp_path / "reminder-regressions.sqlite3"),
        user_timezone="Asia/Kolkata",
    )
    client = TestClient(create_app(runtime))
    session_id = client.post("/api/conversations", json={}).json()["session_id"]
    return client, session_id


def _send(client, session_id, message):
    return client.post(
        f"/api/conversations/{session_id}/messages",
        json={"message": message, "request_id": str(uuid4()), "timezone": "Asia/Kolkata"},
    )


@pytest.mark.parametrize("clock", ["46 PM", "13 PM", "8:75 PM", "8:99 AM"])
def test_malformed_meridian_times_do_not_create_or_default_a_reminder(tmp_path, clock):
    client, session_id = _client(tmp_path)
    response = _send(client, session_id, f"Remind me about my OOP record tomorrow at {clock}.")

    assert response.status_code == 200
    assert "valid time" in response.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []


def test_title_clarification_parses_clock_and_rejects_invalid_replacement(tmp_path):
    client, session_id = _client(tmp_path)

    initial = _send(client, session_id, "Remind me tomorrow at 8:45 PM.")
    assert "what should i remind you about" in initial.json()["reply"].lower()

    malformed = _send(client, session_id, "46 PM of reminding me about my OOP record")
    assert "valid time" in malformed.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []

    corrected = _send(client, session_id, "8:45 p.m.")
    assert "not active until you confirm" in corrected.json()["reply"].lower()
    reminder = client.get("/api/reminders").json()[0]
    assert reminder["title"] == "my OOP record"
    assert reminder["due_time"] == "20:45:00"
    assert reminder["timezone"] == "Asia/Kolkata"
    assert reminder["status"] == "proposed"


def test_title_and_time_in_same_clarification_reply_are_split(tmp_path):
    client, session_id = _client(tmp_path)
    _send(client, session_id, "Remind me tomorrow.")

    response = _send(client, session_id, "My OOP record at 8:44 p.m.")
    assert "not active until you confirm" in response.json()["reply"].lower()
    reminder = client.get("/api/reminders").json()[0]
    assert reminder["title"] == "My OOP record"
    assert reminder["due_time"] == "20:44:00"
    assert reminder["status"] == "proposed"


def test_today_followup_preserves_previously_parsed_time_and_title(tmp_path):
    client, session_id = _client(tmp_path)
    initial = _send(client, session_id, "Set me a reminder named record at 8:44 p.m.")
    assert "when would you like" in initial.json()["reply"].lower()

    response = _send(client, session_id, "Today")
    assert "not active until you confirm" in response.json()["reply"].lower()
    reminder = client.get("/api/reminders").json()[0]
    assert reminder["title"] == "record"
    assert reminder["due_time"] == "20:44:00"
    assert reminder["timezone"] == "Asia/Kolkata"
    assert reminder["status"] == "proposed"


def test_malformed_time_containing_remind_phrase_does_not_contaminate_title(tmp_path):
    client, session_id = _client(tmp_path)
    response = _send(
        client,
        session_id,
        "Set me a reminder at 46 PM of reminding me about my OOP record tomorrow.",
    )
    assert "valid time" in response.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []

    corrected = _send(client, session_id, "8:45 PM")
    assert "not active until you confirm" in corrected.json()["reply"].lower()
    reminder = client.get("/api/reminders").json()[0]
    assert reminder["title"] == "my OOP record"
    assert reminder["due_time"] == "20:45:00"
    assert reminder["status"] == "proposed"
