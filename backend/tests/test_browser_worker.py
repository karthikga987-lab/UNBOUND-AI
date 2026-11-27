from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.app.browser.worker_protocol import (
    BrowserWorkerClient,
    CloseBrowser,
    ExtractVisible,
    GUEST_POLICY_SHA256,
    GUEST_POLICY_VERSION,
    HealthChallenge,
    GuestPolicyAttestation,
    GuestPolicyChecks,
    Navigate,
    WorkerHealth,
    WorkerProtocolError,
    WorkerRequest,
    WorkerResponse,
    WorkerResult,
    make_worker_request,
    sign_payload,
    validate_worker_endpoint,
    verify_guest_policy_attestation,
    verify_envelope,
)
from backend.app.browser.worker_service import ReplayGuard, WorkerService


def _keypair():
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def _request(*, now=None):
    return make_worker_request(
        worker_id="worker-test", approval_id=uuid4(), tool_name="browser.navigate",
        task_id=uuid4(), step_id="step_1", session_id=uuid4(),
        operation=Navigate(operation="navigate", url="https://example.com/"),
        now=now,
    )


class Executor:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return WorkerResult(success=True, current_url="https://example.com/", title="Synthetic")


class Transport:
    def __init__(self, worker_key):
        self.worker_key = worker_key
        self.calls = []
        self.error = None
        self.response = None

    def request(self, method, path, body, timeout):
        self.calls.append((method, path, body, timeout))
        if self.error:
            raise self.error
        return 200, self.response


def test_request_schema_is_strict_and_browser_content_is_untrusted():
    with pytest.raises(Exception):
        Navigate.model_validate({"operation": "navigate", "url": "javascript:alert(1)"})
    for url in ("http://127.0.0.1/", "http://10.1.2.3/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://0177.0.0.1/", "http://0x7f.0.0.1/", "https://example.com:8443/", "https://printer.local/", "https://example.com/\r\nX-Test: value"):
        with pytest.raises(Exception):
            Navigate.model_validate({"operation": "navigate", "url": url})
    with pytest.raises(Exception):
        Navigate.model_validate({"operation": "navigate", "url": "https://user:pass@example.invalid", "script": "x"})
    assert ExtractVisible(operation="extract_visible", max_chars=100).max_chars == 100
    assert WorkerResult(success=True, visible_text="page").content_trust == "untrusted_webpage_data"
    image = base64.b64encode(b"synthetic-image").decode("ascii")
    assert WorkerResult(success=True, screenshot_base64=image).screenshot_base64 == image
    with pytest.raises(Exception):
        WorkerResult(success=True, screenshot_base64=base64.b64encode(b"x" * (2 * 1024 * 1024 + 1)).decode("ascii"))
    with pytest.raises(Exception):
        WorkerResult(success=True, evidence_id="guest-asserted")


@pytest.mark.parametrize("endpoint", [
    "http://192.168.56.10:9443", "https://127.0.0.1:9443", "https://8.8.8.8:9443",
    "https://192.168.56.1:9443", "https://192.168.56.255:9443", "https://192.168.57.10:9443",
])
def test_worker_endpoint_rejects_non_host_only_or_host_addresses(endpoint):
    with pytest.raises(WorkerProtocolError):
        validate_worker_endpoint(endpoint)


def test_host_only_network_cannot_be_overridden_to_public_or_other_subnet():
    with pytest.raises(TypeError):
        validate_worker_endpoint("https://8.8.8.8:9443", allowed_network="0.0.0.0/0")
    host_private, _ = _keypair()
    _, worker_public = _keypair()
    with pytest.raises(TypeError):
        BrowserWorkerClient(
            endpoint="https://192.168.56.10:9443", host_signing_key=host_private,
            worker_verification_key=worker_public, worker_id="worker-test",
            transport=Transport(None), allowed_network="0.0.0.0/0",
        )


def test_signature_tampering_and_oversized_envelopes_are_rejected():
    private, public = _keypair()
    signed = sign_payload({"operation": "extract_visible", "max_chars": 100}, private)
    altered = signed.replace(b"100", b"101")
    with pytest.raises(WorkerProtocolError, match="signature"):
        verify_envelope(altered, public, max_bytes=1024)
    with pytest.raises(WorkerProtocolError, match="size"):
        verify_envelope(b"x" * 1025, public, max_bytes=1024)


