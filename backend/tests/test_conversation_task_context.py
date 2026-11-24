from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend.app.api.runtime import TaskRuntime
from backend.app.conversation.store import SQLiteConversationStore
from backend.app.main import create_app
from backend.app.schemas.contracts import (
    EvidenceItem, EvidenceSourceType, TaskCreateRequest, TaskStatus, TaskStep,
)


FOLLOW_UP = (
    "Does this document prove that UNBOUND AI achieves 99% accuracy? Identify the exact supporting evidence. "
    "If the document does not establish this claim, explicitly say so. Do not infer or invent a percentage."
)


class CapturingProvider:
    provider_name = "ollama"
    model = "test-model"
    base_url = "http://127.0.0.1:11434/v1"

    def __init__(self, answer="The document does not establish that UNBOUND AI achieves 99% accuracy."):
        self.answer = answer
        self.prompt = None

    def generate_text(self, *, system, prompt, **kwargs):
        self.prompt = prompt
        return self.answer


def make_runtime(tmp_path, *, provider=None):
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    return TaskRuntime(conversation_store=store, provider=provider or CapturingProvider())


def add_verified_document_task(runtime, session_id, filename, excerpt, request=None):
    user_request = request or f"Read C:\\synthetic\\{filename} and analyze the main findings."
    session_messages = runtime.conversation_store.get_messages(session_id)
    if not any(item["content"] == user_request for item in session_messages if item["role"] == "user"):
        runtime.conversation_store.append_exchange(session_id, __import__("uuid").uuid4(), user_request, "Task prepared.")
    step = TaskStep(
        step_id="read_analyze", tool_name="document.read_local_file", description="Read the approved synthetic document.",
        input={"file_path": f"C:\\synthetic\\{filename}"}, requires_approval=True,
        expected_outcome="Extract and verify document evidence.",
    )
    task = runtime.manager.create(TaskCreateRequest(user_request=user_request), [step])
    evidence = EvidenceItem(
        task_id=task.task_id, source_type=EvidenceSourceType.DOCUMENT, source_name=filename,
        excerpt=excerpt, supports_claim="Exact synthetic source excerpt",
    )
    finding = {
        "kind": "fact", "statement": "The document contains no accuracy measurement.",
        "quote": excerpt, "evidence_id": str(evidence.evidence_id), "source_chunk_id": "synthetic-chunk-1",
    }
    task.status = TaskStatus.COMPLETED
    task.evidence = [evidence]
    task.result = {
        "verified": [{
            "step_id": "read_analyze", "verification": {
                "verified": True,
                "tool_result": {
                    "original_filename": filename, "analysis_status": "verified_quotes",
                    "analysis": {"summary": "The synthetic document reports its limitations.", "findings": [finding]},
                    "analysis_scope": {"complete_document_coverage": True},
                    "analysis_scope_limited_to_chunks": ["synthetic-chunk-1"],
                },
            },
        }],
    }
    # Represent a task persisted by the trusted executor. Public save/get
    # intentionally redact verifier tool results and are not suitable for
    # constructing an internal verified-task fixture.
    runtime.manager.save_for_execution(task)
    runtime.conversation_store.link_task(session_id, task.task_id)
    return task, evidence


