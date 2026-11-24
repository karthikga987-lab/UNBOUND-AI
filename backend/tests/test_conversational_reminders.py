from __future__ import annotations

from datetime import date, time, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo
import pytest

from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.app.api.runtime import TaskRuntime
from backend.app.assistant.reminder_intent import _parse_time, parse_reminder_command
from backend.app.conversation.store import SQLiteConversationStore
from backend.app.main import create_app
from backend.app.schemas.contracts import ToolDefinition, ToolResult


class MustNotCallProvider:
    def generate_text(self, **kwargs):
        raise AssertionError("explicit reminder commands should not need the LLM")


class ConversationProvider:
    def generate_text(self, **kwargs):
        return "I can help with that."


class SearchInput(BaseModel):
    query: str


def setup(tmp_path, provider=None):
    runtime = TaskRuntime(
        provider=provider or MustNotCallProvider(),
        conversation_store=SQLiteConversationStore(tmp_path / "conversation.sqlite3"),
        user_timezone="Asia/Kolkata",
    )
    external_calls = []

    def external_tool(arguments, tool_call):
        external_calls.append(tool_call.tool_name)
        return ToolResult(success=True, data={"items": []})

    runtime.register_tool(
        ToolDefinition(
            tool_name="document.search",
            description="Search documents",
            input_schema=SearchInput.model_json_schema(),
        ),
        SearchInput,
        external_tool,
    )
    client = TestClient(create_app(runtime))
    session_id = client.post("/api/conversations", json={}).json()["session_id"]
    return client, runtime, session_id, external_calls


def test_notification_agent_status_is_shared_and_cached(tmp_path, monkeypatch):
    import backend.app.api.runtime as runtime_module

    checks = []
    monkeypatch.setattr(runtime_module, "notification_agent_status", lambda: checks.append(True) or {"installed": True, "running": True})
    runtime = TaskRuntime(conversation_store=SQLiteConversationStore(tmp_path / "status-cache.sqlite3"))
    assert runtime.get_notification_agent_status() == {"installed": True, "running": True}
    assert runtime.get_notification_agent_status() == {"installed": True, "running": True}
    assert checks == [True]


def send(client, session_id, message):
    return client.post(
        f"/api/conversations/{session_id}/messages",
        json={"message": message, "request_id": str(uuid4()), "timezone": "Asia/Kolkata"},
    )


def test_normal_conversation_proposes_then_confirms_and_persists_reminder(tmp_path):
    client, runtime, session_id, external_calls = setup(tmp_path)

    proposed = send(client, session_id, "Hey UNBOUND, remind me tomorrow at 8 AM to submit my UHV assignment.")
    assert proposed.status_code == 200
    assert "no task tools registered" not in proposed.json()["reply"].lower()
    assert "not active until you confirm" in proposed.json()["reply"]
    records = client.get("/api/reminders").json()
    assert len(records) == 1
    assert records[0]["title"] == "submit my UHV assignment"
    assert records[0]["status"] == "proposed"
    assert records[0]["timezone"] == "Asia/Kolkata"
    reminder_id = records[0]["reminder_id"]

    confirmed = send(client, session_id, "yes")
    assert confirmed.status_code == 200
    assert "saved and active in unbound" in confirmed.json()["reply"].lower()
    assert client.get(f"/api/reminders/{reminder_id}").json()["status"] == "active"
    assert external_calls == []
    assert runtime.manager.repository._tasks == {}


def test_stateless_conversation_endpoint_also_routes_explicit_reminder(tmp_path):
    client, _, _, external_calls = setup(tmp_path)
    response = client.post("/api/conversation", json={
        "message": "Remind me tomorrow at 8 AM to submit my assignment.",
        "timezone": "Asia/Kolkata",
    })
    assert response.status_code == 200
    assert "not active until you confirm" in response.json()["reply"]
    assert client.get("/api/reminders").json()[0]["status"] == "proposed"
    assert external_calls == []


def test_missing_or_ambiguous_time_asks_clarification_before_proposal(tmp_path):
    client, _, session_id, _ = setup(tmp_path)

    missing = send(client, session_id, "Remind me tomorrow to submit my assignment.")
    assert "specific time" in missing.json()["reply"].lower()
    assert "all-day reminder" in missing.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []

    answer = send(client, session_id, "8 AM")
    assert "not active until you confirm" in answer.json()["reply"]
    assert len(client.get("/api/reminders").json()) == 1


