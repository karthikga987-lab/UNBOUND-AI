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
from backend.app.schemas.contracts import TaskStep, ToolDefinition, ToolResult
from backend.app.assistant.intent import (
    classify_assistant_intent,
    is_public_web_research_request,
    public_web_search_tool_names,
)


class SearchInput(BaseModel):
    query: str


class BrowserNavigateInput(BaseModel):
    url: str


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
                goal="Perform the requested browser research",
                required_capabilities=[step.tool_name for step in self.plan_steps],
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


def test_public_research_is_not_routed_to_local_document_search(tmp_path):
    class NoModelNeededProvider(IntentProvider):
        def generate_json(self, *, system, prompt, response_model, **kwargs):
            raise AssertionError("Unavailable public search must be reported before LLM planning")

    provider = NoModelNeededProvider()
    client, runtime, executions = make_client(tmp_path, provider)

    response = client.post(
        "/api/assistant/requests",
        json={"message": "Research the latest developments in AI agents and cite credible public sources."},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"] is None
    assert body["intent"]["category"] == "action_execution"
    assert "document.search searches only locally indexed documents" in body["reply"]
    assert "No public search was performed" in body["reply"]
    assert "sources were generated" in body["reply"]
    assert runtime.manager.repository._tasks == {}
    assert provider.calls == []
    assert executions == []


@pytest.mark.parametrize("message", [
    "Search my project documents for the release plan.",
    "Search the report for the budget section.",
])
def test_local_document_search_is_not_misclassified_as_public_research(message):
    assert not is_public_web_research_request(message)


def test_direct_url_page_analysis_is_not_misclassified_as_web_discovery():
    assert not is_public_web_research_request(
        "Search the web and inspect https://example.com/research."
    )


def test_public_search_tool_detection_uses_actual_registered_metadata():
    assert public_web_search_tool_names([
        {"tool_name": "document.search", "description": "Search indexed documents"},
        {"tool_name": "browser.navigate", "description": "Navigate to an allowlisted URL"},
        {"tool_name": "web.search", "description": "Search the public web"},
    ]) == {"web.search"}


def test_public_research_intent_is_deterministic_and_does_not_call_model():
    class NoModelNeededProvider(IntentProvider):
        def generate_json(self, *, system, prompt, response_model, **kwargs):
            raise AssertionError("Clear public-research intent must not depend on an LLM guess")

    intent = classify_assistant_intent(
        "Research the latest developments in AI agents and cite credible sources.",
        NoModelNeededProvider(),
        registered_tools=[{"tool_name": "document.search", "description": "Search indexed documents"}],
    )

    assert intent.category == "action_execution"
    assert intent.current_turn_authorizes_execution is True


def test_direct_browser_research_prepares_plan_and_run_plan_executes_saved_parameters(tmp_path):
    url = "https://example.com/research"
    provider = IntentProvider(plan_steps=[TaskStep(
        step_id="open_research_source",
        tool_name="browser.navigate",
        description="Open the approved research source",
        input={"url": url},
        expected_outcome="The requested page is observed",
    )])
    store = SQLiteConversationStore(tmp_path / "browser-research.sqlite3")
    runtime = TaskRuntime(provider=provider, conversation_store=store)
    calls = []
    runtime.register_tool(
        ToolDefinition(
            tool_name="browser.navigate",
            description="BROWSERLESS CLOUD DEMO: navigate to exact allowlisted HTTPS hosts only.",
            input_schema=BrowserNavigateInput.model_json_schema(),
        ),
        BrowserNavigateInput,
        lambda arguments, call: calls.append(call) or ToolResult(success=True, data={"url": arguments.url}),
        verifier=lambda tool, expected, result: result.success and result.data["url"] == url,
    )
    client = TestClient(create_app(runtime))

    prepared = client.post("/api/assistant/requests", json={"message": f"Search the web and inspect {url}."})

    assert prepared.status_code == 200, prepared.text
    body = prepared.json()
    assert body["intent"]["category"] == "action_execution"
    assert body["task"]["status"] == "created"
    assert body["task"]["plan"][0]["input"] == {"url": url}
    assert "select Run plan" in body["reply"]
    assert calls == []

    executed = client.post(f"/api/tasks/{body['task']['task_id']}/execute")

    assert executed.status_code == 200, executed.text
    assert executed.json()["status"] == "completed", json.dumps(executed.json(), indent=2)
    assert len(calls) == 1
    assert calls[0].arguments == {"url": url}


def test_explicit_readonly_https_request_does_not_turn_extraction_outputs_into_clarification(tmp_path):
    class NoModelNeededProvider(IntentProvider):
        def generate_json(self, *, system, prompt, response_model, **kwargs):
            self.calls.append({"model": response_model.__name__})
            raise AssertionError("Explicit read-only HTTPS navigation should not ask the model for output fields")

    url = "https://example.com"
    provider = NoModelNeededProvider()
    runtime = TaskRuntime(provider=provider, conversation_store=SQLiteConversationStore(tmp_path / "direct-page.sqlite3"))
    calls = []
    runtime.register_tool(
        ToolDefinition(
            tool_name="browser.navigate",
            description="BROWSERLESS CLOUD DEMO: exact-host HTTPS navigation only.",
            input_schema=BrowserNavigateInput.model_json_schema(),
        ),
        BrowserNavigateInput,
        lambda arguments, call: calls.append(call) or ToolResult(success=True, data={"url": str(arguments.url)}),
        verifier=lambda _tool, _expected, result: result.success and result.data["url"] == f"{url}/",
    )
    client = TestClient(create_app(runtime))

    prepared = client.post("/api/assistant/requests", json={
        "message": "Open https://example.com, extract its page title and main visible text, and capture a screenshot."
    })

    assert prepared.status_code == 200, prepared.text
    body = prepared.json()
    assert body["task"]["status"] == "created"
    assert body["task"]["plan"][0]["tool_name"] == "browser.navigate"
    assert body["task"]["plan"][0]["input"] == {"url": url}
    assert "select Run plan" in body["reply"]
    assert calls == []
    assert provider.calls == []


def test_uploaded_document_request_selects_managed_upload_and_prepares_read_only_plan(tmp_path):
    from backend.app.documents.extractor import extract_document
    from backend.app.documents.local_file import LocalDocumentAnalysis
    from backend.app.documents.store import DocumentStore

    class UploadedProvider(IntentProvider):
        base_url = "http://127.0.0.1:11434/v1"

        def generate_json(self, *, system, prompt, response_model, **kwargs):
            if response_model is LocalDocumentAnalysis:
                payload = json.loads(prompt)
                span = payload["source_spans"][0]
                return response_model.model_validate({
                    "summary": "The report says delivery is planned for June.",
                    "findings": [{
                        "kind": "fact",
                        "statement": "Delivery is planned for June.",
                        "source_chunk_id": span["chunk_id"],
                        "source_span_id": span["source_span_id"],
                    }],
                    "missing_or_unreadable": [],
                })
            return super().generate_json(system=system, prompt=prompt, response_model=response_model, **kwargs)

        def generate_text(self, *, system, prompt, **kwargs):
            return "The uploaded report states that delivery is planned for June."

    provider = UploadedProvider()
    runtime = TaskRuntime(
        provider=provider,
        conversation_store=SQLiteConversationStore(tmp_path / "uploaded.sqlite3"),
        document_store=DocumentStore(tmp_path / "documents"),
    )
    runtime.register_document_tools()
    source = b"Synthetic report: delivery is planned for June."
    document_id_value = "de9a2ef0-20eb-4280-9b77-979a5d148e3e"
    extraction = extract_document(source, document_id_value, "brief.txt")
    document_id, stored_name, stored_path = runtime.document_store.save_file(
        source, "brief.txt", document_id=document_id_value
    )
    runtime.document_store.save_extraction(
        document_id, "brief.txt", stored_name, stored_path, extraction
    )
    client = TestClient(create_app(runtime))

    session_id = client.post("/api/conversations").json()["session_id"]
    response = client.post("/api/assistant/requests", json={"message": "Analyze this document.", "session_id": session_id})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"]["status"] == "created"
    assert body["task"]["plan"][0]["tool_name"] == "document.analyze_uploaded"
    assert body["task"]["plan"][0]["input"]["document_id"] == document_id
    assert body["task"]["plan"][0]["requires_approval"] is False
    assert "No analysis has run yet" in body["reply"]

    executed = client.post(f"/api/tasks/{body['task']['task_id']}/execute")
    assert executed.status_code == 200, executed.text
    assert executed.json()["status"] == "completed", json.dumps(executed.json(), indent=2)
    assert executed.json()["evidence"][0]["excerpt"] == "Synthetic report: delivery is planned for June."

    followup = client.post("/api/assistant/requests", json={
        "message": "What are the report findings?", "session_id": session_id,
    })
    assert followup.status_code == 200, followup.text
    assert "planned for june" in followup.json()["reply"].lower()
    assert "verified source" in followup.json()["reply"].lower()


def test_uploaded_document_request_without_upload_does_not_claim_to_have_read_a_file(tmp_path):
    from backend.app.documents.store import DocumentStore

    runtime = TaskRuntime(
        provider=IntentProvider(),
        conversation_store=SQLiteConversationStore(tmp_path / "no-upload.sqlite3"),
        document_store=DocumentStore(tmp_path / "empty-documents"),
    )
    runtime.register_document_tools()
    client = TestClient(create_app(runtime))

    response = client.post("/api/assistant/requests", json={"message": "Analyze this document."})

    assert response.status_code == 200
    assert response.json()["task"] is None
    assert response.json()["intent"]["category"] == "clarification_required"
    assert "upload" in response.json()["reply"].lower()
    assert "no file was opened" in response.json()["reply"].lower()
