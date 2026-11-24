from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.app.api.runtime import TaskRuntime
from backend.app.conversation.store import SQLiteConversationStore
from backend.app.main import create_app
from backend.app.orchestrator.intent import TaskIntent
from backend.app.llm.provider import LLMProviderError
from backend.app.schemas.contracts import TaskStep, ToolDefinition


class SearchInput(BaseModel):
    query: str


def classification(category: str, **values):
    return {
        "category": category,
        "clarification_question": "Which project do you mean?" if category == "clarification_required" else None,
        "current_turn_authorizes_execution": category == "action_execution",
        **values,
    }


class IntentProvider:
    provider_name = "ollama"
    model = "qwen3.5:4b"

    def __init__(self, intent=None, plan_steps=None, task_missing_information=None):
        self.intent = intent or classification("information")
        self.task_missing_information = task_missing_information or []
        self.plan_steps = plan_steps or [TaskStep(
            step_id="search_project",
            tool_name="document.search",
            description="Search synthetic project documents",
            input={"query": "project milestones"},
            expected_outcome="Matching project evidence is returned",
        )]
        self.responses = []
        self.calls = []

    def generate_json(self, *, system, prompt, response_model, **kwargs):
        self.calls.append({"system": system, "prompt": prompt, "model": response_model.__name__})
        if response_model.__name__ == "AssistantIntent":
            if isinstance(self.intent, Exception):
                raise self.intent
            if isinstance(self.intent, dict):
                return self.intent
            return response_model.model_validate(self.intent)
        if response_model is TaskIntent:
            return response_model(
                goal="Search synthetic project documents",
                required_capabilities=["document.search"],
                sensitive_actions=[],
                missing_information=self.task_missing_information,
            )
        return response_model(steps=self.plan_steps)

    def generate_text(self, *, system, prompt, **kwargs):
        self.responses.append({"system": system, "payload": json.loads(prompt)})
        return "A synthetic informational reply."


def make_client(tmp_path, provider, *, register_tool=True):
    store = SQLiteConversationStore(tmp_path / "assistant-intent.sqlite3")
    runtime = TaskRuntime(provider=provider, conversation_store=store)
    executions = []
    if register_tool:
        runtime.register_tool(
            ToolDefinition(
                tool_name="document.search",
                description="Search indexed documents",
                input_schema=SearchInput.model_json_schema(),
            ),
            SearchInput,
            lambda arguments, call: executions.append(call),
        )
    return TestClient(create_app(runtime)), runtime, executions


@pytest.mark.parametrize("category", ["conversation", "information", "drafting"])
def test_non_action_intent_categories_answer_without_creating_tasks(tmp_path, category):
    provider = IntentProvider(classification(category))
    client, runtime, executions = make_client(tmp_path, provider)

    response = client.post("/api/assistant/requests", json={"message": "Help me understand this synthetic concept."})

    assert response.status_code == 200
    assert response.json()["intent"]["category"] == category
    assert response.json()["task"] is None
    assert response.json()["reply"] == "A synthetic informational reply."
    assert runtime.manager.repository._tasks == {}
    assert executions == []


def test_local_model_connectivity_answer_uses_successful_bounded_provider_request(tmp_path):
    provider = IntentProvider(classification("information"))
    client, runtime, _ = make_client(tmp_path, provider)

    response = client.post(
        "/api/assistant/requests",
        json={"message": "Hello, UNBOUND. Are you connected to my local model?"},
    )

    assert response.status_code == 200
    assert "Ollama responded successfully to a live request" in response.json()["reply"]
    assert "qwen3.5:4b" in response.json()["reply"]
    assert len(provider.responses) == 1
    assert runtime.manager.repository._tasks == {}


def test_ambiguous_intent_returns_focused_clarification_without_task(tmp_path):
    provider = IntentProvider({
        "category": "clarification_required",
        "question": "Which project do you mean?",
        "current_turn_authorizes_execution": False,
    })
    client, runtime, executions = make_client(tmp_path, provider)

    response = client.post("/api/assistant/requests", json={"message": "Help me organize my files."})

    assert response.status_code == 200
    assert response.json()["reply"] == "Which project do you mean?"
    assert response.json()["task"] is None
    assert len(provider.calls) == 1
    assert runtime.manager.repository._tasks == {}
    assert executions == []


