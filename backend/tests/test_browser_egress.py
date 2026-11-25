from __future__ import annotations

import base64
from http.client import HTTPConnection
import ipaddress
import socket
import ssl
import threading

import pytest

from backend.app.egress_proxy import DNSOverTLSResolver
from backend.app.browser.egress import DestinationPolicy, EgressDenied, LocalEgressProxy, classify_address


def _dns_response(query, records=()):
    import dns.message
    import dns.rrset
    import dns.rdatatype

    response = dns.message.make_response(query)
    for record_type, address in records:
        response.answer.append(dns.rrset.from_text(
            query.question[0].name, 60, "IN", dns.rdatatype.to_text(record_type), address
        ))
    return response


def test_dot_uses_pinned_numeric_resolver_ip_and_verified_tls_identity(monkeypatch):
    import dns.rdatatype

    calls = []

    def query_tls(query, **kwargs):
        calls.append((query.question[0].rdtype, kwargs))
        record = ((dns.rdatatype.A, "93.184.216.34"),) if query.question[0].rdtype == dns.rdatatype.A else ()
        return _dns_response(query, record)

    resolver = DNSOverTLSResolver(
        "1.1.1.1", "resolver.example", timeout=2.5, query_tls=query_tls
    )
    assert resolver("site.example", 443) == ("93.184.216.34",)
    assert [item[0] for item in calls] == [dns.rdatatype.A, dns.rdatatype.AAAA]
    for _rdtype, kwargs in calls:
        assert kwargs["where"] == "1.1.1.1"
        assert kwargs["port"] == 853
        assert kwargs["server_hostname"] == "resolver.example"
        assert kwargs["timeout"] == 2.5
        assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["ssl_context"].check_hostname is True


@pytest.mark.parametrize("failure", [ssl.SSLCertVerificationError("bad certificate"), TimeoutError("timeout")])
def test_dot_certificate_failure_and_timeout_fail_closed(failure):
    resolver = DNSOverTLSResolver(
        "1.1.1.1", "resolver.example", query_tls=lambda *_args, **_kwargs: (_ for _ in ()).throw(failure)
    )
    with pytest.raises(EgressDenied, match="could not be resolved safely"):
        DestinationPolicy(resolver=resolver).validate("site.example", 443)


def test_dot_malformed_response_and_empty_a_aaaa_fail_closed():
    import dns.message

    def malformed(query, **_kwargs):
        return dns.message.make_response(dns.message.make_query("other.example", query.question[0].rdtype))

    resolver = DNSOverTLSResolver("1.1.1.1", "resolver.example", query_tls=malformed)
    with pytest.raises(EgressDenied, match="could not be resolved safely"):
        DestinationPolicy(resolver=resolver).validate("site.example", 443)


def test_dot_dns_error_response_code_fails_closed():
    import dns.message
    import dns.rcode

    def server_failure(query, **_kwargs):
        response = dns.message.make_response(query)
        response.set_rcode(dns.rcode.SERVFAIL)
        return response

    resolver = DNSOverTLSResolver("1.1.1.1", "resolver.example", query_tls=server_failure)
    with pytest.raises(EgressDenied, match="could not be resolved safely"):
        DestinationPolicy(resolver=resolver).validate("site.example", 443)

    resolver = DNSOverTLSResolver(
        "1.1.1.1", "resolver.example", query_tls=lambda query, **_kwargs: _dns_response(query)
    )
    with pytest.raises(EgressDenied, match="could not be resolved safely"):
        DestinationPolicy(resolver=resolver).validate("site.example", 443)


def test_dot_mixed_public_private_answer_is_rejected():
    import dns.rdatatype

    def mixed(query, **_kwargs):
        if query.question[0].rdtype == dns.rdatatype.A:
            return _dns_response(query, ((dns.rdatatype.A, "93.184.216.34"),))
        return _dns_response(query, ((dns.rdatatype.AAAA, "fd00::1"),))

    resolver = DNSOverTLSResolver("1.1.1.1", "resolver.example", query_tls=mixed)
    with pytest.raises(EgressDenied, match="non-public"):
        DestinationPolicy(resolver=resolver).validate("site.example", 443)


