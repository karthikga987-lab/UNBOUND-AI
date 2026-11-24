from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.api.runtime import TaskRuntime
from backend.app.conversation.store import SQLiteConversationStore
from backend.app.main import create_app
from backend.app.memory.models import ExtractedMemoryCandidate, MemoryCategory, MemoryExtractionOutput
from backend.app.memory.store import SQLiteMemoryStore


class MemoryFakeProvider:
    provider_name = "fake"

    def __init__(self, candidates=None):
        self.candidates = list(candidates or [])
        self.prompts = []
        self.extraction_prompts = []
        self.extraction_error = None

    def generate_text(self, *, system, prompt, **kwargs):
        self.prompts.append(json.loads(prompt))
        return "A safe answer."

    def generate_json(self, *, system, prompt, response_model, **kwargs):
        self.extraction_prompts.append(json.loads(prompt))
        if self.extraction_error:
            raise self.extraction_error
        return response_model(candidates=self.candidates)


def setup_client(tmp_path, provider=None):
    conversation_store = SQLiteConversationStore(tmp_path / "unbound.sqlite3")
    runtime = TaskRuntime(
        provider=provider or MemoryFakeProvider(),
        conversation_store=conversation_store,
        memory_store=SQLiteMemoryStore(conversation_store),
    )
    return TestClient(create_app(runtime)), runtime


def new_session(client):
    response = client.post("/api/conversations")
    assert response.status_code == 201
    return response.json()["session_id"]


def add_exchange(client, session_id, message):
    response = client.post(f"/api/conversations/{session_id}/messages", json={"message": message})
    assert response.status_code == 200
    return response.json()


def propose(client, **overrides):
    payload = {
        "content": "Prefers concise technical explanations.",
        "category": "preference",
        **overrides,
    }
    return client.post("/api/memories/proposals", json=payload)


