from __future__ import annotations

import socket
from uuid import uuid4

import pytest

from backend.app.browser.browserless_controller import (
    BrowserlessController,
    explicit_browserless_hosts,
    normalize_allowed_hosts,
    validate_browserless_endpoint,
    validate_cloud_target,
)
from backend.app.browser.live_events import BrowserLiveEventStore
from backend.app.api.runtime import TaskRuntime
from backend.app.main import create_app
from backend.app.schemas.contracts import TaskCreateRequest, TaskStep
from fastapi.testclient import TestClient


def _public_resolver(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def test_browserless_endpoint_requires_tls_account_route_and_no_embedded_token():
    assert validate_browserless_endpoint("wss://production-sfo.browserless.io/chromium/playwright") == "wss://production-sfo.browserless.io/chromium/playwright"
    for endpoint in (
        "ws://production-sfo.browserless.io/chromium/playwright",
        "wss://production-sfo.browserless.io/chromium/cdp",
        "wss://user:secret@production-sfo.browserless.io/chromium/playwright",
        "wss://production-sfo.browserless.io:444/chromium/playwright",
        "wss://production-sfo.browserless.io/chromium/playwright?token=secret",
        "wss://127.0.0.1/chromium/playwright",
    ):
        with pytest.raises(ValueError):
            validate_browserless_endpoint(endpoint)


def test_allowlist_is_exact_and_cloud_url_rejects_unsafe_shapes():
    hosts = normalize_allowed_hosts("Example.com")
    assert hosts == frozenset({"example.com"})
    assert validate_cloud_target("https://example.com/path", hosts, _public_resolver) == "https://example.com/path"
    assert validate_cloud_target("https://docs.python.org/3/", {"docs.python.org"}, _public_resolver) == "https://docs.python.org/3/"
    for url in (
        "http://example.com/",
        "https://www.example.com/",
        "https://example.com:8443/",
        "https://user@example.com/",
        "https://127.0.0.1/",
    ):
        with pytest.raises(Exception):
            validate_cloud_target(url, hosts, _public_resolver)


def test_explicit_domain_extraction_is_exact_and_rejects_unsupported_url_schemes():
    assert explicit_browserless_hosts("Research github.com and https://science.nasa.gov/missions.") == {
        "github.com", "science.nasa.gov",
    }
    # File extensions and email domains are not interpreted as destinations.
    assert explicit_browserless_hosts(r"Analyze C:\\Users\\me\\notes.txt and user@example.com") == frozenset()
    with pytest.raises(Exception):
        explicit_browserless_hosts("Open http://example.com")


def test_nasa_and_esa_plan_review_includes_every_exact_navigation_host(monkeypatch):
    monkeypatch.setenv("UNBOUND_BROWSER_PROVIDER", "browserless")
    monkeypatch.delenv("UNBOUND_BROWSER_LOCAL_DEMO", raising=False)
    monkeypatch.setenv("BROWSERLESS_API_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("BROWSERLESS_WS_ENDPOINT", "wss://production-sfo.browserless.io/chromium/playwright")
    monkeypatch.setenv("BROWSERLESS_ALLOWED_HOSTS", "example.com")
    runtime = TaskRuntime()
    runtime.initialize_browser_integration()
    steps = [
        TaskStep(step_id="nasa", tool_name="browser.navigate", description="NASA", input={"url": "https://science.nasa.gov/"}, expected_outcome="NASA page"),
        TaskStep(step_id="agency", tool_name="browser.navigate", description="NASA", input={"url": "https://nasa.gov/"}, expected_outcome="NASA page", depends_on=["nasa"]),
        TaskStep(step_id="esa", tool_name="browser.navigate", description="ESA", input={"url": "https://esa.int/"}, expected_outcome="ESA page", depends_on=["agency"]),
    ]
    policy = runtime.browserless_domain_review_policy("Research NASA and ESA developments", steps)
    assert policy["requested_browser_domains"] == ["esa.int", "nasa.gov", "science.nasa.gov"]
    assert policy["baseline_browser_domains"] == ["example.com"]
    runtime.shutdown()


def test_cloud_url_rejects_mixed_and_private_dns_answers():
    def mixed(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443)),
        ]

    with pytest.raises(Exception, match="prohibited"):
        validate_cloud_target("https://example.com", {"example.com"}, mixed)