def test_dot_never_falls_back_to_system_dns(monkeypatch):
    import dns.rdatatype

    def no_system_dns(*_args, **_kwargs):
        raise AssertionError("system DNS fallback was attempted")

    monkeypatch.setattr(socket, "getaddrinfo", no_system_dns)
    resolver = DNSOverTLSResolver(
        "1.1.1.1", "resolver.example",
        query_tls=lambda query, **_kwargs: _dns_response(
            query,
            ((dns.rdatatype.A, "93.184.216.34"),) if query.question[0].rdtype == dns.rdatatype.A else (),
        ),
    )
    assert resolver("site.example", 443) == ("93.184.216.34",)


def test_default_destination_policy_requires_dot_configuration_and_uses_dot(monkeypatch):
    import dns.query
    import dns.rdatatype

    for name in ("UNBOUND_DOT_RESOLVER_IP", "UNBOUND_DOT_TLS_SERVER_NAME", "UNBOUND_DOT_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="must be configured"):
        DestinationPolicy()

    monkeypatch.setenv("UNBOUND_DOT_RESOLVER_IP", "1.1.1.1")
    monkeypatch.setenv("UNBOUND_DOT_TLS_SERVER_NAME", "resolver.example")
    monkeypatch.setenv("UNBOUND_DOT_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("system DNS fallback")),
    )
    monkeypatch.setattr(
        dns.query,
        "tls",
        lambda query, **_kwargs: _dns_response(
            query,
            ((dns.rdatatype.A, "93.184.216.34"),) if query.question[0].rdtype == dns.rdatatype.A else (),
        ),
    )
    assert DestinationPolicy().validate("site.example", 443).addresses == (
        ipaddress.ip_address("93.184.216.34"),
    )


@pytest.mark.parametrize(
    ("resolver_ip", "server_name", "timeout"),
    [("resolver.example", "resolver.example", 3), ("127.0.0.1", "resolver.example", 3), ("1.1.1.1", "bad name", 3), ("1.1.1.1", "resolver.example", 0)],
)
def test_dot_configuration_rejects_invalid_values(resolver_ip, server_name, timeout):
    with pytest.raises(ValueError):
        DNSOverTLSResolver(resolver_ip, server_name, timeout=timeout)


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0", "10.1.2.3", "127.0.0.1", "169.254.169.254", "172.16.0.1",
        "192.168.1.1", "224.0.0.1", "240.0.0.1", "192.0.2.1",
        "::", "::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "fc00::1",
        "fe80::1", "ff02::1", "2001:db8::1", "64:ff9b::a00:1",
        "2002:0a00:0001::1", "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
    ],
)
def test_non_public_ipv4_ipv6_and_transition_destinations_are_rejected(address):
    assert classify_address(address) is False


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_global_unicast_addresses_are_accepted(address):
    assert classify_address(address) is True


def test_dns_failure_and_empty_answers_fail_closed():
    def failed(_host, _port):
        raise OSError("synthetic resolver failure")

    with pytest.raises(EgressDenied, match="could not be resolved"):
        DestinationPolicy(resolver=failed).validate("public.example", 443)
    with pytest.raises(EgressDenied, match="no addresses"):
        DestinationPolicy(resolver=lambda *_: ()).validate("public.example", 443)


def test_mixed_public_and_private_dns_answer_is_rejected_without_connecting():
    connected = []
    policy = DestinationPolicy(
        resolver=lambda *_: ("93.184.216.34", "10.0.0.9"),
        connector=lambda address, port, timeout: connected.append((address, port)),
    )
    with pytest.raises(EgressDenied, match="non-public"):
        policy.connect("rebound.example", 443)
    assert connected == []


def test_connection_uses_the_validated_numeric_address_not_a_second_dns_lookup():
    class FakeSocket:
        def getpeername(self):
            return ("93.184.216.34", 443)

    calls = []
    policy = DestinationPolicy(
        resolver=lambda host, port: ("93.184.216.34",),
        connector=lambda address, port, timeout: calls.append((address, port)) or FakeSocket(),
    )
    connected = policy.connect("public.example", 443)
    assert connected.getpeername() == ("93.184.216.34", 443)
    assert calls == [(ipaddress.ip_address("93.184.216.34"), 443)]