def test_candidate_requires_approval_and_approved_memory_retrieves_in_another_session(tmp_path):
    client, runtime = setup_client(tmp_path)
    session_a = new_session(client)
    exchange = add_exchange(client, session_a, "Please remember that I prefer concise technical explanations.")
    source_id = exchange["user_message"]["message_id"]

    candidate = propose(
        client,
        source_session_id=session_a,
        source_message_id=source_id,
    )
    assert candidate.status_code == 201
    pending = candidate.json()
    assert pending["status"] == "pending_approval"
    assert pending["user_confirmed"] is False
    assert pending["origin"] == "user_provided"
    assert "prefer concise" in pending["source_excerpt"]
    assert runtime.memory_store.retrieve_relevant("concise technical explanations") == []

    approved = client.post(f"/api/memories/{pending['memory_id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["user_confirmed"] is True
    session_b = new_session(client)
    add_exchange(client, session_b, "Can you give concise technical explanations?")
    assert runtime.provider.prompts[-1]["relevant_approved_memories"][0]["content"] == pending["content"]


def test_explicit_remember_request_approves_only_the_submitted_memory(tmp_path):
    client, runtime = setup_client(tmp_path)
    response = propose(client, explicitly_requested=True, category="project", content="Working on UNBOUND AI.")
    assert response.status_code == 201
    assert response.json()["status"] == "approved"
    assert response.json()["user_confirmed"] is True
    assert runtime.memory_store.retrieve_relevant("UNBOUND AI project")[0]["content"] == "Working on UNBOUND AI."


def test_model_extraction_creates_only_pending_candidates_with_user_source(tmp_path):
    provider = MemoryFakeProvider()
    client, _ = setup_client(tmp_path, provider)
    session_id = new_session(client)
    exchange = add_exchange(client, session_id, "I am working on the Atlas migration project this quarter.")
    message_id = exchange["user_message"]["message_id"]
    provider.candidates = [
        ExtractedMemoryCandidate(
            content="User is working on the Atlas migration project.",
            category=MemoryCategory.PROJECT,
            source_message_id=message_id,
        ),
        ExtractedMemoryCandidate(
            content="  USER is working on the Atlas migration project. ",
            category=MemoryCategory.PROJECT,
            source_message_id=message_id,
        ),
    ]

    extracted = client.post("/api/memories/extract", json={"session_id": session_id})
    assert extracted.status_code == 200
    assert len(extracted.json()) == 1
    memory = extracted.json()[0]
    assert memory["status"] == "pending_approval"
    assert memory["origin"] == "model_extracted"
    assert memory["user_confirmed"] is False
    assert memory["source_message_id"] == message_id
    assert "Atlas migration" in memory["source_excerpt"]
    assert runtime_memory(client, memory["memory_id"])["status"] == "pending_approval"


def runtime_memory(client, memory_id):
    response = client.get(f"/api/memories/{memory_id}")
    assert response.status_code == 200
    return response.json()


def test_model_candidate_with_invalid_or_assistant_source_is_not_saved(tmp_path):
    provider = MemoryFakeProvider()
    client, _ = setup_client(tmp_path, provider)
    session_id = new_session(client)
    exchange = add_exchange(client, session_id, "I prefer dark mode.")
    assistant_id = exchange["assistant_message"]["message_id"]
    provider.candidates = [ExtractedMemoryCandidate(
        content="User prefers dark mode.", category=MemoryCategory.PREFERENCE, source_message_id=assistant_id
    )]
    result = client.post("/api/memories/extract", json={"session_id": session_id})
    assert result.status_code == 200
    assert result.json() == []
    assert client.get("/api/memories").json() == []


def test_rejection_is_audited_and_old_history_cannot_recreate_deleted_candidate(tmp_path):
    provider = MemoryFakeProvider()
    client, _ = setup_client(tmp_path, provider)
    session_id = new_session(client)
    exchange = add_exchange(client, session_id, "I prefer dark mode for coding.")
    user_message_id = exchange["user_message"]["message_id"]
    provider.candidates = [ExtractedMemoryCandidate(
        content="Prefers dark mode for coding.", category=MemoryCategory.PREFERENCE, source_message_id=user_message_id
    )]
    first = client.post("/api/memories/extract", json={"session_id": session_id}).json()[0]
    rejected = client.post(f"/api/memories/{first['memory_id']}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert client.get(f"/api/memories/{first['memory_id']}").status_code == 404
    assert client.post(f"/api/memories/{first['memory_id']}/approve").status_code == 404
    again = client.post("/api/memories/extract", json={"session_id": session_id})
    assert again.status_code == 200
    assert again.json() == []
    assert client.get("/api/memories").json() == []


def test_duplicate_memory_proposals_are_normalized_and_not_duplicated(tmp_path):
    client, _ = setup_client(tmp_path)
    first = propose(client, content="Prefers concise technical explanations.")
    duplicate = propose(client, content="  PREFERS   concise technical explanations.  ")
    assert first.status_code == duplicate.status_code == 201
    assert first.json()["memory_id"] == duplicate.json()["memory_id"]
    assert len(client.get("/api/memories").json()) == 1


def test_concurrent_duplicate_proposals_create_one_memory_row(tmp_path):
    client, runtime = setup_client(tmp_path)
    content = "Prefers concise technical explanations."

    def create():
        return runtime.memory_store.create_candidate(
            content=content,
            category="preference",
            origin="user_provided",
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: create(), range(6)))
    assert len({result["memory_id"] for result in results}) == 1
    assert len(client.get("/api/memories").json()) == 1


def test_memory_correction_updates_text_and_does_not_reuse_stale_source_provenance(tmp_path):
    client, _ = setup_client(tmp_path)
    session_id = new_session(client)
    exchange = add_exchange(client, session_id, "I prefer concise answers.")
    candidate = propose(
        client,
        content="Prefers concise answers.",
        source_session_id=session_id,
        source_message_id=exchange["user_message"]["message_id"],
    ).json()
    approved = client.post(f"/api/memories/{candidate['memory_id']}/approve")
    updated = client.patch(
        f"/api/memories/{candidate['memory_id']}",
        json={"content": "Prefers detailed answers."},
    )
    assert updated.status_code == 200
    assert updated.json()["content"] == "Prefers detailed answers."
    assert updated.json()["origin"] == "user_edited"
    assert updated.json()["source_session_id"] is None
    assert updated.json()["source_message_id"] is None
    assert client.post(f"/api/memories/{candidate['memory_id']}/approve").status_code == 409
    assert approved.status_code == 200


def test_delete_removes_memory_from_list_and_retrieval_and_blocks_old_candidate(tmp_path):
    provider = MemoryFakeProvider()
    client, runtime = setup_client(tmp_path, provider)
    session_id = new_session(client)
    exchange = add_exchange(client, session_id, "My current project is Atlas migration.")
    user_message_id = exchange["user_message"]["message_id"]
    candidate = propose(
        client,
        explicitly_requested=True,
        content="Current project is Atlas migration.",
        source_session_id=session_id,
        source_message_id=user_message_id,
    ).json()
    assert runtime.memory_store.retrieve_relevant("Atlas migration project")
    assert client.delete(f"/api/memories/{candidate['memory_id']}").status_code == 204
    assert runtime.memory_store.retrieve_relevant("Atlas migration project") == []
    provider.candidates = [ExtractedMemoryCandidate(
        content="Working on the Atlas migration project.", category=MemoryCategory.PROJECT, source_message_id=user_message_id
    )]
    restarted = TaskRuntime(
        provider=provider,
        conversation_store=SQLiteConversationStore(runtime.conversation_store.database_path),
    )
    restarted_client = TestClient(create_app(restarted))
    assert restarted_client.post("/api/memories/extract", json={"session_id": session_id}).json() == []
    assert restarted_client.get("/api/memories").json() == []


def test_memory_retrieval_can_be_disabled_and_setting_persists_across_runtime(tmp_path):
    client, runtime = setup_client(tmp_path)
    approved = propose(client, explicitly_requested=True).json()
    disabled = client.patch("/api/memories/settings", json={"retrieval_enabled": False})
    assert disabled.status_code == 200
    session_id = new_session(client)
    add_exchange(client, session_id, "Can you tailor a concise technical answer?")
    assert runtime.provider.prompts[-1]["relevant_approved_memories"] == []

    database = runtime.conversation_store.database_path
    new_runtime = TaskRuntime(
        provider=MemoryFakeProvider(), conversation_store=SQLiteConversationStore(database)
    )
    restored = TestClient(create_app(new_runtime))
    assert restored.get("/api/memories/settings").json()["retrieval_enabled"] is False
    assert restored.get(f"/api/memories/{approved['memory_id']}").status_code == 200
    assert restored.patch("/api/memories/settings", json={"retrieval_enabled": True}).status_code == 200
    assert new_runtime.memory_store.retrieve_relevant("concise technical explanations")


def test_memory_persists_across_new_runtime_and_session_isolation(tmp_path):
    client, runtime = setup_client(tmp_path)
    record = propose(client, explicitly_requested=True).json()
    db_path = runtime.conversation_store.database_path
    restarted_store = SQLiteConversationStore(db_path)
    restarted = SQLiteMemoryStore(restarted_store)
    assert restarted.get_memory(record["memory_id"])["content"] == record["content"]
    assert restarted.retrieve_relevant("concise technical explanations")


def test_invalid_source_duplicate_resolution_and_secret_content_fail_safely(tmp_path):
    client, _ = setup_client(tmp_path)
    source_session = new_session(client)
    other_session = new_session(client)
    exchange = add_exchange(client, source_session, "I enjoy hiking.")
    wrong_session = propose(
        client,
        source_session_id=other_session,
        source_message_id=exchange["user_message"]["message_id"],
    )
    assert wrong_session.status_code == 422
    secret = propose(client, content="password: sample-secret")
    assert secret.status_code == 422
    candidate = propose(client).json()
    assert client.post(f"/api/memories/{candidate['memory_id']}/approve").status_code == 200
    assert client.post(f"/api/memories/{candidate['memory_id']}/approve").status_code == 409
    assert client.post(f"/api/memories/{uuid4()}/approve").status_code == 404
    assert client.delete(f"/api/memories/{uuid4()}").status_code == 404


def test_model_extraction_provider_failure_creates_no_memory(tmp_path):
    provider = MemoryFakeProvider()
    client, _ = setup_client(tmp_path, provider)
    session_id = new_session(client)
    add_exchange(client, session_id, "My project is Atlas migration.")

    def fail_json(**kwargs):
        from backend.app.llm.provider import LLMProviderError
        raise LLMProviderError("private failure")

    provider.generate_json = fail_json
    response = client.post("/api/memories/extract", json={"session_id": session_id})
    assert response.status_code == 503
    assert "private failure" not in response.text
    assert client.get("/api/memories").json() == []


def test_invalid_memory_requests_and_unknown_source_session(tmp_path):
    client, _ = setup_client(tmp_path)
    assert client.post("/api/memories/proposals", json={}).status_code == 422
    assert client.post("/api/memories/extract", json={"session_id": str(uuid4())}).status_code == 404
    assert client.patch("/api/memories/settings", json={}).status_code == 422
    assert client.patch("/api/memories", json={}).status_code == 405


def test_irrelevant_approved_memory_is_not_added_to_conversation_context(tmp_path):
    client, runtime = setup_client(tmp_path)
    propose(client, explicitly_requested=True, content="Prefers concise technical explanations.")
    session_id = new_session(client)
    add_exchange(client, session_id, "What is the weather like today?")
    assert runtime.provider.prompts[-1]["relevant_approved_memories"] == []


def test_normal_conversation_does_not_automatically_create_memory(tmp_path):
    client, runtime = setup_client(tmp_path)
    session_id = new_session(client)
    add_exchange(client, session_id, "I am working on a long term project called Atlas.")
    assert client.get("/api/memories").json() == []
    assert runtime.manager.repository._tasks == {}


def test_memory_candidate_secret_is_not_saved(tmp_path):
    provider = MemoryFakeProvider()
    client, _ = setup_client(tmp_path, provider)
    session_id = new_session(client)
    exchange = add_exchange(client, session_id, "My preferred coding style is concise.")
    user_message_id = exchange["user_message"]["message_id"]
    provider.candidates = [
        ExtractedMemoryCandidate(
            content="password: dont-store-this",
            category=MemoryCategory.OTHER,
            source_message_id=user_message_id,
        )
    ]
    result = client.post("/api/memories/extract", json={"session_id": session_id})
    assert result.status_code == 200
    assert result.json() == []
    assert client.get("/api/memories").json() == []


def test_memory_storage_failure_returns_safe_error(tmp_path):
    blocker = tmp_path / "database-parent-is-a-file"
    blocker.write_text("not a directory")
    conversation_store = SQLiteConversationStore(blocker / "unbound.sqlite3")
    runtime = TaskRuntime(
        conversation_store=conversation_store,
        memory_store=SQLiteMemoryStore(conversation_store),
    )
    response = TestClient(create_app(runtime)).post(
        "/api/memories/proposals",
        json={"content": "Likes short summaries", "category": "preference"},
    )
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "memory_storage_error"
    assert str(blocker) not in response.text
