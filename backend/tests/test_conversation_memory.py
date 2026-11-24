from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.api.runtime import TaskRuntime
from backend.app.conversation.store import ConversationStoreError, SQLiteConversationStore
from backend.app.llm.provider import LLMProviderError
from backend.app.main import create_app


class CapturingProvider:
    provider_name = "fake"

    def __init__(self, replies=None):
        self.replies = list(replies or ["A synthetic response."])
        self.prompts = []

    def generate_text(self, *, system, prompt, **kwargs):
        self.prompts.append(json.loads(prompt))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def client_for(tmp_path, provider=None):
    runtime = TaskRuntime(
        provider=provider or CapturingProvider(),
        conversation_store=SQLiteConversationStore(tmp_path / "conversations.sqlite3"),
    )
    return TestClient(create_app(runtime)), runtime


def create_session(client):
    response = client.post("/api/conversations")
    assert response.status_code == 201
    return response.json()["session_id"]


def send(client, session_id, message, request_id=None):
    return client.post(
        f"/api/conversations/{session_id}/messages",
        json={"message": message, **({"request_id": request_id} if request_id else {})},
    )


def test_session_creation_and_exchange_persist_user_and_assistant_messages(tmp_path):
    client, _ = client_for(tmp_path)
    session_id = create_session(client)
    response = send(client, session_id, "Hello")
    assert response.status_code == 200
    assert response.json()["reply"] == "A synthetic response."
    history = client.get(f"/api/conversations/{session_id}").json()
    assert [message["role"] for message in history["messages"]] == ["user", "assistant"]
    assert [message["sequence"] for message in history["messages"]] == [1, 2]
    assert history["messages"][0]["content"] == "Hello"
    assert history["messages"][1]["content"] == "A synthetic response."
    assert client.get("/api/conversations").json()[0]["message_count"] == 2


def test_session_and_history_restore_with_new_runtime_instance(tmp_path):
    db = tmp_path / "conversations.sqlite3"
    client, _ = client_for(tmp_path, CapturingProvider(["Remembered reply."]))
    session_id = create_session(client)
    send(client, session_id, "Keep this locally")

    restarted = TestClient(create_app(TaskRuntime(
        provider=CapturingProvider(), conversation_store=SQLiteConversationStore(db)
    )))
    assert restarted.get(f"/api/conversations/{session_id}").json()["messages"][0]["content"] == "Keep this locally"


