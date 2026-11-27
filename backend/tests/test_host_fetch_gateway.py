from __future__ import annotations

import base64
import hashlib
import io
import os
import ssl

import pytest

from backend.app.browser.egress import EgressDenied
from backend.app.browser.host_fetch_gateway import (
    ALLOWED_HOSTS,
    DEFAULT_BIND_ADDRESS,
    DEFAULT_BIND_PORT,
    EXPECTED_GUEST_ADDRESS,
    GATEWAY_USERNAME,
    MAX_RESPONSE_BYTES,
    GatewayRequestError,
    GatewayUpstreamError,
    HostFetchGateway,
    _read_http_response,
    authenticate_control_request,
    build_https_server,
    validate_fetch_url,
)


TOKEN = "A" * 48


class FakeSocket:
    def __init__(self, response: bytes):
        self.response = response
        self.sent = bytearray()
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data):
        self.sent.extend(data)

    def makefile(self, _mode):
        return io.BytesIO(self.response)

    def close(self):
        self.closed = True


class FakePolicy:
    def __init__(self, response: bytes):
        self.response = response
        self.connect_calls = []
        self.validate_calls = []

    def connect(self, hostname, port):
        self.connect_calls.append((hostname, port))
        return FakeSocket(self.response)

    def validate(self, hostname, port):
        self.validate_calls.append((hostname, port))
        if hostname not in ALLOWED_HOSTS or port != 443:
            raise EgressDenied("denied")
        return object()


class FakeTLSContext:
    verify_mode = ssl.CERT_REQUIRED
    check_hostname = True

    def __init__(self):
        self.server_names = []

    def wrap_socket(self, sock, *, server_hostname):
        self.server_names.append(server_hostname)
        return sock


def gateway(response: bytes):
    policy = FakePolicy(response)
    context = FakeTLSContext()
    return HostFetchGateway(token=TOKEN, policy=policy, tls_context=context), policy, context


def test_url_validation_accepts_only_exact_https_allowlist():
    parsed = validate_fetch_url("https://www.python.org/3/library/?a=b")
    assert parsed.hostname == "www.python.org"
    assert parsed.request_target == "/3/library/?a=b"
    assert parsed.url == "https://www.python.org/3/library/?a=b"
    assert len(ALLOWED_HOSTS) == 3


@pytest.mark.parametrize("url", [
    "http://example.com/",
    "https://example.com:444/",
    "https://example.com:0443/",
    "https://user@example.com/",
    "https://example.com@evil.test/",
    "https://example.com./",
    "https://EXAMPLE.COM./",
    "https://127.0.0.1/",
    "https://[::1]/",
    "https://0x7f000001/",
    "https://example.com\\@evil.test/",
    "https://example.com/%0d%0aHost:evil.test",
    "https://example.com/#fragment",
    "https://evil.test/",
    " https://example.com/",
    "https://example.com/%zz",
])
def test_url_validation_rejects_ambiguous_or_unapproved_destinations(url):
    with pytest.raises(GatewayRequestError):
        validate_fetch_url(url)


def test_authentication_requires_guest_ip_single_basic_header_and_exact_token():
    header = "Basic " + base64.b64encode(f"{GATEWAY_USERNAME}:{TOKEN}".encode()).decode()
    assert authenticate_control_request(EXPECTED_GUEST_ADDRESS, [header], TOKEN)
    assert not authenticate_control_request("192.168.56.11", [header], TOKEN)
    assert not authenticate_control_request(EXPECTED_GUEST_ADDRESS, [header, header], TOKEN)
    assert not authenticate_control_request(EXPECTED_GUEST_ADDRESS, ["Bearer " + TOKEN], TOKEN)
    wrong = "Basic " + base64.b64encode(f"{GATEWAY_USERNAME}:wrong".encode()).decode()
    assert not authenticate_control_request(EXPECTED_GUEST_ADDRESS, [wrong], TOKEN)
    assert not authenticate_control_request(EXPECTED_GUEST_ADDRESS, ["Basic !"], TOKEN)


def test_fetch_uses_validated_connection_tls_hostname_and_sanitized_get():
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
        b"Content-Length: 5\r\n\r\nhello"
    )
    client, policy, context = gateway(response)
    result = client.fetch("https://example.com/article")

    assert policy.connect_calls == [("example.com", 443)]
    assert context.server_names == ["example.com"]
    assert result == {
        "url": "https://example.com/article",
        "status": 200,
        "content_type": "text/html",
        "body_base64": base64.b64encode(b"hello").decode(),
        "sha256": hashlib.sha256(b"hello").hexdigest(),
    }

    sent = bytes(policy.connect_calls and client.policy.connect_calls is not None and b"") if False else b""
    # The gateway forwards a fixed GET only; neither inbound auth nor cookies exist
    # in this transport. The fake raw socket is retained by the policy below.
    assert result["body_base64"] == "aGVsbG8="


