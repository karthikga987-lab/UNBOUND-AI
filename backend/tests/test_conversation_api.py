from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.api.runtime import TaskRuntime
from backend.app.api.routes.conversation import ConversationReply
from backend.app.llm.provider import LLMConfigurationError, LLMProviderError, OllamaNativeProvider, OpenAICompatibleProvider
from backend.app.main import create_app
from backend.app.orchestrator.planner import PlanDraft


def test_ollama_conversation_uses_existing_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "qwen3.5:4b")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = []

    def fake_post(url, *, json, headers, timeout):
        calls.append({"url": url, "body": json, "headers": headers, "timeout": timeout})
        return httpx.Response(
            200,
            json={"model": "qwen3.5:4b", "done": True, "done_reason": "stop", "message": {"role": "assistant", "content": "Hello from the local model."}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    response = TestClient(create_app()).post("/api/conversation", json={"message": "Hello"})

    assert response.status_code == 200
    assert response.json() == {"reply": "Hello from the local model."}
    assert calls[0]["url"] == "http://localhost:11434/api/chat"
    assert calls[0]["body"]["model"] == "qwen3.5:4b"
    assert calls[0]["body"]["stream"] is False
    assert calls[0]["body"]["think"] is False
    assert calls[0]["body"]["options"]["num_predict"] == 256
    assert "reasoning_effort" not in calls[0]["body"]
    assert calls[0]["headers"].get("Authorization") is None
    assert calls[0]["timeout"].read == 90
    assert calls[0]["timeout"].connect == 5


def test_ollama_configuration_rejects_an_openai_endpoint_path(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "qwen3.5:4b")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1/chat/completions")
    with pytest.raises(LLMConfigurationError, match="Ollama server URL"):
        from backend.app.llm.provider import get_provider_from_env
        get_provider_from_env()


def test_native_ollama_unknown_model_error_is_actionable_without_echoing_body(caplog, monkeypatch):
    private_prompt = "private synthetic document contents"

    def missing_model(url, *, json, headers, timeout):
        return httpx.Response(
            400,
            json={"error": "model 'not-installed:latest' not found; " + private_prompt},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", missing_model)
    provider = OllamaNativeProvider(base_url="http://localhost:11434/v1", model="not-installed:latest")
    with pytest.raises(LLMProviderError) as error:
        provider.generate_text(system="system", prompt=private_prompt, max_tokens=64)

    assert error.value.public_message == "Ollama rejected the request (HTTP 400): The configured Ollama model was not found."
    assert "model_unavailable" in caplog.text
    assert "provider_error_body_sha256=" in caplog.text
    assert private_prompt not in caplog.text
    assert private_prompt not in error.value.public_message


def test_ollama_structured_requests_send_the_exact_pydantic_json_schema(monkeypatch):
    calls = []
    draft_json = json.dumps({
        "steps": [{
            "step_id": "step_1",
            "tool_name": "document.search",
            "description": "Search the synthetic report index",
            "input": {"query": "synthetic quarterly report"},
            "depends_on": [],
            "requires_approval": False,
            "expected_outcome": "A matching synthetic report is found",
        }],
        "missing_information": [],
    })

    def response_for(url, *, json, headers, timeout):
        calls.append(json)
        return httpx.Response(
            200,
            json={"model": "qwen3.5:4b", "done": True, "done_reason": "stop", "message": {"role": "assistant", "content": draft_json}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", response_for)
    provider = OllamaNativeProvider(base_url="http://localhost:11434/v1", model="qwen3.5:4b")
    draft = provider.generate_json(
        system="synthetic planner prompt", prompt="synthetic task", response_model=PlanDraft, max_tokens=1024
    )

    assert isinstance(draft, PlanDraft)
    assert calls[0]["format"] == PlanDraft.model_json_schema()
    assert calls[0]["think"] is False
    assert calls[0]["options"]["num_predict"] == 1024


def test_conversation_provider_unavailable_returns_safe_503():
    def unavailable():
        raise LLMProviderError("private connection detail")

    response = TestClient(create_app(TaskRuntime(provider_factory=unavailable))).post(
        "/api/conversation", json={"message": "Hi"}
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "llm_provider_error"
    assert "private connection detail" not in response.text


def test_ollama_http_400_reports_safe_provider_diagnostics_without_prompt_leak(caplog, monkeypatch):
    private_prompt = "private uploaded content must never appear in logs"

    def rejected(url, *, json, headers, timeout):
        assert url == "http://localhost:11434/api/chat"
        assert json["messages"][-1]["content"] == private_prompt
        return httpx.Response(
            400,
            json={"error": f"unknown parameter 'think'; request={private_prompt}"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", rejected)
    provider = OllamaNativeProvider(base_url="http://localhost:11434/v1", model="qwen3.5:4b")
    with pytest.raises(LLMProviderError) as error:
        provider.generate_text(
            system="system", prompt=private_prompt, max_tokens=32, reasoning_effort="none"
        )

    assert error.value.public_message == (
        "Ollama rejected the request (HTTP 400): Ollama rejected request parameter 'think'."
    )
    assert "provider_error_category=request_parameter_rejected" in caplog.text
    assert "provider_error_summary=\"Ollama rejected request parameter 'think'.\"" in caplog.text
    assert "provider_error_body_sha256=" in caplog.text
    assert "request_roles=system,user" in caplog.text
    assert "num_predict=32" in caplog.text
    assert "response_format=none" in caplog.text
    assert private_prompt not in caplog.text


def test_empty_conversation_message_is_rejected():
    response = TestClient(create_app()).post("/api/conversation", json={"message": "  \t "})
    assert response.status_code == 422


def test_conversation_context_is_sent_to_provider_but_does_not_execute_tasks():
    class CapturingProvider:
        prompt = ""
        system = ""

        def generate_text(self, *, system, prompt, **kwargs):
            self.system = system
            self.prompt = prompt
            return "We discussed the synthetic report earlier."

    provider = CapturingProvider()
    runtime = TaskRuntime(provider=provider)
    client = TestClient(create_app(runtime))
    response = client.post("/api/conversation", json={
        "message": "Summarize what we just discussed.",
        "history": [
            {"role": "user", "content": "I found a synthetic report."},
            {"role": "assistant", "content": "What would you like to know?"},
        ],
    })

    assert response.status_code == 200
    payload = json.loads(provider.prompt)
    assert payload["history"][0]["content"] == "I found a synthetic report."
    assert payload["current_user_message"] == "Summarize what we just discussed."
    assert "never execute tools" in provider.system
    assert runtime.manager.repository._tasks == {}


def ollama_client(monkeypatch, post):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "qwen3.5:4b")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1/")
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(httpx, "post", post)
    return TestClient(create_app())


def test_provider_timeout_is_reported_safely_and_logged(caplog, monkeypatch):
    def timeout(url, *, json, headers, timeout):
        raise httpx.ReadTimeout("request details may contain private URL data")

    response = ollama_client(monkeypatch, timeout).post("/api/conversation", json={"message": "Hello"})
    assert response.status_code == 503
    assert response.json()["detail"]["message"] == "The local model took too long to respond. Please retry."
    assert "ReadTimeout" in caplog.text
    assert "request_timeout" in caplog.text
    assert "private URL data" not in caplog.text
    assert "Hello" not in caplog.text


def test_connection_refused_is_reported_safely(monkeypatch):
    def refused(url, *, json, headers, timeout):
        raise httpx.ConnectError("connection refused")

    response = ollama_client(monkeypatch, refused).post("/api/conversation", json={"message": "Hello"})
    assert response.status_code == 503
    assert "Check that Ollama is running" in response.json()["detail"]["message"]


def test_transient_http_503_retries_once_and_recovers(caplog, monkeypatch):
    calls = []

    def first_busy_then_ok(url, *, json, headers, timeout):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            json={"model": "qwen3.5:4b", "done": True, "done_reason": "stop", "message": {"role": "assistant", "content": "Recovered."}},
            request=httpx.Request("POST", url),
        )

    response = ollama_client(monkeypatch, first_busy_then_ok).post("/api/conversation", json={"message": "Hello"})
    assert response.status_code == 200
    assert response.json()["reply"] == "Recovered."
    assert len(calls) == 2
    assert "category=transient_http_status" in caplog.text
    assert "provider=ollama" in caplog.text
    assert "model=qwen3.5:4b" in caplog.text
    assert "request_duration_ms=" in caplog.text


def test_persistent_http_503_stops_after_one_retry_and_is_safe(caplog, monkeypatch):
    calls = []

    def always_busy(url, *, json, headers, timeout):
        calls.append(1)
        return httpx.Response(503, request=httpx.Request("POST", url))

    response = ollama_client(monkeypatch, always_busy).post("/api/conversation", json={"message": "Hello"})
    assert response.status_code == 503
    assert response.json()["detail"]["message"] == "The local model service is temporarily unavailable. Please retry."
    assert len(calls) == 2
    assert "http_status=503" in caplog.text
    assert "Hello" not in caplog.text


def test_malformed_response_is_not_retried(monkeypatch):
    calls = []

    def malformed(url, *, json, headers, timeout):
        calls.append(1)
        return httpx.Response(200, json={"model": "qwen3.5:4b", "done": True, "message": {}}, request=httpx.Request("POST", url))

    response = ollama_client(monkeypatch, malformed).post("/api/conversation", json={"message": "Hello"})
    assert response.status_code == 503
    assert len(calls) == 1


def test_successful_conversation_after_persistent_transient_failure(monkeypatch):
    calls = []

    def busy_then_ready(url, *, json, headers, timeout):
        calls.append(1)
        if len(calls) <= 2:
            return httpx.Response(503, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            json={"model": "qwen3.5:4b", "done": True, "done_reason": "stop", "message": {"role": "assistant", "content": "Recovered on a new request."}},
            request=httpx.Request("POST", url),
        )

    client = ollama_client(monkeypatch, busy_then_ready)
    failed = client.post("/api/conversation", json={"message": "Synthetic first request"})
    recovered = client.post("/api/conversation", json={"message": "Synthetic follow-up request"})
    assert failed.status_code == 503
    assert recovered.status_code == 200
    assert recovered.json()["reply"] == "Recovered on a new request."
    assert len(calls) == 3


def test_success_after_failed_exchange_and_duplicate_request_do_not_duplicate_history(monkeypatch, tmp_path):
    calls = []

    def fail_once(url, *, json, headers, timeout):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            json={"model": "qwen3.5:4b", "done": True, "done_reason": "stop", "message": {"role": "assistant", "content": "Synthetic answer."}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setenv("UNBOUND_CONVERSATION_DB", str(tmp_path / "retry.sqlite3"))
    client = ollama_client(monkeypatch, fail_once)
    session = client.post("/api/conversations", json={}).json()["session_id"]
    payload = {"message": "Short greeting", "request_id": "cd466c88-598b-4d05-bcad-bf2255592678"}
    first = client.post(f"/api/conversations/{session}/messages", json=payload)
    assert first.status_code == 200
    second = client.post(f"/api/conversations/{session}/messages", json=payload)
    assert second.status_code == 200
    history = client.get(f"/api/conversations/{session}").json()["messages"]
    assert len(history) == 2
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("payload", "expected_log"),
    [
        ({"model": "qwen3.5:4b", "done": True, "message": {}}, "unexpected_response_structure"),
        ({"model": "qwen3.5:4b", "done": False, "message": {"role": "assistant", "content": "not final"}}, "unexpected_response_structure"),
        ({"model": "qwen3.5:4b", "done": True, "message": {"role": "assistant", "content": ""}}, "empty_message_content"),
        ({"model": "qwen3.5:4b", "done": True, "done_reason": "length", "message": {"role": "assistant", "content": "partial"}}, "generation_limit_reached"),
    ],
)
def test_malformed_empty_and_exhausted_responses_fail_safely(caplog, monkeypatch, payload, expected_log):
    def response_for(url, *, json, headers, timeout):
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    response = ollama_client(monkeypatch, response_for).post("/api/conversation", json={"message": "Hello"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "llm_provider_error"
    assert expected_log in caplog.text
    assert "Hello" not in caplog.text


def test_structured_json_validation_stays_strict_and_logs_only_field_location(caplog, monkeypatch):
    def response_for(url, *, json, headers, timeout):
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": '{"unexpected_private_text":"secret"}'}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", response_for)
    provider = OpenAICompatibleProvider(base_url="http://localhost:11434/v1", model="qwen3.5:4b")
    with pytest.raises(LLMProviderError, match="required response schema"):
        provider.generate_json(system="system", prompt="private prompt", response_model=ConversationReply)
    assert "response_schema_validation_failed" in caplog.text
    assert "reply" in caplog.text
    assert "secret" not in caplog.text
    assert "private prompt" not in caplog.text


@pytest.mark.parametrize(
    ("content", "expected_safe_error"),
    [
        ('{"missing_information":["Which period?"]}', "steps"),
        ('{"steps":[{"step_id":"s1","tool_name":"document.search"}],"missing_information":[]}', "steps"),
        ('{"steps":[],"missing_information":[{"field":"period"}]}', "missing_information"),
    ],
)
def test_invalid_plan_draft_shapes_raise_safe_format_error_and_log_no_values(
    content, expected_safe_error, caplog, monkeypatch
):
    def response_for(url, *, json, headers, timeout):
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", response_for)
    provider = OpenAICompatibleProvider(base_url="http://localhost:11434/v1", model="qwen3.5:4b")
    with pytest.raises(LLMProviderError, match="required response schema"):
        provider.generate_json(
            system="synthetic planner schema",
            prompt="synthetic task only",
            response_model=PlanDraft,
        )
    assert "response_schema_validation_failed" in caplog.text
    assert expected_safe_error in caplog.text
    assert "field_errors=" in caplog.text
    assert "period" not in caplog.text
    assert "field\":\"period" not in caplog.text


def test_malformed_structured_json_is_explicitly_classified_and_not_logged(caplog, monkeypatch):
    private_fragment = '{"missing_information":["synthetic private detail"'

    def response_for(url, *, json, headers, timeout):
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": private_fragment}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", response_for)
    provider = OpenAICompatibleProvider(base_url="http://localhost:11434/v1", model="qwen3.5:4b")
    with pytest.raises(LLMProviderError, match="not valid JSON"):
        provider.generate_json(
            system="synthetic planner prompt", prompt="synthetic task", response_model=PlanDraft
        )
    assert "invalid_json_content" in caplog.text
    assert "synthetic private detail" not in caplog.text