def test_remember_to_remind_phrase_is_a_reminder_not_personal_memory(tmp_path):
    client, _, session_id, _ = setup(tmp_path)
    response = send(client, session_id, "Remember to remind me to call my friend tomorrow.")
    assert "specific time" in response.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []

    proposal = send(client, session_id, "8 AM")
    assert "not active until you confirm" in proposal.json()["reply"]
    reminders = client.get("/api/reminders").json()
    assert len(reminders) == 1
    assert reminders[0]["title"] == "call my friend"
    assert reminders[0]["status"] == "proposed"

    ambiguous = send(client, session_id, "Remind me tomorrow at 8 to call Alex.")
    assert "am or pm" in ambiguous.json()["reply"].lower()
    assert len(client.get("/api/reminders").json()) == 1


def test_reminder_listing_completion_and_deletion_are_conversational(tmp_path):
    client, _, session_id, _ = setup(tmp_path)

    first = send(client, session_id, "Remind me tomorrow at 9 AM to submit my assignment.")
    second = send(client, session_id, "Remind me tomorrow at 10 AM to call my friend.")
    assert first.status_code == second.status_code == 200
    assert "saved and active" in send(client, session_id, "Confirm reminder for submit my assignment.").json()["reply"].lower()
    assert "saved and active" in send(client, session_id, "Confirm reminder for call my friend.").json()["reply"].lower()

    listed = send(client, session_id, "What reminders do I have?")
    assert "submit my assignment" in listed.json()["reply"]
    assert "call my friend" in listed.json()["reply"]

    assert "marked reminder completed" in send(
        client, session_id, "Mark my assignment reminder as completed."
    ).json()["reply"].lower()
    completed = next(item for item in client.get("/api/reminders").json() if item["title"] == "submit my assignment")
    assert completed["status"] == "completed"

    deleted = send(client, session_id, "Delete my call my friend reminder.")
    assert "deleted reminder" in deleted.json()["reply"].lower()
    assert all(item["title"] != "call my friend" for item in client.get("/api/reminders").json())


def test_memory_request_and_task_execution_remain_outside_reminder_handler(tmp_path):
    client, runtime, session_id, external_calls = setup(tmp_path, ConversationProvider())

    memory_response = send(client, session_id, "Remember that I prefer concise summaries.")
    assert memory_response.status_code == 200
    assert memory_response.json()["reply"] == "I can help with that."
    assert client.get("/api/reminders").json() == []

    information = send(client, session_id, "How do I create a reminder in Outlook?")
    assert information.status_code == 200
    assert information.json()["reply"] == "I can help with that."
    assert client.get("/api/reminders").json() == []

    task_response = send(client, session_id, "Send this email to the client now.")
    assert task_response.status_code == 200
    assert task_response.json()["reply"] == "I can help with that."
    assert runtime.manager.repository._tasks == {}
    assert external_calls == []


def test_date_without_title_and_time_triggers_focused_clarification(tmp_path):
    client, _, session_id, _ = setup(tmp_path)
    response = send(client, session_id, "Set a reminder for next Monday.")
    assert "what should i remind you about" in response.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []


def test_date_without_time_offers_specific_time_or_all_day_and_saves_choice(tmp_path):
    client, _, session_id, _ = setup(tmp_path)
    response = send(client, session_id, "Create a reminder for my DMS assignment tomorrow.")
    assert "specific time" in response.json()["reply"].lower()
    assert "all-day reminder" in response.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []

    proposal = send(client, session_id, "All day")
    assert "all day" in proposal.json()["reply"].lower()
    reminder = client.get("/api/reminders").json()[0]
    assert reminder["title"] == "my DMS assignment"
    assert reminder["due_time"] is None
    assert reminder["status"] == "proposed"