def test_replay_guard_is_atomic_under_concurrent_consumers(tmp_path):
    request_id = uuid4()
    database = tmp_path / "concurrent-replay.sqlite3"

    def consume(_index):
        try:
            ReplayGuard(database).consume(request_id, 200, now=100)
            return "accepted"
        except WorkerProtocolError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(consume, range(8)))
    assert outcomes.count("accepted") == 1
    assert outcomes.count("replay_rejected") == 7


def test_worker_endpoint_accepts_literal_guest_host_only_address():
    assert validate_worker_endpoint("https://192.168.56.10:9443/") == "https://192.168.56.10:9443"


def test_https_transport_requires_tls_certificate_and_hostname_verification():
    from backend.app.browser.worker_protocol import HttpsWorkerTransport
    with pytest.raises(ValueError, match="verify certificates"):
        HttpsWorkerTransport("https://192.168.56.10:9443", ssl._create_unverified_context())
    context = ssl.create_default_context()
    transport = HttpsWorkerTransport("https://192.168.56.10:9443", context)
    with pytest.raises(WorkerProtocolError, match="path"):
        transport.request("POST", "/arbitrary", b"{}", 1)


def test_tls_certificate_failure_is_reported_as_worker_unavailable(monkeypatch):
    from backend.app.browser.worker_protocol import HttpsWorkerTransport
    host_private, _ = _keypair()
    _, worker_public = _keypair()
    transport = HttpsWorkerTransport("https://192.168.56.10:9443", ssl.create_default_context())

    def reject_tls(*_args, **_kwargs):
        raise ssl.SSLCertVerificationError("synthetic invalid certificate")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", reject_tls)
    client = BrowserWorkerClient(
        endpoint="https://192.168.56.10:9443", host_signing_key=host_private,
        worker_verification_key=worker_public, worker_id="worker-test", transport=transport, enabled=True,
    )
    with pytest.raises(WorkerProtocolError, match="unavailable"):
        client.health()


def test_replay_guard_rejects_replay_across_reopen(tmp_path):
    request_id = uuid4()
    guard = ReplayGuard(tmp_path / "replay.sqlite3")
    guard.consume(request_id, 200, now=100)
    reopened = ReplayGuard(tmp_path / "replay.sqlite3")
    with pytest.raises(WorkerProtocolError, match="already consumed"):
        reopened.consume(request_id, 200, now=101)


def test_worker_service_executes_only_signed_unexpired_request_once(tmp_path):
    host_private, host_public = _keypair()
    worker_private, worker_public = _keypair()
    executor = Executor()
    service = WorkerService(
        worker_id="worker-test", host_verification_key=host_public,
        worker_signing_key=worker_private, executor=executor,
        replay_guard=ReplayGuard(tmp_path / "replay.sqlite3"), proxy_available=lambda: True,
    )
    now = int(time.time())
    request = _request(now=now)
    response_body = service.handle_execute(sign_payload(request, host_private), now=now)
    payload = verify_envelope(response_body, worker_public, max_bytes=4 * 1024 * 1024)
    response = WorkerResponse.model_validate(payload)
    assert response.request_id == request.request_id
    assert response.task_id == request.task_id
    assert response.result.success is True
    assert response.result.content_trust == "untrusted_webpage_data"
    assert len(executor.requests) == 1
    with pytest.raises(WorkerProtocolError, match="already consumed"):
        service.handle_execute(sign_payload(request, host_private), now=now)


def test_worker_service_rejects_bad_signature_expired_and_unconfigured_proxy(tmp_path):
    host_private, host_public = _keypair()
    other_private, _ = _keypair()
    worker_private, _ = _keypair()
    executor = Executor()
    service = WorkerService(
        worker_id="worker-test", host_verification_key=host_public,
        worker_signing_key=worker_private, executor=executor,
        replay_guard=ReplayGuard(tmp_path / "replay.sqlite3"), proxy_available=lambda: True,
    )
    now = int(time.time())
    request = _request(now=now)
    with pytest.raises(WorkerProtocolError, match="signature"):
        service.handle_execute(sign_payload(request, other_private), now=now)
    expired = _request(now=now - 120)
    with pytest.raises(WorkerProtocolError, match="expired"):
        service.handle_execute(sign_payload(expired, host_private), now=now)
    unavailable = WorkerService(
        worker_id="worker-test", host_verification_key=host_public,
        worker_signing_key=worker_private, executor=executor,
        replay_guard=ReplayGuard(tmp_path / "replay2.sqlite3"), proxy_configured=False,
    )
    with pytest.raises(WorkerProtocolError, match="proxy"):
        unavailable.handle_execute(sign_payload(request, host_private), now=now)
    assert executor.requests == []