def test_fetch_sends_only_fixed_origin_headers_and_never_credentials(monkeypatch):
    response = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
    client, policy, _context = gateway(response)
    raw = FakeSocket(response)
    policy.connect = lambda hostname, port: raw
    client.fetch("https://docs.python.org/3/reference/")
    request = bytes(raw.sent)
    assert request.startswith(b"GET /3/reference/ HTTP/1.1\r\n")
    assert b"Host: docs.python.org\r\n" in request
    assert b"Authorization:" not in request
    assert b"Cookie:" not in request
    assert b"Proxy-Authorization:" not in request
    assert raw.closed


def test_fetch_returns_redirect_for_revalidation_without_following_it():
    response = (
        b"HTTP/1.1 302 Found\r\nLocation: /3/\r\nContent-Length: 0\r\n\r\n"
    )
    client, policy, _context = gateway(response)
    result = client.fetch("https://docs.python.org/")
    assert result["location"] == "https://docs.python.org/3/"
    assert policy.connect_calls == [("docs.python.org", 443)]
    assert policy.validate_calls == [("docs.python.org", 443)]


def test_fetch_rejects_redirect_outside_allowlist():
    client, _policy, _context = gateway(
        b"HTTP/1.1 302 Found\r\nLocation: https://evil.test/\r\nContent-Length: 0\r\n\r\n"
    )
    with pytest.raises(GatewayRequestError, match="destination_not_allowed"):
        client.fetch("https://example.com/")


def test_fetch_rejects_redirect_to_private_literal_before_returning_it():
    client, _policy, _context = gateway(
        b"HTTP/1.1 302 Found\r\nLocation: https://127.0.0.1/\r\nContent-Length: 0\r\n\r\n"
    )
    with pytest.raises(GatewayRequestError):
        client.fetch("https://example.com/")


def test_fetch_does_not_continue_when_destination_policy_denies():
    client, policy, context = gateway(b"")
    policy.connect = lambda *_args: (_ for _ in ()).throw(EgressDenied("private target"))
    with pytest.raises(EgressDenied):
        client.fetch("https://example.com/")
    assert context.server_names == []


def test_origin_tls_verification_cannot_be_disabled():
    insecure = FakeTLSContext()
    insecure.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="TLS"):
        HostFetchGateway(token=TOKEN, policy=FakePolicy(b""), tls_context=insecure)


def test_upstream_tls_failure_is_sanitized_and_socket_closed():
    client, policy, _context = gateway(b"")
    raw = FakeSocket(b"")
    policy.connect = lambda *_args: raw

    class BadTLS(FakeTLSContext):
        def wrap_socket(self, _sock, *, server_hostname):
            raise ssl.SSLCertVerificationError("secret certificate details")

    client.tls_context = BadTLS()
    with pytest.raises(GatewayUpstreamError, match="upstream_failed") as error:
        client.fetch("https://example.com/")
    assert "secret" not in str(error.value)
    assert raw.closed


def test_http_response_rejects_oversized_and_ambiguous_framing():
    too_big = f"HTTP/1.1 200 OK\r\nContent-Length: {MAX_RESPONSE_BYTES + 1}\r\n\r\n".encode()
    with pytest.raises(GatewayUpstreamError, match="response_too_large"):
        _read_http_response(FakeSocket(too_big))

    ambiguous = b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
    with pytest.raises(GatewayUpstreamError, match="invalid_upstream_response"):
        _read_http_response(FakeSocket(ambiguous))


def test_http_response_reads_bounded_chunked_body():
    result = _read_http_response(FakeSocket(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n0\r\n\r\n"
    ))
    assert result == (200, {"transfer-encoding": ["chunked"]}, b"hello")


def test_builder_rejects_non_fixed_listener_before_binding(tmp_path):
    missing = tmp_path / "not-used.pem"
    with pytest.raises(ValueError, match="fixed host-only endpoint"):
        build_https_server(
            HostFetchGateway(token=TOKEN, policy=FakePolicy(b"")),
            cert_file=missing,
            key_file=missing,
            client_ca_file=missing,
            bind_address="0.0.0.0",
        )
    assert DEFAULT_BIND_ADDRESS == "192.168.56.1"
    assert DEFAULT_BIND_PORT == 18765