@pytest.mark.parametrize("category", ["action_planning", "action_execution"])
def test_action_intent_uses_registered_planner_and_never_auto_executes(tmp_path, category):
    provider = IntentProvider(classification(category))
    client, runtime, executions = make_client(tmp_path, provider)

    response = client.post("/api/assistant/requests", json={"message": "Search my project documents."})

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"]["category"] == category
    assert payload["task"]["status"] == "created"
    assert payload["task"]["plan"][0]["tool_name"] == "document.search"
    assert payload["task"]["plan"][0]["depends_on"] == []
    assert "Nothing has been executed" in payload["reply"]
    assert runtime.manager.get(payload["task"]["task_id"]).status.value == "created"
    assert executions == []


def test_action_execution_without_current_turn_authorization_downgrades_to_clarification(tmp_path):
    provider = IntentProvider(classification("action_execution", current_turn_authorizes_execution=False))
    client, runtime, executions = make_client(tmp_path, provider)

    response = client.post("/api/assistant/requests", json={"message": "I wish someone would organize my files."})

    assert response.status_code == 200
    assert response.json()["intent"]["category"] == "clarification_required"
    assert "plan" in response.json()["reply"].lower()
    assert response.json()["task"] is None
    assert len(provider.calls) == 1
    assert runtime.manager.repository._tasks == {}
    assert executions == []


def test_missing_parameters_request_clarification_before_planning(tmp_path):
    provider = IntentProvider(
        classification("action_execution", current_turn_authorizes_execution=True),
        task_missing_information=["source folder"],
    )
    client, runtime, executions = make_client(tmp_path, provider)

    response = client.post("/api/assistant/requests", json={"message": "Move the specified files."})

    assert response.status_code == 200
    assert response.json()["intent"]["category"] == "clarification_required"
    assert "source folder" in response.json()["reply"]
    assert response.json()["task"] is None
    assert len(provider.calls) == 3
    assert runtime.manager.repository._tasks == {}
    assert executions == []


def test_malformed_classifier_output_fails_safely(tmp_path):
    provider = IntentProvider({"category": "execute"})
    client, runtime, executions = make_client(tmp_path, provider)

    response = client.post("/api/assistant/requests", json={"message": "Move files."})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "llm_provider_error"
    assert runtime.manager.repository._tasks == {}
    assert executions == []


def test_classifier_provider_timeout_returns_safe_unavailable_response(tmp_path):
    provider = IntentProvider(LLMProviderError("private timeout details", public_message="The local model timed out. Retry."))
    client, runtime, executions = make_client(tmp_path, provider)

    response = client.post("/api/assistant/requests", json={"message": "Move files."})

    assert response.status_code == 503
    assert response.json()["detail"]["message"] == "The local model timed out. Retry."
    assert "private timeout details" not in response.text
    assert runtime.manager.repository._tasks == {}
    assert executions == []


def test_historical_context_is_not_execution_authorization(tmp_path):
    provider = IntentProvider(classification("action_execution", current_turn_authorizes_execution=False))
    client, runtime, executions = make_client(tmp_path, provider)
    session = client.post("/api/conversations").json()
    session_id = session["session_id"]
    runtime.conversation_store.append_exchange(
        session_id,
        "2ea9e2a3-8df7-471a-a820-1a5bb0a73bf2",
        "Please delete the old files.",
        "I can help plan that, but deletion needs explicit confirmation.",
    )

    response = client.post("/api/assistant/requests", json={
        "message": "I wish my files were organized.",
        "session_id": session_id,
    })

    assert response.status_code == 200
    assert response.json()["intent"]["category"] == "clarification_required"
    classifier_payload = json.loads(provider.calls[0]["prompt"])
    assert classifier_payload["history_context"][0]["content"] == "Please delete the old files."
    assert "relevant_approved_memories" not in classifier_payload
    assert runtime.manager.repository._tasks == {}
    assert executions == []


def test_unavailable_tools_are_reported_without_fabricating_a_plan(tmp_path):
    provider = IntentProvider(classification("action_execution"))
    client, runtime, executions = make_client(tmp_path, provider, register_tool=False)

    response = client.post("/api/assistant/requests", json={"message": "Search the report."})

    assert response.status_code == 200
    assert response.json()["task"] is None
    assert response.json()["intent"]["category"] == "action_execution"
    assert "no tools are currently registered" in response.json()["reply"].lower()
    assert runtime.manager.repository._tasks == {}
    assert executions == []