def test_browserless_route_aborts_unapproved_redirect_or_subresource():
    controller = BrowserlessController(
        api_token="synthetic-test-token",
        ws_endpoint="wss://production-sfo.browserless.io/chromium/playwright",
        allowed_hosts={"example.com"},
        resolver=_public_resolver,
    )

    class Request:
        def __init__(self, url):
            self.url = url

    class Route:
        def __init__(self, url):
            self.request = Request(url)
            self.action = None

        def abort(self, reason):
            self.action = ("abort", reason)

        def continue_(self):
            self.action = ("continue", None)

    allowed = Route("https://example.com/image.png")
    denied = Route("https://169.254.169.254/latest/meta-data/")
    redirect = Route("https://attacker.example/redirected")
    controller._guard_cloud_request(allowed)
    controller._guard_cloud_request(denied)
    controller._guard_cloud_request(redirect)
    assert allowed.action == ("continue", None)
    assert denied.action == ("abort", "blockedbyclient")
    assert redirect.action == ("abort", "blockedbyclient")
    assert controller.last_policy_rejection == {
        "category": "destination_policy_rejected",
        "reason": "Destination is outside the HTTPS hostname allowlist",
    }


def test_task_scoped_hosts_add_exact_domain_without_widening_baseline():
    first = BrowserlessController(
        api_token="synthetic-test-token",
        ws_endpoint="wss://production-sfo.browserless.io/chromium/playwright",
        allowed_hosts={"example.com"},
        resolver=_public_resolver,
    )
    second = BrowserlessController(
        api_token="synthetic-test-token",
        ws_endpoint="wss://production-sfo.browserless.io/chromium/playwright",
        allowed_hosts={"example.com"},
        resolver=_public_resolver,
    )
    first.set_task_allowed_hosts({"github.com"})

    class Request:
        def __init__(self, url):
            self.url = url

    class Route:
        def __init__(self, url):
            self.request = Request(url)
            self.action = None

        def abort(self, reason):
            self.action = ("abort", reason)

        def continue_(self):
            self.action = ("continue", None)

    approved = Route("https://github.com/security")
    redirect = Route("https://attacker.example/redirected")
    first._guard_cloud_request(approved)
    first._guard_cloud_request(redirect)
    assert approved.action == ("continue", None)
    assert redirect.action == ("abort", "blockedbyclient")

    unrelated = Route("https://github.com/")
    second._guard_cloud_request(unrelated)
    assert unrelated.action == ("abort", "blockedbyclient")


def test_policy_denial_and_browserless_provider_failure_are_distinguished(monkeypatch):
    from backend.app.browser.tools import BrowserToolAdapter

    controller = BrowserlessController(
        api_token="synthetic-test-token",
        ws_endpoint="wss://production-sfo.browserless.io/chromium/playwright",
        allowed_hosts={"example.com"},
        resolver=_public_resolver,
    )
    controller._last_policy_rejection = {
        "category": "destination_policy_rejected",
        "reason": "Destination is outside the HTTPS hostname allowlist",
    }
    denied = BrowserToolAdapter._browserless_navigation_failure(controller)
    assert denied.data["failure_code"] == "browserless_destination_rejected"
    assert "outside the HTTPS hostname allowlist" in denied.error

    controller._last_policy_rejection = None
    provider_failure = BrowserToolAdapter._browserless_navigation_failure(controller)
    assert provider_failure.data["failure_code"] == "browserless_provider_or_transport_failure"
    assert "no application policy rejection was recorded" in provider_failure.error