def test_dns_rebinding_is_revalidated_on_each_new_connection():
    answers = iter((("93.184.216.34",), ("127.0.0.1",)))
    policy = DestinationPolicy(
        resolver=lambda *_: next(answers),
        connector=lambda address, port, timeout: type(
            "FakeSocket", (), {"getpeername": lambda self: (str(address), port)}
        )(),
    )
    first = policy.connect("rebound.example", 443)
    assert first.getpeername() == ("93.184.216.34", 443)
    with pytest.raises(EgressDenied, match="non-public"):
        policy.connect("rebound.example", 443)


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0177.0.0.1", "0x7f000001", "localhost", "singlelabel", "bad..example"])
def test_noncanonical_ip_encodings_and_non_dns_hostnames_are_rejected(host):
    policy = DestinationPolicy(resolver=lambda *_: ("93.184.216.34",))
    with pytest.raises(EgressDenied):
        policy.validate(host, 443)


def test_disallowed_destination_ports_fail_before_dns_resolution():
    calls = []
    policy = DestinationPolicy(resolver=lambda *args: calls.append(args) or ("93.184.216.34",))
    with pytest.raises(EgressDenied):
        policy.validate("public.example", 8443)
    assert calls == []


@pytest.mark.parametrize(
    "host",
    [
        "metadata.google.internal", "metadata.azure.internal", "instance-data.ec2.internal",
        "169.254.169.254", "168.63.129.16", "100.100.100.200", "printer.local",
    ],
)
def test_metadata_and_local_hostnames_are_rejected(host):
    policy = DestinationPolicy(resolver=lambda *_: ("93.184.216.34",))
    with pytest.raises(EgressDenied):
        policy.validate(host, 443)


class _PeerSocket:
    """Socket-pair endpoint exposing the validated peer for proxy tests."""

    def __init__(self, sock: socket.socket, address: str, port: int):
        self.sock = sock
        self.address = address
        self.port = port

    def getpeername(self):
        return self.address, self.port

    def fileno(self):
        return self.sock.fileno()

    def settimeout(self, value):
        self.sock.settimeout(value)

    def sendall(self, data):
        self.sock.sendall(data)

    def recv(self, size):
        return self.sock.recv(size)

    def close(self):
        self.sock.close()


def _fake_connector(resolver_map, path_responses=None, calls=None, requests=None):
    calls = calls if calls is not None else []

    def connect(address, port, _timeout):
        calls.append((str(address), port))
        client_sock, server_sock = socket.socketpair()

        def serve():
            try:
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    data = server_sock.recv(4096)
                    if not data:
                        return
                    request.extend(data)
                if requests is not None:
                    requests.append(bytes(request))
                first_line = bytes(request).split(b"\r\n", 1)[0].decode("ascii", "replace")
                path = first_line.split(" ")[1]
                response = (path_responses or {}).get(
                    path,
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok",
                )
                server_sock.sendall(response)
            finally:
                server_sock.close()

        threading.Thread(target=serve, daemon=True).start()
        return _PeerSocket(client_sock, str(address), port)

    return connect


def _request(proxy: LocalEgressProxy, url: str):
    host_port = proxy.server.removeprefix("http://")
    host, port = host_port.rsplit(":", 1)
    connection = HTTPConnection(host, int(port), timeout=3)
    credentials = proxy.proxy_credentials
    token = base64.b64encode(f"{credentials['username']}:{credentials['password']}".encode()).decode()
    connection.request("GET", url, headers={
        "Host": url.split("/", 3)[2], "Proxy-Authorization": f"Basic {token}",
    })
    response = connection.getresponse()
    result = response.status, response.getheader("Location"), response.read()
    connection.close()
    return result


def test_proxy_accepts_public_http_and_connects_to_pinned_ip():
    calls = []
    forwarded_requests = []
    policy = DestinationPolicy(
        resolver=lambda *_: ("93.184.216.34",),
        connector=_fake_connector({}, calls=calls, requests=forwarded_requests),
    )
    proxy = LocalEgressProxy(policy=policy).start()
    try:
        status, _, body = _request(proxy, "http://public.example/path?q=1")
        assert status == 200
        assert body == b"ok"
        assert calls == [("93.184.216.34", 80)]
        assert b"Proxy-Authorization" not in forwarded_requests[0]
        assert proxy.proxy_credentials["password"].encode() not in forwarded_requests[0]
    finally:
        proxy.close()