def test_document_followup_uses_verified_task_evidence_and_does_not_create_task(tmp_path):
    provider = CapturingProvider(
        'The document does not establish 99% accuracy. It says: "No experimental measurements are provided." '
        "[unbound_demo.txt, evidence 1]"
    )
    runtime = make_runtime(tmp_path, provider=provider)
    client = TestClient(create_app(runtime))
    session_id = client.post("/api/conversations").json()["session_id"]
    excerpt = "Limitations: This document contains no experimental measurements and does not establish real-world AI accuracy."
    task, evidence = add_verified_document_task(runtime, session_id, "unbound_demo.txt", excerpt)
    public_tool_result = runtime.manager.get(task.task_id).result["verified"][0]["verification"]["tool_result"]
    assert public_tool_result == {}
    before = len(runtime.manager.repository._tasks)

    intent = client.post("/api/assistant/classify", json={"session_id": session_id, "message": FOLLOW_UP})
    assert intent.status_code == 200
    assert intent.json()["category"] == "information"
    response = client.post(
        f"/api/conversations/{session_id}/messages",
        json={"message": FOLLOW_UP, "request_id": "b3511cce-9246-4e17-8cda-62b346ee971d"},
    )
    assert response.status_code == 200
    assert "does not establish 99% accuracy" in response.json()["reply"]
    assert excerpt in response.json()["reply"]
    assert str(evidence.evidence_id) in response.json()["reply"]
    prompt = json.loads(provider.prompt)
    context = prompt["previous_verified_document_task"]
    assert context["task_id"] == str(task.task_id)
    assert context["evidence"][0]["evidence_id"] == str(evidence.evidence_id)
    assert context["evidence"][0]["excerpt"] == excerpt
    assert len(runtime.manager.repository._tasks) == before
    assert runtime.manager.get(task.task_id).status == TaskStatus.COMPLETED


def test_multiple_document_tasks_require_clarification_unless_filename_matches(tmp_path):
    runtime = make_runtime(tmp_path)
    client = TestClient(create_app(runtime))
    session_id = client.post("/api/conversations").json()["session_id"]
    add_verified_document_task(runtime, session_id, "first.txt", "First source: no measurements.")
    second, _ = add_verified_document_task(runtime, session_id, "second.txt", "Second source: no accuracy data.")

    ambiguous = client.post("/api/assistant/classify", json={"session_id": session_id, "message": FOLLOW_UP})
    assert ambiguous.json()["category"] == "clarification_required"
    focused = client.post(
        f"/api/conversations/{session_id}/messages",
        json={"message": "Does second.txt establish the accuracy claim?", "request_id": "1c38918e-93ca-4fda-9a2a-34820992d9c1"},
    )
    assert focused.status_code == 200
    context = json.loads(runtime.provider.prompt)["previous_verified_document_task"]
    assert context["task_id"] == str(second.task_id)
    assert context["source_name"] == "second.txt"


def test_followup_naming_a_different_document_does_not_use_wrong_evidence(tmp_path):
    provider = CapturingProvider()
    runtime = make_runtime(tmp_path, provider=provider)
    client = TestClient(create_app(runtime))
    session_id = client.post("/api/conversations").json()["session_id"]
    add_verified_document_task(runtime, session_id, "analyzed.txt", "Only this document is verified.")
    response = client.post(
        f"/api/conversations/{session_id}/messages",
        json={"message": "Does other-report.pdf establish an accuracy claim?", "request_id": "bbe75fa1-5f55-4e93-b7bd-544d78a7de99"},
    )
    assert response.status_code == 200
    assert "don't have a verified analysis" in response.json()["reply"]
    assert provider.prompt is None


def test_legacy_server_task_reference_is_restored_and_exposed_to_frontend(tmp_path):
    runtime = make_runtime(tmp_path)
    client = TestClient(create_app(runtime))
    session_id = client.post("/api/conversations").json()["session_id"]
    task, _ = add_verified_document_task(runtime, session_id, "legacy.txt", "Legacy verified excerpt.")
    # Simulate an older assistant client that stored the server task link only in its reply.
    runtime.conversation_store.delete_session(session_id)
    session_id = runtime.conversation_store.create_session()["session_id"]
    user_request = task.user_request
    runtime.conversation_store.append_exchange(
        session_id, __import__("uuid").uuid4(), user_request,
        f"To proceed, explicitly call POST /api/tasks/{task.task_id}/execute.",
    )
    refs = client.get(f"/api/conversations/{session_id}/tasks")
    assert refs.status_code == 200
    assert refs.json()["available_task_ids"] == [str(task.task_id)]


