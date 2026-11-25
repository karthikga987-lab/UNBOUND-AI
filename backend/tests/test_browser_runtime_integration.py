from __future__ import annotations

import base64
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.api.runtime import TaskRuntime
from backend.app.browser.session_manager import BrowserSessionManager
from backend.app.browser.state_encryption import ACTIVE_KEY_ENV
from backend.app.browser.tools import BrowserToolAdapter
from backend.app.browser.workflow_store import WorkflowStore
from backend.app.documents.search import DocumentSearcher
from backend.app.documents.store import DocumentStore
from backend.app.main import create_app
from backend.app.schemas.contracts import (
    ApprovalRequest,
    TaskCreateRequest,
    TaskStep,
    TaskStatus,
    ToolCall,
)


class FakeController:
    def __init__(self):
        self.is_started = False
        self.closed = 0

    def start(self):
        self.is_started = True
        return self

    def close(self):
        self.closed += 1
        self.is_started = False


def _set_valid_key(monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    monkeypatch.setenv(ACTIVE_KEY_ENV, f"runtime-test:{key}")


def test_application_startup_registers_six_gated_browser_tools_and_preserves_documents(tmp_path, monkeypatch):
    _set_valid_key(monkeypatch)
    monkeypatch.setenv("UNBOUND_DOCUMENT_IMPORT_DIR", str(tmp_path / "imports"))
    runtime = TaskRuntime(
        document_store=DocumentStore(tmp_path / "documents"),
        document_searcher=None,
    )
    runtime.document_searcher = DocumentSearcher(runtime.document_store)
    runtime.register_document_tools()
    document_verifier = lambda _tool, _expected, result: bool(result.success)
    runtime.configure_verifier("document.search", document_verifier)

    with TestClient(create_app(runtime)) as client:
        names = {tool.tool_name for tool in runtime.registry.list_tools()}
        expected_browser = {
            "browser.navigate", "browser.click", "browser.fill",
            "browser.record", "browser.replay", "browser.download",
        }
        assert expected_browser <= names
        assert not any(name.startswith("browser.") and name not in expected_browser for name in names)
        assert {"document.upload", "document.search", "context.retrieve", "context.relate", "document.read_local_file"} <= names
        assert runtime.executor.verifier_for("document.search") is document_verifier
        assert runtime.executor.verifier_for("document.read_local_file") is not None
        assert runtime.browser_integration_status == "disabled_pending_egress_review"
        assert runtime.browser_egress_security_enabled is False
        assert runtime.browser_session_manager is not None
        assert runtime.browser_replay_bridge is not None
        assert runtime.browser_egress_proxy is not None
        assert runtime.browser_egress_proxy.is_running
        status = client.get("/api/health").json()
        assert status["browser_egress_proxy_active"] is True
        assert status["browser_egress_proxy_pid"] != os.getpid()
        assert status["browser_egress_proxy_identity_verified"] is True
        assert status["browser_egress_proxy_validation_working"] is True
        assert status["browser_egress_proxy_endpoint"].startswith("http://127.0.0.1:")
        assert status["browser_egress_proxy_endpoint"] == "http://127.0.0.1:18765"
        assert status["browser_egress_proxy_executable"].endswith("unbound-egress-proxy.exe")
        assert len(status["browser_egress_proxy_sha256"]) == 64
        assert status["browser_chromium_executable"].endswith("chrome.exe")
        assert len(status["browser_chromium_sha256"]) == 64
        assert status["browser_chromium_identity_verified"] is False
        assert status["browser_backend_pid"] == os.getpid()
        assert status["browser_backend_identity_verified"] is True
        assert status["browser_wfp_policy_installed"] is False
        assert status["browser_wfp_policy_verified"] is False
        assert status["browser_wfp_policy_integrity_verified"] is False
        assert status["browser_wfp_policy_ownership_verified"] is False
        assert status["browser_dns_bypass_blocked"] is False
        assert status["browser_udp_quic_bypass_blocked"] is False
        assert status["browser_proxy_outbound_restrictions_verified"] is False
        assert status["browser_proxy_shutdown_fail_closed_verified"] is False
        assert status["browser_process_containment_verified"] is False
        assert status["browser_public_execution_enabled"] is False
        proxy = runtime.browser_egress_proxy

    assert not proxy.is_running


def test_runtime_honors_configured_proxy_port_in_local_demo(tmp_path, monkeypatch):
    import socket

    _set_valid_key(monkeypatch)
    monkeypatch.setenv("UNBOUND_BROWSER_LOCAL_DEMO", "1")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("UNBOUND_BROWSER_EGRESS_PORT", str(port))
    runtime = TaskRuntime()

    with TestClient(create_app(runtime)) as client:
        assert runtime.browser_egress_proxy is not None
        assert runtime.browser_egress_proxy.is_running
        assert runtime.browser_egress_proxy.server.endswith(f":{port}")
        status = client.get("/api/health").json()
        assert status["browser_egress_proxy_active"] is True
        assert status["browser_egress_proxy_endpoint"] == f"http://127.0.0.1:{port}"
        assert status["browser_egress_proxy_pid"] != os.getpid()
        assert status["browser_process_containment_verified"] is False
        assert status["browser_public_execution_enabled"] is False
        proxy = runtime.browser_egress_proxy

    assert not proxy.is_running


def test_shutdown_closes_browser_sessions_and_clears_recorders(tmp_path, monkeypatch):
    _set_valid_key(monkeypatch)
    runtime = TaskRuntime()
    with TestClient(create_app(runtime)):
        sessions = runtime.browser_session_manager
        adapter = runtime.browser_tool_adapter
        controller = FakeController()
        sessions._controller_factory = lambda: controller
        call = ToolCall(
            task_id=uuid4(), step_id="browser", tool_name="browser.navigate",
            arguments={"url": "https://example.test/"},
        )
        sessions.create_session(call)
        adapter._recorders[call.task_id] = (uuid4(), object())
        assert controller.is_started
        assert adapter._recorders

    assert not controller.is_started
    assert controller.closed == 1
    assert sessions.get_session(call.task_id) is None
    assert adapter._recorders == {}


def test_missing_or_invalid_replay_key_registers_only_disabled_tools(tmp_path, monkeypatch):
    monkeypatch.delenv(ACTIVE_KEY_ENV, raising=False)
    runtime = TaskRuntime()
    with TestClient(create_app(runtime)):
        assert runtime.browser_integration_status == "disabled_missing_replay_key"
        names = {tool.tool_name for tool in runtime.registry.list_tools()}
        assert {
            "browser.navigate", "browser.click", "browser.fill",
            "browser.record", "browser.replay", "browser.download",
        } <= names
        assert runtime.browser_replay_state_store is None
        assert runtime.browser_replay_bridge is None

    monkeypatch.setenv(ACTIVE_KEY_ENV, "invalid-key-material")
    invalid_runtime = TaskRuntime()
    with TestClient(create_app(invalid_runtime)):
        assert invalid_runtime.browser_integration_status == "disabled_invalid_replay_configuration"
        assert "invalid-key-material" not in invalid_runtime.browser_integration_detail
        assert invalid_runtime.browser_replay_state_store is None


def test_proxy_startup_failure_keeps_browser_actions_disabled(tmp_path, monkeypatch):
    _set_valid_key(monkeypatch)
    from backend.app.browser.standalone_proxy import StandaloneEgressProxy

    def fail_start(_self):
        raise OSError("synthetic bind failure")

    monkeypatch.setattr(StandaloneEgressProxy, "start", fail_start)
    runtime = TaskRuntime()
    with TestClient(create_app(runtime)):
        assert runtime.browser_integration_status == "disabled_egress_proxy_unavailable"
        assert runtime.browser_egress_proxy is None
        assert runtime.browser_egress_security_enabled is False
        names = {tool.tool_name for tool in runtime.registry.list_tools()}
        assert {
            "browser.navigate", "browser.click", "browser.fill",
            "browser.record", "browser.replay", "browser.download",
        } <= names


def test_protected_mode_never_falls_back_to_user_writable_proxy_or_browser_cache(monkeypatch):
    import backend.app.browser.protected_deployment as protected_deployment

    _set_valid_key(monkeypatch)
    monkeypatch.setenv("UNBOUND_BROWSER_PROTECTED_MODE", "1")
    monkeypatch.setenv("UNBOUND_BROWSER_LOCAL_DEMO", "1")

    def unavailable():
        raise protected_deployment.ProtectedDeploymentError("synthetic missing deployment")

    monkeypatch.setattr(protected_deployment, "verify_protected_deployment", unavailable)
    runtime = TaskRuntime()
    with TestClient(create_app(runtime)) as client:
        status = client.get("/api/health").json()
        assert runtime.browser_deployment_mode == "protected"
        assert runtime.browser_protected_deployment_verified is False
        assert runtime.browser_deployment_acl_verified is False
        assert runtime.browser_deployment_failure_code == "protected_deployment_unverified"
        assert runtime.browser_egress_proxy is None
        assert runtime.browser_integration_status == "disabled_protected_deployment_unverified"
        assert status["browser_protected_deployment_verified"] is False
        assert status["browser_public_execution_enabled"] is False
        assert runtime.browser_egress_security_enabled is False


def test_registered_browser_navigation_gate_never_reaches_controller(tmp_path, monkeypatch):
    _set_valid_key(monkeypatch)
    runtime = TaskRuntime()
    created = []

    def controller_factory():
        controller = FakeController()
        created.append(controller)
        return controller

    with TestClient(create_app(runtime)):
        runtime.browser_session_manager._controller_factory = controller_factory
        runtime.configure_verifier("browser.navigate", lambda *_args: True)
        task = runtime.manager.create(
            TaskCreateRequest(user_request="Navigate to a test page", auto_execute=False),
            [TaskStep(
                step_id="navigate",
                tool_name="browser.navigate",
                description="Navigate to a public test page",
                input={"url": "https://example.test/"},
                expected_outcome="The browser reaches the test page",
            )],
        )
        result = runtime.executor.run(task.task_id)
        assert result.status == TaskStatus.FAILED
        assert created == []


def test_startup_failure_cleans_partial_browser_resources(tmp_path, monkeypatch):
    _set_valid_key(monkeypatch)
    runtime = TaskRuntime()
    sessions = BrowserSessionManager()
    controller = FakeController()
    call = ToolCall(task_id=uuid4(), step_id="browser", tool_name="browser.navigate", arguments={})
    sessions._controller_factory = lambda: controller
    sessions.create_session(call)

    adapter = BrowserToolAdapter(workflow_store=WorkflowStore(tmp_path / "workflows"), session_manager=sessions)
    cleaned = {"value": False}
    original_cleanup = adapter.cleanup_all

    def cleanup_all():
        cleaned["value"] = True
        original_cleanup()

    adapter.cleanup_all = cleanup_all
    monkeypatch.setattr(runtime, "_build_browser_components", lambda: {
        "session_manager": sessions,
        "adapter": adapter,
        "workflow_store": adapter.workflow_store,
        "state_store": None,
        "bridge": None,
        "status": "test",
        "detail": "test",
    })

    def fail_registration(_entries):
        raise RuntimeError("simulated registration failure")

    monkeypatch.setattr(runtime.registry, "register_many", fail_registration)
    with pytest.raises(RuntimeError, match="simulated registration failure"):
        with TestClient(create_app(runtime)):
            pass

    assert not controller.is_started
    assert controller.closed == 1
    assert cleaned["value"] is True
    assert not any(tool.tool_name.startswith("browser.") for tool in runtime.registry.list_tools())
    assert runtime.browser_session_manager is None
    assert runtime.browser_tools_registered is False


def test_public_task_api_redacts_replay_parameters_and_authorization(tmp_path, monkeypatch):
    _set_valid_key(monkeypatch)
    runtime = TaskRuntime()
    task = runtime.manager.create(
        TaskCreateRequest(user_request="Run a saved browser workflow", auto_execute=False),
        [TaskStep(
            step_id="browser_step",
            tool_name="browser.replay",
            description="Replay workflow",
            input={
                "operation": "start",
                "workflow_id": str(uuid4()),
                "parameters": {"account_token": "test-secret", "query": "private query"},
            },
            expected_outcome="Workflow completes",
            requires_approval=True,
        )],
    )
    private = runtime.manager.get_for_execution(task.task_id)
    private.pending_approvals.append(ApprovalRequest(
        task_id=task.task_id,
        step_id="browser_step",
        tool_name="browser.replay",
        description="private internal approval summary",
        arguments={"authorization": "test-secret", "parameters": {"query": "private query"}},
    ))
    runtime.manager.save_for_execution(private)

    client = TestClient(create_app(runtime))
    with client:
        response = client.get(f"/api/tasks/{task.task_id}")
    assert response.status_code == 200
    public_json = response.text
    assert "test-secret" not in public_json
    assert "private query" not in public_json
    payload = response.json()
    assert payload["plan"][0]["input"]["parameters"] == {
        "account_token": "[REDACTED]", "query": "[REDACTED]"
    }
    assert payload["pending_approvals"][0]["arguments"] == {}
    assert payload["pending_approvals"][0]["description"] == "A browser workflow action requires approval."