def test_proxy_rejects_private_destination_before_connecting():
    calls = []
    policy = DestinationPolicy(
        resolver=lambda *_: ("127.0.0.1",),
        connector=_fake_connector({}, calls=calls),
    )
    proxy = LocalEgressProxy(policy=policy).start()
    try:
        status, _, _ = _request(proxy, "http://private.example/resource")
        assert status == 403
        assert calls == []
    finally:
        proxy.close()


def test_https_connect_is_tunneled_to_the_pinned_public_address():
    calls = []

    def connector(address, port, _timeout):
        client_sock, server_sock = socket.socketpair()

        def echo():
            try:
                received = server_sock.recv(16)
                if received:
                    server_sock.sendall(b"pinned:" + received)
            finally:
                server_sock.close()

        threading.Thread(target=echo, daemon=True).start()
        calls.append((str(address), port))
        return _PeerSocket(client_sock, str(address), port)

    policy = DestinationPolicy(resolver=lambda *_: ("93.184.216.34",), connector=connector)
    proxy = LocalEgressProxy(policy=policy).start()
    sock = socket.create_connection(tuple(proxy._server.server_address), timeout=3)
    try:
        credentials = proxy.proxy_credentials
        token = base64.b64encode(f"{credentials['username']}:{credentials['password']}".encode()).decode()
        sock.sendall(
            f"CONNECT secure.example:443 HTTP/1.1\r\nHost: secure.example:443\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            response += sock.recv(4096)
        assert response.startswith(b"HTTP/1.1 200 Connection Established")
        sock.sendall(b"client-hello")
        assert sock.recv(4096) == b"pinned:client-hello"
        assert calls == [("93.184.216.34", 443)]
    finally:
        sock.close()
        proxy.close()


def test_proxy_requires_per_process_authentication():
    calls = []
    proxy = LocalEgressProxy(policy=DestinationPolicy(
        resolver=lambda *_: ("93.184.216.34",),
        connector=_fake_connector({}, calls=calls),
    )).start()
    try:
        host, port = proxy._server.server_address[:2]
        connection = HTTPConnection(host, port, timeout=3)
        connection.request("GET", "http://public.example/", headers={"Host": "public.example"})
        response = connection.getresponse()
        assert response.status == 407
        assert response.getheader("Proxy-Authenticate")
        response.read()
        connection.close()
        assert calls == []
    finally:
        proxy.close()


def test_redirect_hops_are_independently_validated_before_each_connection():
    calls = []
    responses = {
        "/start": b"HTTP/1.1 302 Found\r\nLocation: http://next.example/hop\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        "/hop": b"HTTP/1.1 302 Found\r\nLocation: http://private.example/metadata\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    }
    policy = DestinationPolicy(
        resolver=lambda host, _port: {"public.example": ("93.184.216.34",), "next.example": ("1.1.1.1",), "private.example": ("10.0.0.8",)}[host],
        connector=_fake_connector({}, path_responses=responses, calls=calls),
    )
    proxy = LocalEgressProxy(policy=policy).start()
    try:
        first = _request(proxy, "http://public.example/start")
        assert first[0] == 302 and first[1] == "http://next.example/hop"
        second = _request(proxy, first[1])
        assert second[0] == 302 and second[1] == "http://private.example/metadata"
        third = _request(proxy, second[1])
        assert third[0] == 403
        assert calls == [("93.184.216.34", 80), ("1.1.1.1", 80)]
    finally:
        proxy.close()


@pytest.mark.parametrize("resource", ["subresource", "iframe", "popup", "download"])
def test_all_browser_http_resource_destinations_use_the_same_proxy_policy(resource):
    calls = []
    policy = DestinationPolicy(
        resolver=lambda *_: ("169.254.169.254",),
        connector=_fake_connector({}, calls=calls),
    )
    proxy = LocalEgressProxy(policy=policy).start()
    try:
        status, _, _ = _request(proxy, f"http://resource.example/{resource}")
        assert status == 403
        assert calls == []
    finally:
        proxy.close()