def test_after_runtime_restart_link_is_reported_unavailable_without_file_read(tmp_path):
    first = make_runtime(tmp_path)
    session_id = first.conversation_store.create_session()["session_id"]
    task, _ = add_verified_document_task(first, session_id, "restart.txt", "Saved verified text.")
    # Same persistent conversation DB, but a new TaskManager as happens after restart.
    restarted = make_runtime(tmp_path)
    client = TestClient(create_app(restarted))
    refs = client.get(f"/api/conversations/{session_id}/tasks")
    assert refs.json()["available_task_ids"] == []
    assert refs.json()["unavailable_task_ids"] == [str(task.task_id)]
    reply = client.post(
        f"/api/conversations/{session_id}/messages",
        json={"message": "Does this document support the claim?", "request_id": "de899cf6-37b6-4e19-8dcc-1969b79534bc"},
    )
    assert reply.status_code == 200
    assert "unavailable in the current backend runtime" in reply.json()["reply"]
    assert "have not reopened the file" in reply.json()["reply"]
    assert restarted.manager.repository._tasks == {}


def test_mixed_available_and_unavailable_document_tasks_require_filename(tmp_path):
    runtime = make_runtime(tmp_path)
    client = TestClient(create_app(runtime))
    session_id = client.post("/api/conversations").json()["session_id"]
    missing, _ = add_verified_document_task(runtime, session_id, "older.txt", "Older verified excerpt.")
    add_verified_document_task(runtime, session_id, "newer.txt", "Newer verified excerpt.")
    runtime.manager.repository._tasks.pop(missing.task_id)

    response = client.post(
        f"/api/assistant/classify",
        json={"session_id": session_id, "message": "Does this document establish an accuracy claim?"},
    )
    assert response.json()["category"] == "clarification_required"
    explicit = client.post(
        f"/api/conversations/{session_id}/messages",
        json={"message": "Does newer.txt establish an accuracy claim?", "request_id": "52528974-6c2c-459d-a7a4-08de997a93f6"},
    )
    assert explicit.status_code == 200
    context = json.loads(runtime.provider.prompt)["previous_verified_document_task"]
    assert context["source_name"] == "newer.txt"


def test_document_followup_without_any_task_remains_ordinary_conversation(tmp_path):
    provider = CapturingProvider("I do not have a linked analysis for that document yet.")
    runtime = make_runtime(tmp_path, provider=provider)
    client = TestClient(create_app(runtime))
    session_id = client.post("/api/conversations").json()["session_id"]
    response = client.post(
        f"/api/conversations/{session_id}/messages",
        json={"message": FOLLOW_UP, "request_id": "a604923d-dc62-4dd7-8f94-084dc17786a2"},
    )
    assert response.status_code == 200
    assert json.loads(provider.prompt)["previous_verified_document_task"] is None
    assert runtime.manager.repository._tasks == {}


def test_task_link_persists_atomically_with_exchange_and_conflicting_retry_does_not_link(tmp_path):
    runtime = make_runtime(tmp_path)
    store = runtime.conversation_store
    session_id = store.create_session()["session_id"]
    task_id = "369395e3-f3ba-43ca-8227-b5d73c18f7fe"
    request_id = "8c43c5be-e203-459b-b6ca-69e052cf773d"
    store.append_exchange(session_id, request_id, "Original request", "Task plan response", task_id=task_id)
    store.append_exchange(session_id, request_id, "Different retry body", "ignored", task_id="cbd56a8b-b2b8-4b53-9f89-696e06f9424c")
    assert store.get_task_ids(session_id) == [task_id]


def test_document_excerpts_are_never_sent_to_nonlocal_provider(tmp_path):
    provider = CapturingProvider("This should not be called.")
    provider.provider_name = "openai"
    provider.base_url = "https://api.example.test/v1"
    runtime = make_runtime(tmp_path, provider=provider)
    client = TestClient(create_app(runtime))
    session_id = client.post("/api/conversations").json()["session_id"]
    add_verified_document_task(runtime, session_id, "private-synthetic.txt", "Synthetic private source excerpt.")

    response = client.post(
        f"/api/conversations/{session_id}/messages",
        json={"message": "What does this document establish?", "request_id": "0089a00e-27c8-4e31-8027-bd2721b168c1"},
    )
    assert response.status_code == 200
    assert "only be sent to local Ollama" in response.json()["reply"]
    assert provider.prompt is None