def test_exact_dms_laptop_request_routes_locally_and_never_claims_windows_install(tmp_path):
    client, runtime, session_id, external_calls = setup(tmp_path)
    initial = send(
        client,
        session_id,
        "I want you to create me a remainder for my dms assignment, set it up on my laptop.",
    )
    assert initial.status_code == 200
    assert "when would you like me to remind you" in initial.json()["reply"].lower()
    assert "microsoft to do" not in initial.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []
    assert runtime.manager.repository._tasks == {}
    assert external_calls == []

    proposal = send(client, session_id, "Tomorrow at 8 AM")
    assert "saved a reminder proposal in unbound" in proposal.json()["reply"].lower()
    reminder = client.get("/api/reminders").json()[0]
    assert reminder["title"] == "my dms assignment"
    assert reminder["status"] == "proposed"

    laptop_follow_up = send(client, session_id, "Set it up on my laptop.")
    assert "saved as a proposal in unbound" in laptop_follow_up.json()["reply"].lower()
    assert "not active until you confirm" in laptop_follow_up.json()["reply"].lower()
    assert client.get(f"/api/reminders/{reminder['reminder_id']}").json()["status"] == "proposed"

    confirmation = send(client, session_id, "Yes")
    assert "saved and active in unbound" in confirmation.json()["reply"].lower()
    assert "notification agent" in confirmation.json()["reply"].lower()
    assert client.get(f"/api/reminders/{reminder['reminder_id']}").json()["status"] == "active"
    assert external_calls == []


@pytest.mark.parametrize(
    ("status", "proposal_text", "active_text"),
    [
        ({"installed": True, "running": True}, "once you confirm", "while the browser and fastapi are closed"),
        ({"installed": True, "running": False}, "once you confirm", "installed but stopped"),
        ({"installed": False, "running": False}, "require the local unbound notification agent to be installed", "require the local unbound notification agent to be installed"),
        ({"installed": None, "running": None}, "can't verify the notification agent status", "can't verify the notification agent status"),
    ],
)
def test_reminder_responses_describe_agent_status_without_overstating_proposals(
    tmp_path, status, proposal_text, active_text
):
    client, runtime, session_id, _ = setup(tmp_path)
    runtime.get_notification_agent_status = lambda: dict(status)
    proposal = send(client, session_id, "Remind me tomorrow at 8 AM to finish the assignment.").json()["reply"].lower()
    assert "not active until you confirm" in proposal
    assert proposal_text in proposal
    assert client.get("/api/reminders").json()[0]["status"] == "proposed"

    active = send(client, session_id, "Yes").json()["reply"].lower()
    assert "saved and active" in active
    assert active_text in active
    assert client.get("/api/reminders").json()[0]["status"] == "active"


def test_common_reminder_spellings_and_forget_phrases_are_understood(tmp_path):
    client, _, session_id, _ = setup(tmp_path)
    for prompt in (
        "Remind me about my DMS assignment.",
        "Create me a reminder for my DMS assignment.",
        "Create a reminder for my DMS assignment.",
        "Create a remainder for my DMS assignment.",
        "I need to remember to submit my assignment.",
        "Don't let me forget my DMS assignment.",
    ):
        result = send(client, session_id, prompt)
        assert result.status_code == 200
        assert "when would you like me to remind you" in result.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []


def test_schedule_reminder_for_date_without_subject_asks_for_subject(tmp_path):
    client, _, session_id, _ = setup(tmp_path)
    response = send(client, session_id, "Schedule a reminder for tomorrow.")
    assert "what should i remind you about" in response.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []


def test_read_and_edit_preserve_reminder_status_and_unrelated_chat_does_not_change_it(tmp_path):
    client, _, session_id, _ = setup(tmp_path, ConversationProvider())
    proposal = client.post("/api/reminders", json={
        "title": "DMS assignment",
        "due_date": "tomorrow",
        "due_time": "08:00:00",
        "timezone": "Asia/Kolkata",
    }).json()
    reminder_id = proposal["reminder_id"]
    assert proposal["status"] == "proposed"

    assert client.get(f"/api/reminders/{reminder_id}").json()["status"] == "proposed"
    edited = client.patch(f"/api/reminders/{reminder_id}", json={"title": "DMS final assignment"})
    assert edited.json()["status"] == "proposed"
    confirmed = client.post(f"/api/reminders/{reminder_id}/confirm").json()
    assert confirmed["status"] == "active"

    ordinary = send(client, session_id, "Explain what a DMS is.")
    assert ordinary.status_code == 200
    after_chat = client.get(f"/api/reminders/{reminder_id}").json()
    assert after_chat["status"] == "active"
    assert after_chat["title"] == "DMS final assignment"

    restarted_runtime = TaskRuntime(
        provider=ConversationProvider(),
        conversation_store=SQLiteConversationStore(tmp_path / "conversation.sqlite3"),
        user_timezone="Asia/Kolkata",
    )
    restarted_client = TestClient(create_app(restarted_runtime))
    assert restarted_client.get(f"/api/reminders/{reminder_id}").json()["status"] == "active"


