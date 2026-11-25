from __future__ import annotations

import base64
import hashlib
import os
import socket
import urllib.error
import urllib.request

import pytest

from backend.app.browser.egress import LOCAL_DEMO_PRODUCT_URL
from backend.app.browser.controller import BrowserController, BrowserControllerError
from backend.app.browser.standalone_proxy import (
    StandaloneEgressProxy,
    StandaloneProxyError,
    default_proxy_executable,
)


def _available_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _authenticated_opener(proxy: StandaloneEgressProxy):
    credentials = proxy.proxy_credentials
    password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(
        None, proxy.server, credentials["username"], credentials["password"]
    )
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy.server}),
        urllib.request.ProxyBasicAuthHandler(password_manager),
    )


def test_non_demo_proxy_requires_explicit_dot_configuration(monkeypatch):
    for name in (
        "UNBOUND_DOT_RESOLVER_IP",
        "UNBOUND_DOT_TLS_SERVER_NAME",
        "UNBOUND_DOT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="must be configured"):
        StandaloneEgressProxy(port=18765, demo_only=False)

    monkeypatch.setenv("UNBOUND_DOT_RESOLVER_IP", "1.1.1.1")
    monkeypatch.setenv("UNBOUND_DOT_TLS_SERVER_NAME", "resolver.example")
    monkeypatch.setenv("UNBOUND_DOT_TIMEOUT_SECONDS", "2.5")
    proxy = StandaloneEgressProxy(port=18765, demo_only=False)
    assert proxy._dns_resolver_ip == "1.1.1.1"
    assert proxy._dns_tls_server_name == "resolver.example"
    assert proxy._dns_timeout_seconds == 2.5


def test_local_demo_proxy_does_not_require_dot_configuration(monkeypatch):
    for name in (
        "UNBOUND_DOT_RESOLVER_IP",
        "UNBOUND_DOT_TLS_SERVER_NAME",
        "UNBOUND_DOT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    proxy = StandaloneEgressProxy(port=18765, demo_only=True)
    assert proxy._dns_resolver_ip is None
    assert proxy._dns_tls_server_name is None
    assert proxy._dns_timeout_seconds is None


@pytest.mark.skipif(not default_proxy_executable().is_file(), reason="Build the standalone proxy executable first")
def test_packaged_proxy_has_distinct_identity_fixed_endpoint_and_private_control_pipe():
    port = _available_loopback_port()
    proxy = StandaloneEgressProxy(port=port, demo_only=True)
    try:
        proxy.start()
        assert proxy.is_running
        assert proxy.process_id is not None
        assert proxy.process_id != os.getpid()
        assert proxy.executable_path == default_proxy_executable().resolve()
        assert proxy.server == f"http://127.0.0.1:{port}"
        assert proxy.executable_sha256 == hashlib.sha256(proxy.executable_path.read_bytes()).hexdigest()
        assert proxy.identity_verified
        assert proxy.probe_validation()

        response = _authenticated_opener(proxy).open(LOCAL_DEMO_PRODUCT_URL, timeout=3)
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert "Version: 2.1" in body
        assert "All information on this page is synthetic" in body
    finally:
        child_pid = proxy.process_id
        proxy.close()

    assert not proxy.is_running
    assert proxy.process_id is None
    if child_pid is not None:
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)


@pytest.mark.skipif(not default_proxy_executable().is_file(), reason="Build the standalone proxy executable first")
def test_standalone_proxy_rejects_private_destination_and_does_not_fallback_port():
    port = _available_loopback_port()
    first = StandaloneEgressProxy(port=port, demo_only=True).start()
    second = StandaloneEgressProxy(port=port, demo_only=True)
    try:
        credentials = first.proxy_credentials
        token = base64.b64encode(
            f"{credentials['username']}:{credentials['password']}".encode("utf-8")
        ).decode("ascii")
        connection = socket.create_connection(("127.0.0.1", port), timeout=2)
        connection.sendall(
            (
                "GET http://127.0.0.1/ HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                f"Proxy-Authorization: Basic {token}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        response = connection.recv(128)
        connection.close()
        assert response.startswith(b"HTTP/1.1 403")
        with pytest.raises(StandaloneProxyError, match="startup rejected"):
            second.start()
        assert not second.is_running
        assert first.server == f"http://127.0.0.1:{port}"
    finally:
        second.close()
        first.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows image sharing semantics only")
@pytest.mark.skipif(not default_proxy_executable().is_file(), reason="Build the standalone proxy executable first")
def test_proxy_executable_cannot_be_opened_for_replacement_while_running():
    proxy = StandaloneEgressProxy(port=_available_loopback_port(), demo_only=True).start()
    try:
        with pytest.raises(OSError):
            with proxy.executable_path.open("ab"):
                pass
        internal_runtime = proxy.executable_path.parent / "_internal" / "python314.dll"
        if internal_runtime.is_file():
            with pytest.raises(OSError):
                with internal_runtime.open("ab"):
                    pass
        assert proxy.identity_verified
    finally:
        proxy.close()


def test_child_stderr_is_not_forwarded_into_backend_logs(caplog):
    from types import SimpleNamespace

    proxy = object.__new__(StandaloneEgressProxy)
    proxy._process = SimpleNamespace(stderr=iter([
        "unexpected error proxy_secret=do-not-log https://private.example/path\n",
    ]))
    proxy._read_stderr()
    assert "do-not-log" not in caplog.text
    assert "private.example" not in caplog.text
    assert "emitted a diagnostic" in caplog.text


@pytest.mark.skipif(not default_proxy_executable().is_file(), reason="Build the standalone proxy executable first")
def test_real_chromium_navigation_fails_after_proxy_shutdown():
    proxy = StandaloneEgressProxy(port=_available_loopback_port(), demo_only=True).start()
    browser = BrowserController(
        proxy_server=proxy.server,
        proxy_credentials=proxy.proxy_credentials,
        headless=True,
        timeout_ms=1500,
    ).start()
    try:
        proxy.close()
        with pytest.raises(BrowserControllerError):
            browser.navigate(LOCAL_DEMO_PRODUCT_URL)
    finally:
        browser.close()
        proxy.close()