def test_next_message_uses_bounded_persisted_context(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVERSATION_MAX_TURNS", "1")
    monkeypatch.setenv("CONVERSATION_MAX_CONTEXT_CHARS", "4000")
    provider = CapturingProvider(["First answer.", "Second answer.", "Third answer."])
    client, _ = client_for(tmp_path, provider)
    session_id = create_session(client)
    send(client, session_id, "old " + "x" * 2500)
    send(client, session_id, "recent question")
    send(client, session_id, "follow up")
    context = provider.prompts[-1]["history"]
    assert [turn["content"] for turn in context] == ["recent question", "Second answer."]
    assert provider.prompts[-1]["current_user_message"] == "follow up"


def test_sessions_are_isolated_and_delete_cascades_messages(tmp_path):
    client, _ = client_for(tmp_path)
    first, second = create_session(client), create_session(client)
    send(client, first, "Only in first")
    assert client.get(f"/api/conversations/{second}").json()["messages"] == []
    deleted = client.delete(f"/api/conversations/{first}")
    assert deleted.status_code == 204
    assert client.get(f"/api/conversations/{first}").status_code == 404
    assert client.get(f"/api/conversations/{second}").status_code == 200
    assert client.get("/api/conversations").json()[0]["session_id"] == second


def test_invalid_session_ids_and_unknown_sessions_are_rejected(tmp_path):
    client, _ = client_for(tmp_path)
    assert client.get("/api/conversations/not-a-uuid").status_code == 422
    assert client.post("/api/conversations/not-a-uuid/messages", json={"message": "Hi"}).status_code == 422
    missing = str(uuid4())
    assert client.get(f"/api/conversations/{missing}").status_code == 404
    assert send(client, missing, "Hi").status_code == 404
    assert client.delete(f"/api/conversations/{missing}").status_code == 404


def test_provider_failure_does_not_persist_a_partial_exchange(tmp_path):
    client, _ = client_for(tmp_path, CapturingProvider([LLMProviderError("private failure")]))
    session_id = create_session(client)
    response = send(client, session_id, "Do not lose this on error")
    assert response.status_code == 503
    assert "private failure" not in response.text
    assert client.get(f"/api/conversations/{session_id}").json()["messages"] == []


@pytest.mark.parametrize("secret", ["password: example-secret", "API key is sk-abcdefghijklmnopqrstuvwxyz123456", "Bearer abcdefghijklmnop"])
def test_obvious_secrets_are_not_sent_to_provider_or_persisted(tmp_path, secret):
    provider = CapturingProvider()
    client, _ = client_for(tmp_path, provider)
    session_id = create_session(client)
    response = send(client, session_id, f"Please remember {secret}")
    assert response.status_code == 422
    assert provider.prompts == []
    assert client.get(f"/api/conversations/{session_id}").json()["messages"] == []


def test_credential_like_model_output_is_not_persisted(tmp_path):
    provider = CapturingProvider(["Your password is do-not-save-this"])
    client, _ = client_for(tmp_path, provider)
    session_id = create_session(client)
    response = send(client, session_id, "Tell me a password")
    assert response.status_code == 503
    assert client.get(f"/api/conversations/{session_id}").json()["messages"] == []


def test_persistence_failure_is_reported_without_claiming_saved_message(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file blocks db directory")
    runtime = TaskRuntime(
        provider=CapturingProvider(), conversation_store=SQLiteConversationStore(blocker / "db.sqlite3")
    )
    response = TestClient(create_app(runtime)).post("/api/conversations")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "conversation_storage_error"
    assert "not-a-directory" not in response.text


def test_exchange_persistence_failure_returns_error_and_stores_no_partial_pair(tmp_path, monkeypatch):
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    client, _ = client_for(tmp_path, CapturingProvider())
    session_id = create_session(client)

    def fail_append(*args, **kwargs):
        raise ConversationStoreError("private persistence details")

    monkeypatch.setattr(store, "append_exchange", fail_append)
    runtime = TaskRuntime(provider=CapturingProvider(), conversation_store=store)
    client = TestClient(create_app(runtime))
    response = send(client, session_id, "Persist me")
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "conversation_storage_error"
    assert "private persistence details" not in response.text
    assert client.get(f"/api/conversations/{session_id}").json()["messages"] == []


def test_retry_with_same_request_id_returns_saved_exchange_without_duplicate_messages(tmp_path):
    provider = CapturingProvider(["One generated answer."])
    client, _ = client_for(tmp_path, provider)
    session_id, request_id = create_session(client), str(uuid4())
    first = send(client, session_id, "Retry safely", request_id)
    retry = send(client, session_id, "Retry safely", request_id)
    assert first.json() == retry.json()
    assert len(provider.prompts) == 1
    assert len(client.get(f"/api/conversations/{session_id}").json()["messages"]) == 2
    assert send(client, session_id, "Different text", request_id).status_code == 409


def test_legacy_conversation_endpoint_remains_stateless_and_compatible(tmp_path):
    provider = CapturingProvider(["Legacy reply."])
    client, runtime = client_for(tmp_path, provider)
    response = client.post("/api/conversation", json={
        "message": "Continue",
        "history": [{"role": "user", "content": "Earlier"}],
    })
    assert response.status_code == 200
    assert response.json() == {"reply": "Legacy reply."}
    assert provider.prompts[0]["history"] == [{"role": "user", "content": "Earlier"}]
    assert runtime.manager.repository._tasks == {}


def test_invalid_conversation_body_stays_422(tmp_path):
    client, _ = client_for(tmp_path)
    session_id = create_session(client)
    assert send(client, session_id, "  ").status_code == 422
    assert client.post(f"/api/conversations/{session_id}/messages", json={}).status_code == 422