def test_invalid_timezone_is_rejected_in_conversation_request(tmp_path):
    client, _, session_id, _ = setup(tmp_path)
    response = client.post(
        f"/api/conversations/{session_id}/messages",
        json={"message": "Remind me tomorrow at 8 AM to call Alex.", "timezone": "Not/AZone"},
    )
    assert response.status_code == 422
    assert client.get("/api/reminders").json() == []


def test_explicit_date_and_meridiem_parse_without_losing_the_schedule():
    command = parse_reminder_command(
        "Set me a reminder at 8:32 p.m. of 24th November 2026.", "Asia/Kolkata"
    )
    assert command is not None
    assert command.due_date == date(2026, 11, 24)
    assert command.due_time == time(20, 32)
    assert command.ambiguous_time is False
    assert command.title is None


def test_explicit_schedule_without_subject_only_asks_for_the_subject(tmp_path):
    client, _, session_id, _ = setup(tmp_path)
    response = send(client, session_id, "Set me a reminder at 8:32 p.m. of 24th November 2026.")
    assert "what should i remind you about" in response.json()["reply"].lower()
    assert "am or pm" not in response.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("at 8:32 p.m.", time(20, 32)),
        ("at 8:32 PM", time(20, 32)),
        ("at 8:32 post meridian", time(20, 32)),
        ("at 8:32 ante meridiem", time(8, 32)),
        ("at 20:33", time(20, 33)),
    ],
)
def test_twelve_and_twenty_four_hour_time_forms(text, expected):
    parsed, ambiguous = _parse_time(text)
    assert parsed == expected
    assert ambiguous is False


def test_bare_meridiem_followup_resolves_existing_ambiguous_time(tmp_path):
    client, _, session_id, _ = setup(tmp_path)
    first = send(client, session_id, "Set me a reminder named record at 8:44 on 25th November 2026.")
    assert "am or pm" in first.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []

    resolved = send(client, session_id, "pm")
    assert "not active until you confirm" in resolved.json()["reply"].lower()
    reminder = client.get("/api/reminders").json()[0]
    assert reminder["title"] == "record"
    assert reminder["due_date"] == "2026-11-25"
    assert reminder["due_time"].startswith("20:44")
    assert reminder["timezone"] == "Asia/Kolkata"
    assert reminder["status"] == "proposed"

    confirmed = send(client, session_id, "yes")
    assert "saved and active" in confirmed.json()["reply"].lower()
    assert client.get(f"/api/reminders/{reminder['reminder_id']}").json()["status"] == "active"


def test_today_followup_preserves_previously_supplied_title_and_time(tmp_path):
    client, _, session_id, _ = setup(tmp_path)
    first = send(client, session_id, "Set me a reminder named record at 8:44 p.m.")
    assert "when would you like me to remind you" in first.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []

    proposal = send(client, session_id, "Today")
    assert "not active until you confirm" in proposal.json()["reply"].lower()
    reminder = client.get("/api/reminders").json()[0]
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    assert reminder["title"] == "record"
    assert reminder["due_date"] == today
    assert reminder["due_time"].startswith("20:44")
    assert reminder["timezone"] == "Asia/Kolkata"
    assert reminder["status"] == "proposed"


@pytest.mark.parametrize(
    "initial,followup,expected_date,expected_time",
    [
        ("Set me a reminder named record for 24-11-2026.", "20:33", "2026-11-24", "20:33"),
        ("Set me a reminder named record", "20:33 24-11-2026", "2026-11-24", "20:33"),
    ],
)
def test_compact_date_and_24_hour_followups_are_retained(
    tmp_path, initial, followup, expected_date, expected_time
):
    client, _, session_id, _ = setup(tmp_path)
    send(client, session_id, initial)
    proposal = send(client, session_id, followup)
    assert "not active until you confirm" in proposal.json()["reply"].lower()
    reminder = client.get("/api/reminders").json()[0]
    assert reminder["title"] == "record"
    assert reminder["due_date"] == expected_date
    assert reminder["due_time"].startswith(expected_time)
    assert reminder["timezone"] == "Asia/Kolkata"
    assert reminder["status"] == "proposed"


def test_invalid_explicit_date_does_not_create_or_adjust_a_reminder(tmp_path):
    client, _, session_id, _ = setup(tmp_path)
    response = send(client, session_id, "Set me a reminder named record at 8:32 p.m. on 31-02-2026.")
    assert "when would you like me to remind you" in response.json()["reply"].lower()
    assert client.get("/api/reminders").json() == []