def test_expiry_boundary_rejects_and_close_operation_works_when_proxy_is_down(tmp_path):
    host_private, host_public = _keypair()
    worker_private, worker_public = _keypair()
    executor = Executor()
    service = WorkerService(
        worker_id="worker-test", host_verification_key=host_public,
        worker_signing_key=worker_private, executor=executor,
        replay_guard=ReplayGuard(tmp_path / "replay.sqlite3"), proxy_available=lambda: False,
    )
    now = int(time.time())
    expired = make_worker_request(
        worker_id="worker-test", approval_id=uuid4(), tool_name="browser.navigate",
        task_id=uuid4(), step_id="step_1", session_id=uuid4(),
        operation=Navigate(operation="navigate", url="https://example.com/"),
        now=now - 1, ttl_seconds=1,
    )
    with pytest.raises(WorkerProtocolError, match="expired"):
        service.handle_execute(sign_payload(expired, host_private), now=now)

    close_request = make_worker_request(
        worker_id="worker-test", approval_id=uuid4(), tool_name="browser.navigate",
        task_id=uuid4(), step_id="step_close", session_id=uuid4(),
        operation=CloseBrowser(operation="close"),
        now=now,
    )
    response = WorkerResponse.model_validate(verify_envelope(
        service.handle_execute(sign_payload(close_request, host_private), now=now),
        worker_public, max_bytes=4 * 1024 * 1024,
    ))
    assert response.result.success is True


def test_worker_service_health_requires_authenticated_fresh_challenge(tmp_path):
    host_private, host_public = _keypair()
    worker_private, worker_public = _keypair()
    service = WorkerService(
        worker_id="worker-test", host_verification_key=host_public,
        worker_signing_key=worker_private, executor=Executor(),
        replay_guard=ReplayGuard(tmp_path / "replay.sqlite3"),
        browser_available=lambda: True, proxy_available=lambda: False,
    )
    from backend.app.browser.worker_protocol import HealthChallenge
    now = int(time.time())
    challenge = HealthChallenge(request_id=uuid4(), challenge="x" * 48, issued_at=now)
    body = service.handle_health(sign_payload(challenge, host_private), now=now)
    from backend.app.browser.worker_protocol import WorkerHealth
    response = WorkerHealth.model_validate(verify_envelope(body, worker_public, max_bytes=4 * 1024 * 1024))
    assert response.ready is False
    assert response.proxy_configured is True
    assert response.public_browsing_enabled is False
    with pytest.raises(WorkerProtocolError, match="expired"):
        service.handle_health(sign_payload(challenge, host_private), now=now + 60)


def _valid_guest_policy_attestation(*, now: int, request_id=None, worker_id="worker-test", **overrides):
    check_values = {name: True for name in GuestPolicyChecks.model_fields}
    check_values.update(overrides.pop("checks", {}))
    payload = {
        "request_id": request_id or uuid4(), "worker_id": worker_id, "boot_id": uuid4(),
        "policy_version": GUEST_POLICY_VERSION, "policy_sha256": GUEST_POLICY_SHA256,
        "observed_at": now, "checks": GuestPolicyChecks(**check_values),
    }
    payload.update(overrides)
    return GuestPolicyAttestation(**payload)


def test_guest_policy_attestation_requires_fresh_challenge_bound_all_true_checks():
    now = int(time.time())
    request_id = uuid4()
    report = _valid_guest_policy_attestation(now=now, request_id=request_id)
    health = WorkerHealth(
        request_id=request_id, challenge="challenge-value", worker_id="worker-test",
        ready=True, browser_available=True, proxy_configured=True,
        guest_policy_attestation=report,
    )
    assert verify_guest_policy_attestation(health, expected_worker_id="worker-test", now=now) == (True, "verified")

    assert verify_guest_policy_attestation(
        health, expected_worker_id="worker-test", now=now + 31
    ) == (False, "guest_policy_attestation_stale")
    assert verify_guest_policy_attestation(
        health, expected_worker_id="another-worker", now=now
    ) == (False, "guest_policy_attestation_identity_mismatch")


