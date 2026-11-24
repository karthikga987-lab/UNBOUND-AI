from __future__ import annotations

import json

from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.app.api.runtime import TaskRuntime
from backend.app.conversation.store import SQLiteConversationStore
from backend.app.main import create_app
from backend.app.memory.models import ExtractedMemoryCandidate
from backend.app.schemas.contracts import TaskStep, ToolDefinition, ToolResult


class SearchInput(BaseModel):
    query: str


class SyntheticProvider:
    provider_name = "synthetic-test"

    def __init__(self):
        self.contexts = []

    def generate_text(self, *, system, prompt, **kwargs):
        payload = json.loads(prompt)
        self.contexts.append(payload)
        return "Synthetic answer based on the supplied context."

    def generate_json(self, *, system, prompt, response_model, **kwargs):
        name = response_model.__name__
        if name == "TaskIntent":
            return response_model(
                goal="Search synthetic project documents",
                required_capabilities=["document.search"],
                sensitive_actions=[],
                missing_information=[],
            )
        if name == "MemoryExtractionOutput":
            payload = json.loads(prompt)
            source_id = payload["user_messages"][0]["message_id"]
            return response_model(candidates=[ExtractedMemoryCandidate(
                content="Synthetic preference: concise project summaries.",
                category="preference",
                source_message_id=source_id,
            )])
        if name == "AssistantIntent":
            payload = json.loads(prompt)
            message = payload["current_user_message"].lower()
            category = "action_planning" if "search my synthetic" in message else "information"
            return response_model.model_validate({
                "category": category,
                "clarification_question": None,
                "current_turn_authorizes_execution": False,
            })
        if name == "PlanDraft":
            return response_model(steps=[TaskStep(
                step_id="search_synthetic_project",
                tool_name="document.search",
                description="Search synthetic project documents",
                input={"query": "synthetic project"},
                expected_outcome="A matching synthetic source is found",
            )])
        raise AssertionError(f"Unexpected structured response model: {response_model.__name__}")


def test_synthetic_local_companion_flow_keeps_memory_conversation_and_actions_separate(tmp_path):
    database = SQLiteConversationStore(tmp_path / "companion.sqlite3")
    provider = SyntheticProvider()
    runtime = TaskRuntime(provider=provider, conversation_store=database)
    executions = []
    runtime.register_tool(
        ToolDefinition(
            tool_name="document.search",
            description="Search indexed documents",
            input_schema=SearchInput.model_json_schema(),
            requires_approval=True,
        ),
        SearchInput,
        lambda arguments, call: (executions.append(call) or ToolResult(
            success=True,
            data={"match_count": 1},
            evidence=[{
                "task_id": str(call.task_id),
                "source_type": "document",
                "source_name": "synthetic-report.txt",
                "excerpt": "Synthetic matching source.",
                "supports_claim": "A synthetic project source matched the query.",
            }],
        )),
        verifier=lambda tool, expected, result: result.success and result.data["match_count"] == 1,
    )
    client = TestClient(create_app(runtime))

    session = client.post("/api/conversations", json={}).json()
    exchange = client.post(f"/api/conversations/{session['session_id']}/messages", json={
        "message": "Synthetic test: I prefer concise project summaries.",
    })
    assert exchange.status_code == 200
    assert client.get(f"/api/conversations/{session['session_id']}").status_code == 200

    extraction = client.post("/api/memories/extract", json={"session_id": session["session_id"]})
    assert extraction.status_code == 200 and extraction.json()[0]["status"] == "pending_approval"
    memory_id = extraction.json()[0]["memory_id"]
    assert client.post(f"/api/memories/{memory_id}/approve").json()["user_confirmed"] is True

    new_session = client.post("/api/conversations", json={}).json()
    answer = client.post("/api/conversation", json={"message": "What is my preference for concise project summaries?"})
    assert answer.status_code == 200
    assert provider.contexts[-1]["relevant_approved_memories"]
    client.patch("/api/memories/settings", json={"retrieval_enabled": False})
    client.post("/api/conversation", json={"message": "What is my preference for concise project summaries?"})
    assert provider.contexts[-1]["relevant_approved_memories"] == []
    client.patch("/api/memories/settings", json={"retrieval_enabled": True})

    info = client.post("/api/assistant/requests", json={"message": "How should I summarize a synthetic report?"})
    assert info.json()["intent"]["category"] == "information"
    assert info.json()["task"] is None
    assert runtime.manager.repository._tasks == {}

    prepared = client.post("/api/assistant/requests", json={"message": "Search my synthetic project documents."})
    assert prepared.status_code == 200, prepared.text
    task = prepared.json()["task"]
    assert task["status"] == "created"
    assert executions == [], "planning must not invoke the registered tool"
    awaiting = client.post(f"/api/tasks/{task['task_id']}/execute")
    assert awaiting.json()["status"] == "awaiting_approval"
    assert executions == []
    approval_id = awaiting.json()["pending_approvals"][0]["approval_id"]
    assert client.post(
        f"/api/tasks/{task['task_id']}/approvals/{approval_id}", json={"decision": "APPROVED"}
    ).status_code == 200
    completed = client.post(f"/api/tasks/{task['task_id']}/execute")
    assert completed.json()["status"] == "completed"
    assert completed.json()["plan"][0]["verification"]["verified"] is True
    assert len(executions) == 1

    assert client.delete(f"/api/memories/{memory_id}").status_code == 204
    restarted_runtime = TaskRuntime(
        provider=SyntheticProvider(),
        conversation_store=SQLiteConversationStore(tmp_path / "companion.sqlite3"),
    )
    restarted = TestClient(create_app(restarted_runtime))
    assert restarted.get(f"/api/conversations/{session['session_id']}").status_code == 200
    assert restarted.get(f"/api/memories/{memory_id}").status_code == 404
    assert restarted.post("/api/memories/extract", json={"session_id": session["session_id"]}).json() == []
    assert restarted.get(f"/api/tasks/{task['task_id']}").status_code == 404
    assert new_session["session_id"] != session["session_id"]
