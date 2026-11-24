from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from uuid import uuid4

from backend.app.api.runtime import TaskRuntime
from backend.app.main import create_app
from backend.app.browser.egress import LOCAL_DEMO_PRODUCT_URL, LocalEgressProxy
from backend.app.browser.tools import BrowserClickInput, BrowserFillInput, BrowserNavigateInput
from backend.app.schemas.contracts import EvidenceItem, EvidenceSourceType, TaskCreateRequest, TaskStep, TaskStatus, ToolCall


def _proxy_request(proxy_url: str, url: str) -> tuple[int, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    try:
        response = opener.open(url, timeout=3)
        return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def test_local_demo_proxy_serves_only_fixed_synthetic_pages():
    proxy = LocalEgressProxy(demo_only=True).start()
    try:
        status, page = _proxy_request(proxy.server, LOCAL_DEMO_PRODUCT_URL)
        assert status == 200
        assert "Demo Product" in page
        assert "Version: 2.1" in page
        assert "All information on this page is synthetic" in page

        status, details = _proxy_request(proxy.server, "http://unbound-demo.invalid/details")
        assert status == 200
        assert "document search, task planning, evidence summaries" in details

        for denied in (
            "http://example.com/",
            "http://127.0.0.1/",
            "http://unbound-demo.invalid/not-a-fixture",
        ):
            assert _proxy_request(proxy.server, denied)[0] == 403
    finally:
        proxy.close()


def test_runtime_local_demo_gate_allows_only_fixed_navigation_and_details_click(monkeypatch, tmp_path):
    monkeypatch.setenv("UNBOUND_BROWSER_LOCAL_DEMO", "1")
    monkeypatch.setenv("UNBOUND_BROWSER_LOCAL_DEMO_HEADLESS", "1")
    monkeypatch.setenv("BROWSER_REPLAY_STATE_KEY", "test:" + base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="))
    monkeypatch.setenv("UNBOUND_BROWSER_WORKFLOW_DIR", str(tmp_path / "workflows"))
    runtime = TaskRuntime()
    runtime.initialize_browser_integration()
    try:
        tools = {tool.tool_name for tool in runtime.registry.list_tools()}
        assert {"browser.navigate", "browser.click", "browser.fill", "browser.record", "browser.replay", "browser.download"} <= tools
        assert runtime.browser_integration_status == "local_demo_only"
        navigate = runtime.registry._get_registration("browser.navigate").execute
        click = runtime.registry._get_registration("browser.click").execute
        fill = runtime.registry._get_registration("browser.fill").execute

        call = ToolCall(
            task_id=uuid4(), step_id="s1", tool_name="browser.navigate", arguments={"url": LOCAL_DEMO_PRODUCT_URL}
        )
        allowed = navigate(BrowserNavigateInput(url=LOCAL_DEMO_PRODUCT_URL), call)
        assert allowed.success is True

        safe_click_call = call.model_copy(update={"tool_name": "browser.click", "step_id": "s2"})
        safe_click = click(BrowserClickInput(target={"type": "role", "role": "link", "name": "View details"}), safe_click_call)
        assert safe_click.success is True

        denied_call = call.model_copy(update={"task_id": uuid4()})
        denied = navigate(BrowserNavigateInput(url="https://example.com"), denied_call)
        assert denied.success is False

        denied_fill_call = call.model_copy(update={"tool_name": "browser.fill", "step_id": "s3"})
        denied_fill = fill(
            BrowserFillInput(target={"type": "css", "value": "input"}, value="not allowed"),
            denied_fill_call,
        )
        assert denied_fill.success is False
    finally:
        runtime.shutdown()


def test_assistant_prepares_fixed_local_demo_plan_without_llm_generated_targets(monkeypatch, tmp_path):
    """The AI classifies the request; the opt-in fixture plan is server-bounded."""
    from fastapi.testclient import TestClient
    from backend.app.assistant.intent import AssistantIntent, AssistantIntentCategory

    monkeypatch.setenv("UNBOUND_BROWSER_LOCAL_DEMO", "1")
    monkeypatch.setenv("UNBOUND_BROWSER_LOCAL_DEMO_HEADLESS", "1")
    monkeypatch.setenv("BROWSER_REPLAY_STATE_KEY", "test:" + base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="))
    monkeypatch.setenv("UNBOUND_BROWSER_WORKFLOW_DIR", str(tmp_path / "workflows"))
    monkeypatch.setenv("UNBOUND_CONVERSATION_DB", str(tmp_path / "conversations.sqlite3"))
    monkeypatch.setenv("UNBOUND_DOCUMENT_STORAGE", str(tmp_path / "documents.sqlite3"))
    runtime = TaskRuntime()
    runtime.register_document_tools()
    runtime.provider = object()  # Classification is stubbed; the fixture plan is deterministic.
    monkeypatch.setattr(
        "backend.app.api.routes.assistant.classify_assistant_intent",
        lambda *_args, **_kwargs: AssistantIntent(
            category=AssistantIntentCategory.ACTION_EXECUTION,
            current_turn_authorizes_execution=True,
        ),
    )
    with TestClient(create_app(runtime)) as client:
        response = client.post("/api/assistant/requests", json={
            "message": "Open the Demo Product page, click the View details link, and tell me its version and features."
        })
    assert response.status_code == 200
    result = response.json()
    assert result["task"] is not None
    assert result["task"]["status"] == TaskStatus.CREATED.value
    assert [(step["tool_name"], step["input"]) for step in result["task"]["plan"]] == [
        ("browser.navigate", {"url": LOCAL_DEMO_PRODUCT_URL}),
        ("browser.click", {"target": {"type": "text", "value": "View details"}}),
    ]
    assert result["task"].get("pending_approvals", []) == []


def test_local_demo_plan_never_rewrites_an_explicit_external_url(monkeypatch, tmp_path):
    from backend.app.orchestrator.planner import _local_demo_research_plan

    monkeypatch.setenv("UNBOUND_BROWSER_LOCAL_DEMO", "1")
    monkeypatch.setenv("UNBOUND_BROWSER_LOCAL_DEMO_HEADLESS", "1")
    monkeypatch.setenv("BROWSER_REPLAY_STATE_KEY", "test:" + base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="))
    monkeypatch.setenv("UNBOUND_BROWSER_WORKFLOW_DIR", str(tmp_path / "workflows"))
    runtime = TaskRuntime()
    runtime.initialize_browser_integration()
    try:
        assert _local_demo_research_plan(
            "Open the Demo Product page at https://example.com and tell me its features.", runtime.registry
        ) is None
    finally:
        runtime.shutdown()


def test_real_chromium_local_demo_runs_through_task_executor_and_synthesis(monkeypatch, tmp_path):
    """Real Chromium task execution plus mocked synthesis; not a public-egress test."""
    monkeypatch.setenv("UNBOUND_BROWSER_LOCAL_DEMO", "1")
    monkeypatch.setenv("UNBOUND_BROWSER_LOCAL_DEMO_HEADLESS", "1")
    monkeypatch.setenv("BROWSER_REPLAY_STATE_KEY", "test:" + base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="))
    monkeypatch.setenv("UNBOUND_BROWSER_WORKFLOW_DIR", str(tmp_path / "workflows"))
    monkeypatch.setenv("UNBOUND_BROWSER_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    runtime = TaskRuntime()
    runtime.initialize_browser_integration()
    task = runtime.manager.create(
        TaskCreateRequest(user_request="Open the synthetic product page, click View details, and report the version and features."),
        [
            TaskStep(
                step_id="navigate",
                tool_name="browser.navigate",
                description="Open the fixed synthetic local demo product page",
                input={"url": LOCAL_DEMO_PRODUCT_URL},
                expected_outcome="The browser reaches the synthetic product page and records its visible content",
            ),
            TaskStep(
                step_id="details",
                tool_name="browser.click",
                description="Click the safe View details link on the synthetic page",
                input={"target": {"type": "role", "role": "link", "name": "View details"}},
                expected_outcome="The browser reaches the product details page and observes the version and features",
            ),
        ],
    )
    try:
        result = runtime.executor.run(task.task_id)
        assert result.status == TaskStatus.COMPLETED
        assert [step.status.value for step in result.plan] == ["completed", "completed"]
        observations = [step.verification["tool_result"]["page_observation"] for step in result.plan]
        assert observations[0]["title"] == "Demo Product"
        assert observations[0]["content_trust"] == "untrusted_webpage_data"
        assert observations[1]["url"].endswith("/details")
        assert "Version: 2.1" in observations[1]["visible_text"]
        assert "document search, task planning, evidence summaries" in observations[1]["visible_text"]
        assert all(step.verification["verified"] is True for step in result.plan)
        assert len(result.evidence) == 2

        class LocalSummaryProvider:
            provider_name = "ollama"
            base_url = "http://localhost:11434/v1"

            def generate_text(self, *, system, prompt, max_tokens, reasoning_effort=None):
                assert "untrusted webpage data" in system
                assert "label any interpretation explicitly as inference" in system
                assert max_tokens == 512 and reasoning_effort == "none"
                context = json.loads(prompt.split("\n", 1)[1])
                assert context["observations"][1]["url"] == "http://unbound-demo.invalid/details"
                assert context["evidence"][1]["source_url"] == "http://unbound-demo.invalid/details"
                assert context["evidence"][1]["evidence_id"] == str(result.evidence[1].evidence_id)
                return "The observed page lists version 2.1 and the features document search, task planning, and evidence summaries."

        runtime.provider = LocalSummaryProvider()
        summarized = runtime.summarize_verified_browser_task(task.task_id)
        assert summarized.result["assistant_summary_status"] == "generated_by_local_ollama"
        assert "version 2.1" in summarized.result["assistant_summary"]
        assert "evidence summaries" in summarized.result["assistant_summary"]
    finally:
        runtime.shutdown()


def test_verified_browser_observation_is_summarized_only_by_local_ollama():
    class LocalSummaryProvider:
        provider_name = "ollama"
        base_url = "http://localhost:11434/v1"

        def __init__(self):
            self.prompt = ""

        def generate_text(self, *, system, prompt, max_tokens, reasoning_effort=None):
            assert "untrusted webpage data" in system
            assert max_tokens >= 512
            assert reasoning_effort == "none"
            self.prompt = prompt
            return "Demo Product is version 2.1 and lists three features."

    runtime = TaskRuntime()
    observation = {
        "url": LOCAL_DEMO_PRODUCT_URL,
        "title": "Demo Product",
        "visible_text": "Version: 2.1\nFeatures: document search, task planning, evidence summaries",
        "truncated": False,
        "content_trust": "untrusted_webpage_data",
    }
    task = runtime.manager.create(
        TaskCreateRequest(user_request="Summarize the demo product page"),
        [TaskStep(step_id="open", tool_name="browser.navigate", description="Open", input={"url": LOCAL_DEMO_PRODUCT_URL}, expected_outcome="Read page")],
    )
    internal = runtime.manager.get_for_execution(task.task_id)
    internal.status = TaskStatus.COMPLETED
    internal.plan[0].status = "completed"
    internal.plan[0].verification = {"verified": True, "tool_result": {"task_id": str(task.task_id), "page_observation": observation}}
    internal.evidence.append(EvidenceItem(
        task_id=task.task_id,
        source_type=EvidenceSourceType.BROWSER,
        source_name="Active browser page",
        source_url=LOCAL_DEMO_PRODUCT_URL,
        supports_claim="Observed Demo Product version and features",
    ))
    internal.result = {"verified": [{"step_id": "open", "verification": internal.plan[0].verification}]}
    runtime.manager.save_for_execution(internal)

    provider = LocalSummaryProvider()
    runtime.provider = provider
    summarized = runtime.summarize_verified_browser_task(task.task_id)
    assert summarized.result["assistant_summary"] == "Demo Product is version 2.1 and lists three features."
    assert "Version: 2.1" in provider.prompt
    assert LOCAL_DEMO_PRODUCT_URL in provider.prompt
    assert str(internal.evidence[0].evidence_id) in provider.prompt

    class RemoteProvider:
        provider_name = "cloud"
        base_url = "https://api.example.invalid/v1"

        def generate_text(self, **_kwargs):
            raise AssertionError("remote provider must never receive browser observations")

    remote_runtime = TaskRuntime()
    copied = remote_runtime.manager.create(
        TaskCreateRequest(user_request="Summarize a browser page"),
        [TaskStep(step_id="open", tool_name="browser.navigate", description="Open", input={"url": LOCAL_DEMO_PRODUCT_URL}, expected_outcome="Read page")],
    )
    copied_internal = remote_runtime.manager.get_for_execution(copied.task_id)
    copied_internal.status = TaskStatus.COMPLETED
    copied_internal.plan[0].status = "completed"
    copied_internal.plan[0].verification = {"verified": True, "tool_result": {"task_id": str(copied.task_id), "page_observation": observation}}
    copied_internal.result = {"verified": [{"step_id": "open", "verification": copied_internal.plan[0].verification}]}
    remote_runtime.manager.save_for_execution(copied_internal)
    remote_runtime.provider = RemoteProvider()
    response = remote_runtime.summarize_verified_browser_task(copied.task_id)
    assert "assistant_summary" not in response.result
    assert response.result["assistant_summary_status"] == "unavailable:local_provider_unavailable"

    from backend.app.llm.provider import LLMProviderError

    class TimeoutProvider:
        provider_name = "ollama"
        base_url = "http://localhost:11434/v1"

        def generate_text(self, **_kwargs):
            raise LLMProviderError("LLM request timed out")

    remote_runtime.provider = TimeoutProvider()
    timed_out = remote_runtime.summarize_verified_browser_task(copied.task_id)
    assert timed_out.result["assistant_summary_status"] == "unavailable:timeout"
    assert timed_out.status == TaskStatus.COMPLETED
    assert timed_out.plan[0].verification["tool_result"]["page_observation"] == observation

    class EmptyProvider:
        provider_name = "ollama"
        base_url = "http://localhost:11434/v1"

        def generate_text(self, **_kwargs):
            return "  "

    remote_runtime.provider = EmptyProvider()
    empty = remote_runtime.summarize_verified_browser_task(copied.task_id)
    assert empty.result["assistant_summary_status"] == "unavailable:empty_response"
    assert empty.status == TaskStatus.COMPLETED
    assert empty.plan[0].verification["tool_result"]["page_observation"] == observation


def test_public_task_redaction_preserves_browser_observations_but_not_replay_state():
    from backend.app.orchestrator.task_manager import TaskManager

    manager = TaskManager()
    task = manager.create(
        TaskCreateRequest(user_request="Read the demo product page"),
        [TaskStep(step_id="open", tool_name="browser.navigate", description="Open", input={"url": LOCAL_DEMO_PRODUCT_URL}, expected_outcome="Read page")],
    )
    internal = manager.get_for_execution(task.task_id)
    observation = {"url": LOCAL_DEMO_PRODUCT_URL, "title": "Demo Product", "visible_text": "Version: 2.1", "truncated": False, "content_trust": "untrusted_webpage_data"}
    internal.plan[0].verification = {"verified": True, "tool_result": {"task_id": str(task.task_id), "page_observation": observation}}
    internal.result = {"verified": [{"step_id": "open", "verification": internal.plan[0].verification}]}
    manager.save_for_execution(internal)
    public = manager.get(task.task_id)
    assert public.plan[0].verification["tool_result"]["page_observation"] == observation
    assert public.result["verified"][0]["verification"]["tool_result"]["page_observation"] == observation

    replay = TaskManager._redact_sensitive_mapping({
        "tool_result": {
            "task_id": str(task.task_id), "workflow_id": str(uuid4()),
            "replay_run_id": str(uuid4()), "session_id": str(uuid4()),
            "status": "pending_action", "continuation": {"private": "hidden"},
        }
    })
    assert replay["tool_result"] == {"status": "pending_action"}