def test_expected_guest_policy_digest_matches_reviewed_repository_policy():
    policy = Path(__file__).resolve().parents[2] / "workers" / "ubuntu" / "host-only-review.nft"
    normalized = policy.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(normalized).hexdigest() == GUEST_POLICY_SHA256


@pytest.mark.parametrize("case,expected", [
    ("missing", "guest_policy_attestation_missing"),
    ("wrong_request", "guest_policy_attestation_identity_mismatch"),
    ("wrong_policy", "guest_policy_version_mismatch"),
    ("failed_check", "guest_policy_check_failed"),
    ("future", "guest_policy_attestation_stale"),
])
def test_guest_policy_attestation_rejects_missing_mismatched_stale_or_failed(case, expected):
    now = int(time.time())
    report = None
    request_id = uuid4()
    if case != "missing":
        kwargs = {"request_id": request_id}
        if case == "wrong_request":
            kwargs["request_id"] = uuid4()
        if case == "wrong_policy":
            kwargs["policy_sha256"] = "0" * 64
        if case == "failed_check":
            kwargs["checks"] = {"renderer_direct_ipv6_blocked": False}
        if case == "future":
            kwargs["observed_at"] = now + 6
        report = _valid_guest_policy_attestation(now=now, **kwargs)
    health = WorkerHealth(
        request_id=request_id, challenge="challenge-value", worker_id="worker-test",
        ready=True, browser_available=True, proxy_configured=True,
        guest_policy_attestation=report,
    )
    assert verify_guest_policy_attestation(health, expected_worker_id="worker-test", now=now) == (False, expected)


def test_worker_health_attestation_callback_is_signed_and_challenge_bound(tmp_path):
    host_private, host_public = _keypair()
    worker_private, worker_public = _keypair()
    now = int(time.time())
    captured = {}

    def attest(challenge):
        captured["challenge"] = challenge
        return _valid_guest_policy_attestation(now=now, request_id=challenge.request_id)

    service = WorkerService(
        worker_id="worker-test", host_verification_key=host_public,
        worker_signing_key=worker_private, executor=Executor(),
        replay_guard=ReplayGuard(tmp_path / "attested-replay.sqlite3"),
        browser_available=lambda: True, proxy_available=lambda: True,
        guest_policy_attestor=attest,
    )
    challenge = HealthChallenge(request_id=uuid4(), challenge="z" * 48, issued_at=now)
    body = service.handle_health(sign_payload(challenge, host_private), now=now)
    result = WorkerHealth.model_validate(verify_envelope(body, worker_public, max_bytes=4096))
    assert result.guest_policy_attestation.request_id == challenge.request_id
    assert captured["challenge"].request_id == challenge.request_id


def test_proxy_failure_and_worker_crash_do_not_claim_success_or_retry(tmp_path):
    host_private, host_public = _keypair()
    worker_private, worker_public = _keypair()

    class CrashingExecutor:
        def __init__(self):
            self.calls = 0

        def execute(self, _request):
            self.calls += 1
            raise RuntimeError("synthetic crash; never returned to caller")

    crashed = CrashingExecutor()
    service = WorkerService(
        worker_id="worker-test", host_verification_key=host_public,
        worker_signing_key=worker_private, executor=crashed,
        replay_guard=ReplayGuard(tmp_path / "crashed.sqlite3"), proxy_available=lambda: True,
    )
    now = int(time.time())
    request = _request(now=now)
    response = WorkerResponse.model_validate(verify_envelope(
        service.handle_execute(sign_payload(request, host_private), now=now),
        worker_public, max_bytes=4 * 1024 * 1024,
    ))
    assert response.result.success is False
    assert response.result.failure_code == "worker_operation_failed"
    assert crashed.calls == 1
    with pytest.raises(WorkerProtocolError, match="already consumed"):
        service.handle_execute(sign_payload(request, host_private), now=now)

    blocked_executor = Executor()
    proxy_down = WorkerService(
        worker_id="worker-test", host_verification_key=host_public,
        worker_signing_key=worker_private, executor=blocked_executor,
        replay_guard=ReplayGuard(tmp_path / "proxy-down.sqlite3"),
        proxy_available=lambda: False,
    )
    request = _request(now=now)
    with pytest.raises(WorkerProtocolError, match="proxy"):
        proxy_down.handle_execute(sign_payload(request, host_private), now=now)
    assert blocked_executor.requests == []


