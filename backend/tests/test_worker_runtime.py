from __future__ import annotations

import base64
import hashlib
import json
from uuid import uuid4

import pytest

from backend.app.browser.worker_dispatch import GatewayObservationVerifier
from backend.app.browser.worker_runtime import (
    GatewayServerHandle,
    WorkerConfigurationError,
    build_gateway_client_tls,
    build_worker_client,
    start_fetch_gateway,
)
from backend.app.schemas.contracts import ToolResult
from workers.ubuntu.generate_dev_identity import generate


def identity_bundle(tmp_path):
    destination = tmp_path / "identity"
    destination.mkdir(mode=0o700)
    generate(destination)
    return destination


def test_worker_identity_builds_a_fixed_host_only_mtls_client(tmp_path):
    client = build_worker_client(identity_bundle(tmp_path))

    assert client.enabled is True
    assert client.worker_id == "unbound-ubuntu-worker"
    assert client.endpoint == "https://192.168.56.10:9443"
    assert client.transport.ssl_context.verify_mode == 2
    assert client.transport.ssl_context.check_hostname is True


def test_worker_identity_expiry_fails_closed(tmp_path):
    directory = identity_bundle(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["expires_at"] = "2000-01-01T00:00:00+00:00"
    (directory / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(WorkerConfigurationError, match="identity validation failed"):
        build_worker_client(directory)


def test_gateway_client_identity_loads_mutual_tls_and_hostname_verification(tmp_path):
    context = build_gateway_client_tls(identity_bundle(tmp_path))

    assert context.verify_mode == 2
    assert context.check_hostname is True
    assert context.minimum_version.name == "TLSv1_2"


def test_fetch_gateway_fails_closed_when_dot_is_not_explicitly_configured(tmp_path, monkeypatch):
    directory = identity_bundle(tmp_path)
    for name in ("UNBOUND_DOT_RESOLVER_IP", "UNBOUND_DOT_TLS_SERVER_NAME", "UNBOUND_DOT_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="DNS-over-TLS resolver IP"):
        start_fetch_gateway(directory)


def test_gateway_handle_shutdown_closes_listener_and_joins_thread():
    events = []

    class Server:
        def shutdown(self):
            events.append("shutdown")

        def server_close(self):
            events.append("server_close")

    class Thread:
        def join(self, *, timeout):
            events.append(("join", timeout))

        def is_alive(self):
            return False

    GatewayServerHandle(server=Server(), thread=Thread()).close()

    assert events == ["shutdown", "server_close", ("join", 5)]


def test_gateway_handle_reports_incomplete_thread_cleanup():
    class Server:
        def shutdown(self):
            pass

        def server_close(self):
            pass

    class Thread:
        def join(self, *, timeout):
            pass

        def is_alive(self):
            return True

    with pytest.raises(RuntimeError, match="did not stop"):
        GatewayServerHandle(server=Server(), thread=Thread()).close()


class FakeGateway:
    def __init__(self, body, *, url="https://example.com/"):
        self.body = body
        self.url = url
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return {
            "url": self.url,
            "status": 200,
            "content_type": "text/html",
            "body_base64": base64.b64encode(self.body).decode("ascii"),
            "sha256": hashlib.sha256(self.body).hexdigest(),
        }


def observation_result(visible_text):
    return ToolResult(
        success=True,
        data={
            "task_id": str(uuid4()),
            "page_observation": {
                "url": "https://example.com/",
                "title": "Example Domain",
                "visible_text": visible_text,
            },
        },
    )


def test_host_observation_verifier_matches_independent_allowlisted_origin_fetch():
    html = b"<html><head><title>Example Domain</title></head><body><h1>Example Domain</h1><p>Reserved for illustrative examples.</p></body></html>"
    gateway = FakeGateway(html)
    verifier = GatewayObservationVerifier(gateway)

    result = observation_result("Example Domain\nReserved for illustrative examples.")

    assert verifier("browser.navigate", "page loaded", result)
    assert gateway.calls == ["https://example.com/"]


def test_host_observation_verifier_rejects_fabricated_worker_text():
    html = b"<title>Example Domain</title><body>Reserved for illustrative examples.</body>"
    verifier = GatewayObservationVerifier(FakeGateway(html))

    assert not verifier("browser.navigate", "page loaded", observation_result("Example Domain\nCost is $9999"))