def test_run_plan_without_a_live_task_grant_has_a_precise_audit_reason(monkeypatch):
    from backend.app.browser.tools import BrowserNavigateInput
    from backend.app.schemas.contracts import ToolCall
    from backend.app.schemas.contracts import TaskStatus

    monkeypatch.setenv("UNBOUND_BROWSER_PROVIDER", "browserless")
    monkeypatch.delenv("UNBOUND_BROWSER_LOCAL_DEMO", raising=False)
    monkeypatch.setenv("BROWSERLESS_API_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("BROWSERLESS_WS_ENDPOINT", "wss://production-sfo.browserless.io/chromium/playwright")
    monkeypatch.setenv("BROWSERLESS_ALLOWED_HOSTS", "example.com")
    runtime = TaskRuntime()
    runtime.initialize_browser_integration()
    step = TaskStep(
        step_id="open-github", tool_name="browser.navigate", description="Open GitHub",
        input={"url": "https://github.com/features"}, expected_outcome="Observe the approved page",
    )
    review = runtime.browserless_domain_review_policy("Open https://github.com/features", [step])
    task = runtime.manager.create(
        TaskCreateRequest(user_request="Open https://github.com/features"), [step], browser_domain_policy=review,
    )
    call = ToolCall(task_id=task.task_id, step_id="open-github", tool_name="browser.navigate", arguments={"url": "https://github.com/features"})
    guarded = runtime._browser_execution_gate("browser.navigate", lambda *_: pytest.fail("must not dispatch without a grant"))
    result = guarded(BrowserNavigateInput(url="https://github.com/features"), call)

    assert task.status == TaskStatus.CREATED
    assert result.data["failure_code"] == "browserless_authorization_rejected"
    assert result.error == "No active task-scoped website authorization"
    events = runtime.manager.events(task.task_id)
    rejection = next(event for event in events if event.event_type == "browser.navigation_authorization_rejected")
    assert rejection.error_category == "task_authorization_rejected"
    assert rejection.diagnostic["reason"] == result.error
    runtime.shutdown()


@pytest.mark.parametrize("url", [
    "https://localhost/", "https://127.0.0.1/", "https://[::1]/",
    "https://user:pass@example.com/", "http://github.com/",
    "https://github.com:8443/", "https://github.com.evil.example/",
    "https://*.github.com/", "https://169.254.169.254/latest/meta-data/",
])
def test_dynamic_hosts_do_not_weaken_unsafe_destination_rejection(url):
    with pytest.raises(Exception):
        validate_cloud_target(url, {"example.com", "github.com"}, _public_resolver)


def test_run_plan_persists_task_scoped_domain_grant_and_checks_dns(monkeypatch):
    import backend.app.browser.browserless_controller as policy

    monkeypatch.setenv("UNBOUND_BROWSER_PROVIDER", "browserless")
    monkeypatch.delenv("UNBOUND_BROWSER_LOCAL_DEMO", raising=False)
    monkeypatch.setenv("BROWSERLESS_API_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("BROWSERLESS_WS_ENDPOINT", "wss://production-sfo.browserless.io/chromium/playwright")
    monkeypatch.setenv("BROWSERLESS_ALLOWED_HOSTS", "example.com,docs.python.org")
    monkeypatch.setattr(policy.socket, "getaddrinfo", _public_resolver)
    runtime = TaskRuntime()
    runtime.initialize_browser_integration()
    step = TaskStep(
        step_id="open-github", tool_name="browser.navigate", description="Open GitHub",
        input={"url": "https://github.com/"}, expected_outcome="Observe the approved page",
    )
    review = runtime.browserless_domain_review_policy("Research github.com", [step])
    task = runtime.manager.create(
        TaskCreateRequest(user_request="Research github.com"), [step], browser_domain_policy=review,
    )
    runtime.record_browserless_domain_review(task)
    assert task.baseline_browser_domains == ["docs.python.org", "example.com"]
    assert task.requested_browser_domains == ["github.com"]
    assert task.authorized_browser_domains == []
    assert task.browser_domain_authorization_status.value == "awaiting_run_plan"

    # The API's explicit Run plan action is the only point that grants access.
    with TestClient(create_app(runtime)) as client:
        runtime.executor.run = lambda task_id: runtime.manager.get(task_id)
        response = client.post(f"/api/tasks/{task.task_id}/execute")
        assert response.status_code == 200
        body = response.json()
        assert body["authorized_browser_domains"] == ["github.com"]
        assert body["browser_domain_authorization_status"] == "granted"
        events = client.get(f"/api/tasks/{task.task_id}/events").json()
        grant = next(item for item in events if item["event_type"] == "browser.domains_authorized")
        assert grant["diagnostic"]["task_specific_hosts"] == ["github.com"]
        assert grant["diagnostic"]["enforcement"] == "browserless_application_request_interception"
        assert "synthetic-test-token" not in str(body)
    runtime.shutdown()


def test_domain_preflight_denies_private_dns_without_dispatch(monkeypatch):
    import backend.app.browser.browserless_controller as policy

    def private_resolver(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", 443))]

    monkeypatch.setenv("UNBOUND_BROWSER_PROVIDER", "browserless")
    monkeypatch.delenv("UNBOUND_BROWSER_LOCAL_DEMO", raising=False)
    monkeypatch.setenv("BROWSERLESS_API_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("BROWSERLESS_WS_ENDPOINT", "wss://production-sfo.browserless.io/chromium/playwright")
    monkeypatch.setenv("BROWSERLESS_ALLOWED_HOSTS", "example.com")
    monkeypatch.setattr(policy.socket, "getaddrinfo", private_resolver)
    runtime = TaskRuntime()
    runtime.initialize_browser_integration()
    step = TaskStep(
        step_id="open-github", tool_name="browser.navigate", description="Open GitHub",
        input={"url": "https://github.com/"}, expected_outcome="Observe page",
    )
    task = runtime.manager.create(
        TaskCreateRequest(user_request="Open github.com"), [step],
        browser_domain_policy=runtime.browserless_domain_review_policy("Open github.com", [step]),
    )
    with TestClient(create_app(runtime)) as client:
        dispatched = []
        runtime.executor.run = lambda task_id: dispatched.append(task_id)
        response = client.post(f"/api/tasks/{task.task_id}/execute")
        assert response.status_code == 200
        body = response.json()
        assert body["browser_domain_authorization_status"] == "denied"
        assert body["authorized_browser_domains"] == []
        assert body["status"] == "needs_attention"
        assert dispatched == []
    runtime.shutdown()


def test_task_domain_grant_expires_at_terminal_status_and_does_not_transfer(tmp_path):
    from backend.app.orchestrator.task_manager import SQLiteTaskRepository, TaskManager
    from backend.app.schemas.contracts import BrowserDomainAuthorizationStatus, TaskStatus

    manager = TaskManager(SQLiteTaskRepository(tmp_path / "tasks.sqlite3"))
    first_step = TaskStep(
        step_id="first", tool_name="browser.navigate", description="Open GitHub",
        input={"url": "https://github.com/"}, expected_outcome="Observe page",
    )
    first = manager.create(
        TaskCreateRequest(user_request="Open github.com"), [first_step],
        browser_domain_policy={
            "baseline_browser_domains": ["example.com"],
            "requested_browser_domains": ["github.com"],
            "browser_domain_authorization_status": BrowserDomainAuthorizationStatus.AWAITING_RUN_PLAN,
        },
    )
    manager.authorize_browser_domains(first.task_id, baseline=["example.com"], requested=["github.com"])
    task = manager.get_for_execution(first.task_id)
    task.status = TaskStatus.COMPLETED
    manager.save(task)
    expired = manager.get(first.task_id)
    assert expired.browser_domain_authorization_status == BrowserDomainAuthorizationStatus.EXPIRED
    assert expired.authorized_browser_domains == ["github.com"]  # retained as audit history
    assert any(e.event_type == "browser.domains_authorization_expired" for e in manager.events(first.task_id))

    second = manager.create(
        TaskCreateRequest(user_request="Open a new page"),
        [TaskStep(step_id="second", tool_name="browser.navigate", description="Open", input={"url": "https://example.com/"}, expected_outcome="Observe")],
    )
    assert manager.get_for_execution(second.task_id).authorized_browser_domains == []

    cancelled = manager.create(
        TaskCreateRequest(user_request="Open github.com then cancel"), [first_step],
        browser_domain_policy={
            "baseline_browser_domains": ["example.com"],
            "requested_browser_domains": ["github.com"],
            "browser_domain_authorization_status": BrowserDomainAuthorizationStatus.AWAITING_RUN_PLAN,
        },
    )
    manager.authorize_browser_domains(cancelled.task_id, baseline=["example.com"], requested=["github.com"])
    manager.cancel(cancelled.task_id)
    assert manager.get(cancelled.task_id).browser_domain_authorization_status == BrowserDomainAuthorizationStatus.EXPIRED


def test_active_browserless_grant_is_bound_to_task_and_step(monkeypatch):
    from backend.app.schemas.contracts import TaskStatus, ToolCall
    import backend.app.browser.browserless_controller as policy

    monkeypatch.setenv("UNBOUND_BROWSER_PROVIDER", "browserless")
    monkeypatch.delenv("UNBOUND_BROWSER_LOCAL_DEMO", raising=False)
    monkeypatch.setenv("BROWSERLESS_API_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("BROWSERLESS_WS_ENDPOINT", "wss://production-sfo.browserless.io/chromium/playwright")
    monkeypatch.setenv("BROWSERLESS_ALLOWED_HOSTS", "example.com")
    monkeypatch.setattr(policy.socket, "getaddrinfo", _public_resolver)
    runtime = TaskRuntime()
    runtime.initialize_browser_integration()
    step = TaskStep(
        step_id="open", tool_name="browser.navigate", description="Open GitHub",
        input={"url": "https://github.com/"}, expected_outcome="Observe the page",
    )
    review = runtime.browserless_domain_review_policy("Open github.com", [step])
    first = runtime.manager.create(TaskCreateRequest(user_request="Open github.com"), [step], browser_domain_policy=review)
    runtime.manager.authorize_browser_domains(first.task_id, baseline=["example.com"], requested=["github.com"])
    active = runtime.manager.get_for_execution(first.task_id)
    active.status = TaskStatus.EXECUTING
    active.current_step = "open"
    runtime.manager.save_for_execution(active)

    call = ToolCall(task_id=first.task_id, step_id="open", tool_name="browser.navigate", arguments={"url": "https://github.com/"})
    assert runtime.browserless_hosts_for_active_task(call) == frozenset({"example.com", "github.com"})
    wrong_step = ToolCall(task_id=first.task_id, step_id="other", tool_name="browser.navigate", arguments={"url": "https://github.com/"})
    with pytest.raises(ValueError, match="active task"):
        runtime.browserless_hosts_for_active_task(wrong_step)

    other_step = TaskStep(
        step_id="other", tool_name="browser.navigate", description="Open example",
        input={"url": "https://example.com/"}, expected_outcome="Observe the page",
    )
    second_policy = runtime.browserless_domain_review_policy("Open example.com", [other_step])
    second = runtime.manager.create(TaskCreateRequest(user_request="Open example.com"), [other_step], browser_domain_policy=second_policy)
    second_call = ToolCall(task_id=second.task_id, step_id="other", tool_name="browser.navigate", arguments={"url": "https://example.com/"})
    with pytest.raises(ValueError, match="active task"):
        runtime.browserless_hosts_for_active_task(second_call)
    runtime.shutdown()


def test_live_event_store_is_task_scoped_bounded_and_sanitizes_urls():
    store = BrowserLiveEventStore(max_events_per_task=5, max_tasks=2)
    task_a, task_b = uuid4(), uuid4()
    shot = b"\x89PNG\r\n\x1a\n" + b"synthetic-frame"
    first = store.publish(
        task_a, "screenshot_updated", "Frame captured", session_id=uuid4(),
        url="https://example.com/path?token=must-not-leak#fragment", title="Example Domain",
        provider="browserless_cloud_demo", screenshot_png=shot,
    )
    assert first["url"] == "https://example.com/path"
    assert "token" not in first["url"]
    assert first["screenshot"]["sha256"]
    assert store.after(task_b) == []
    assert [item["sequence"] for item in store.after(task_a)] == [1]
    store.publish(task_a, "page_loaded", "Loaded")
    assert len(store.after(task_a, 1)) == 1
    with pytest.raises(ValueError):
        store.publish(task_a, "screenshot_updated", "oversize", screenshot_png=b"\x89PNG\r\n\x1a\n" + b"x" * (512 * 1024))


def test_browserless_connect_does_not_leak_token_when_connection_fails(monkeypatch):
    captured = {}

    class Chromium:
        def connect(self, endpoint, timeout):
            captured["endpoint"] = endpoint
            raise RuntimeError(f"failed at {endpoint}")

    class Playwright:
        chromium = Chromium()

        def stop(self):
            pass

    class PlaywrightContext:
        def start(self):
            return Playwright()

    monkeypatch.setattr(BrowserlessController, "_load_playwright", staticmethod(lambda: lambda: PlaywrightContext()))
    statuses = []
    token = "synthetic-secret-value"
    controller = BrowserlessController(
        api_token=token,
        ws_endpoint="wss://production-sfo.browserless.io/chromium/playwright",
        on_connection_status=statuses.append,
    )
    with pytest.raises(Exception) as error:
        controller.start()
    assert token not in str(error.value)
    assert token in captured["endpoint"]
    assert statuses[-1] == "failed"


def test_browserless_native_session_uses_isolated_context_guards_and_closes(monkeypatch):
    calls = {}
    image = b"\x89PNG\r\n\x1a\n" + b"bounded-real-browser-frame"

    class Page:
        url = "about:blank"

        def set_default_timeout(self, timeout):
            calls["timeout"] = timeout

        def goto(self, url, wait_until, timeout):
            calls["navigation"] = (url, wait_until, timeout)
            self.url = "https://example.com/"

        def title(self):
            return "Example Domain"

        def screenshot(self, **_kwargs):
            return image

        def evaluate(self, _expression):
            states = calls.setdefault("ready_states", ["interactive", "complete"])
            return states.pop(0) if states else "complete"

        def wait_for_timeout(self, milliseconds):
            calls.setdefault("waits", []).append(milliseconds)

        def wait_for_load_state(self, state, timeout):
            calls["load_state"] = (state, timeout)

        def locator(self, _selector):
            return self

        def inner_text(self, **_kwargs):
            return "Example Domain visible text"

    class Context:
        def route(self, pattern, handler):
            calls["route"] = pattern
            calls["request_handler"] = handler

        def route_web_socket(self, pattern, handler):
            calls["websocket_route"] = pattern
            calls["websocket_handler"] = handler

        def add_init_script(self, script):
            calls["init_script"] = script

        def new_page(self):
            return Page()

        def close(self):
            calls["context_closed"] = True

    class Browser:
        def new_context(self, **kwargs):
            calls["context_options"] = kwargs
            return Context()

        def close(self):
            calls["browser_closed"] = True

    class Chromium:
        def connect(self, endpoint, timeout):
            calls["connect_timeout"] = timeout
            calls["endpoint"] = endpoint
            return Browser()

    class Playwright:
        chromium = Chromium()

        def stop(self):
            calls["playwright_stopped"] = True

    class PlaywrightContext:
        def start(self):
            return Playwright()

    monkeypatch.setattr(BrowserlessController, "_load_playwright", staticmethod(lambda: lambda: PlaywrightContext()))
    controller = BrowserlessController(
        api_token="synthetic + token",
        ws_endpoint="wss://production-sfo.browserless.io/chromium/playwright",
        allowed_hosts={"example.com"},
        resolver=_public_resolver,
    )
    controller.start()
    assert "token=synthetic+%2B+token" in calls["endpoint"]
    assert calls["context_options"] == {"service_workers": "block", "accept_downloads": False}
    assert calls["websocket_route"] == "**/*"
    assert "RTCPeerConnection" in calls["init_script"]
    frames = []
    assert controller.navigate_with_screenshots("https://example.com", frames.append) == "https://example.com/"
    assert frames == [image, image]
    assert calls["waits"] == [500]
    assert controller.title() == "Example Domain"
    assert controller.visible_content() == "Example Domain visible text"
    assert controller.screenshot_png() == image
    controller.close()
    assert calls["context_closed"] and calls["browser_closed"] and calls["playwright_stopped"]


def test_browserless_runtime_is_explicit_and_live_events_are_task_scoped(monkeypatch):
    monkeypatch.setenv("UNBOUND_BROWSER_PROVIDER", "browserless")
    monkeypatch.delenv("UNBOUND_BROWSER_LOCAL_DEMO", raising=False)
    monkeypatch.setenv("BROWSERLESS_API_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("BROWSERLESS_WS_ENDPOINT", "wss://production-sfo.browserless.io/chromium/playwright")
    monkeypatch.setenv("BROWSERLESS_ALLOWED_HOSTS", "example.com")
    runtime = TaskRuntime()
    step = TaskStep(
        step_id="open", tool_name="browser.navigate", description="Open page",
        input={"url": "https://example.com/"}, expected_outcome="Page is observed",
    )
    task = runtime.manager.create(TaskCreateRequest(user_request="Open an approved page"), [step])
    with TestClient(create_app(runtime)) as client:
        health = client.get("/api/health").json()
        cloud = health["browserless_cloud_demo"]
        assert cloud["selected"] is True
        assert cloud["configured"] is True
        assert cloud["connection_status"] == "not_checked"
        assert cloud["execution_configured"] is True
        assert cloud["execution_available"] is False
        assert "synthetic-test-token" not in str(health)
        assert health["browser_public_execution_enabled"] is False
        assert health["browser_worker"]["readiness_gates"]["D"] == "FAIL"
        assert health["browser_worker"]["readiness_gates"]["E"] == "FAIL"

        runtime.browser_live_events.publish(task.task_id, "browser_session_created", "Browser session created", session_id=uuid4(), provider="browserless_cloud_demo")
        runtime.browser_live_events.publish(task.task_id, "page_loaded", "Page loaded", url="https://example.com/", title="Example Domain")
        first = client.get(f"/api/tasks/{task.task_id}/browser-events?after=0").json()["events"]
        assert [event["event_type"] for event in first] == ["browser_session_created", "page_loaded"]
        assert first[0]["task_id"] == str(task.task_id)
        assert client.get(f"/api/tasks/{uuid4()}/browser-events").status_code == 404
    runtime.shutdown()