def test_client_disabled_by_default_never_calls_transport():
    host_private, _ = _keypair()
    _, worker_public = _keypair()
    transport = Transport(None)
    client = BrowserWorkerClient(
        endpoint="https://192.168.56.10:9443", host_signing_key=host_private,
        worker_verification_key=worker_public, worker_id="worker-test", transport=transport,
    )
    with pytest.raises(WorkerProtocolError, match="disabled"):
        client.execute(_request())
    with pytest.raises(WorkerProtocolError, match="disabled"):
        client.health()
    assert transport.calls == []


def test_client_authenticates_health_and_correlates_execution_response():
    host_private, _ = _keypair()
    worker_private, worker_public = _keypair()
    transport = Transport(worker_private)
    client = BrowserWorkerClient(
        endpoint="https://192.168.56.10:9443", host_signing_key=host_private,
        worker_verification_key=worker_public, worker_id="worker-test", transport=transport, enabled=True,
    )
    def response_for(method, path, body, _timeout):
        if path.endswith("health"):
            envelope = json.loads(body)
            challenge = envelope["payload"]
            from backend.app.browser.worker_protocol import HealthChallenge, WorkerHealth
            request = HealthChallenge.model_validate(challenge)
            return 200, sign_payload(WorkerHealth(
                request_id=request.request_id, challenge=request.challenge,
                worker_id="worker-test", ready=True, browser_available=True, proxy_configured=True,
            ), worker_private)
        payload = json.loads(body)["payload"]
        request = WorkerRequest.model_validate(payload)
        return 200, sign_payload(WorkerResponse(
            request_sha256=hashlib.sha256(body).hexdigest(), request_id=request.request_id, task_id=request.task_id, step_id=request.step_id,
            session_id=request.session_id, worker_id="worker-test", completed_at=int(time.time()),
            result=WorkerResult(success=True, current_url=request.operation.url, title="Synthetic"),
        ), worker_private)
    transport.request = response_for
    assert client.health().ready
    request = _request()
    observed = client.observe(request)
    assert observed.response.request_id == request.request_id
    assert observed.authorization == request
    assert observed.independently_verified is False
    assert hashlib.sha256(observed.signed_response).hexdigest() == observed.response_sha256
    assert observed.receipt_id != request.request_id
    assert observed.response.result.content_trust == "untrusted_webpage_data"


def test_client_timeout_and_mismatched_response_fail_closed():
    host_private, _ = _keypair()
    worker_private, worker_public = _keypair()
    transport = Transport(worker_private)
    client = BrowserWorkerClient(
        endpoint="https://192.168.56.10:9443", host_signing_key=host_private,
        worker_verification_key=worker_public, worker_id="worker-test", transport=transport, enabled=True,
    )
    transport.error = TimeoutError()
    with pytest.raises(WorkerProtocolError, match="timed out"):
        client.execute(_request())
    transport.error = None
    request = _request()
    transport.response = sign_payload(WorkerResponse(
        request_sha256="f"*64, request_id=uuid4(), task_id=request.task_id, step_id=request.step_id,
        session_id=request.session_id, worker_id="worker-test", completed_at=int(time.time()),
        result=WorkerResult(success=True),
    ), worker_private)
    with pytest.raises(WorkerProtocolError, match="did not match"):
        client.execute(request)

    transport.response = sign_payload({"not": "a worker response"}, worker_private)
    with pytest.raises(WorkerProtocolError, match="malformed"):
        client.execute(_request())


def test_browser_executor_requires_exact_proxy_endpoint():
    from backend.app.browser.worker_service import BrokerProxy
    with pytest.raises(ValueError, match="host-only policy"):
        BrokerProxy(
            proxy_endpoint="http://127.0.0.1:18765", proxy_username="u", proxy_password="p",
        )
    with pytest.raises(ValueError, match="credentials"):
        BrokerProxy(
            proxy_endpoint="https://192.168.56.1:18765", proxy_username="unbound-worker", proxy_password="p",
        )


def test_proxy_health_probe_requires_authentication_and_denied_private_target(monkeypatch):
    from backend.app.browser.worker_service import BrokerProxy

    class Connection:
        def __init__(self, status):
            self.status = status
            self.sent = b""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _timeout):
            pass

        def sendall(self, body):
            self.sent = body

        def recv(self, _count):
            return self.status if b"Proxy-Authorization:" in self.sent else self.anonymous_status

    connection = Connection(b"HTTP/1.1 403 Forbidden\r\n")
    connection.anonymous_status = b"HTTP/1.1 407 Proxy Authentication Required\r\n"
    monkeypatch.setattr("backend.app.browser.worker_service.socket.create_connection", lambda *_a, **_k: connection)
    tls = ssl.create_default_context()
    monkeypatch.setattr(tls, "wrap_socket", lambda stream, **kwargs: stream)
    executor = BrokerProxy(tls=tls,
        proxy_endpoint="https://192.168.56.1:18765", proxy_username="unbound", proxy_password="secret-test",
    )
    assert executor.proxy_available() is True
    assert b"Proxy-Authorization: Basic dW5ib3VuZDpzZWNyZXQtdGVzdA==" in connection.sent

    connection.anonymous_status = connection.status
    assert executor.proxy_available() is False
    connection.anonymous_status = b"HTTP/1.1 407 Proxy Authentication Required\r\n"
    connection.status = b"HTTP/1.1 407 Proxy Authentication Required\r\n"
    assert executor.proxy_available() is False


def test_worker_listener_factory_rejects_wildcard_and_host_addresses(tmp_path):
    from backend.app.browser.worker_service import build_https_server
    private, public = _keypair()
    service = WorkerService(
        worker_id="worker-test", host_verification_key=public, worker_signing_key=private,
        executor=Executor(), replay_guard=ReplayGuard(tmp_path / "replay.sqlite3"), proxy_available=lambda: True,
    )
    for address in ("0.0.0.0", "192.168.56.1", "192.168.56.0", "192.168.57.10"):
        with pytest.raises(ValueError, match="host-only policy"):
            build_https_server(service, bind_host=address, port=9443, cert_file="missing", key_file="missing")


@pytest.mark.parametrize("case", ["other-approval", "missing-source", "missing-screenshot", "type-coercion"])
def test_signed_guest_reply_cannot_substitute_authorization_or_observation(case):
    host_key, _ = _keypair()
    guest_key, verifier = _keypair()
    transport = Transport(guest_key)
    request = _request()
    if case == "missing-screenshot":
        from backend.app.browser.worker_protocol import Screenshot
        request = make_worker_request(
            task_id=request.task_id, step_id=request.step_id, session_id=request.session_id,
            approval_id=request.approval_id, tool_name=request.tool_name, worker_id=request.worker_id,
            operation=Screenshot(operation="screenshot"),
        )

    def reply(_method, _path, wire, _timeout):
        if case == "other-approval":
            wire = sign_payload(request.model_copy(update={"approval_id": uuid4()}), host_key)
        claimed = {"success": True, "current_url": "https://example.com/"}
        if case == "missing-source":
            del claimed["current_url"]
        if case == "type-coercion":
            claimed["success"] = "true"
        payload = {
            "request_sha256": hashlib.sha256(wire).hexdigest(), "request_id": str(request.request_id),
            "task_id": str(request.task_id), "step_id": request.step_id, "session_id": str(request.session_id),
            "worker_id": request.worker_id, "completed_at": int(time.time()), "result": claimed,
        }
        return 200, sign_payload(payload, guest_key)

    transport.request = reply
    client = BrowserWorkerClient(
        endpoint="https://192.168.56.10:9443", worker_id=request.worker_id, transport=transport,
        host_signing_key=host_key, worker_verification_key=verifier, enabled=True,
    )
    with pytest.raises(WorkerProtocolError) as rejected:
        client.observe(request)
    assert rejected.value.code == ("response_mismatch" if case == "other-approval" else "invalid_response")
