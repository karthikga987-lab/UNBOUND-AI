from __future__ import annotations

import base64
import hashlib
import os
import socket
import urllib.error
import urllib.request

import pytest

from backend.app.browser.egress import LOCAL_DEMO_PRODUCT_URL
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
        assert proxy.identity_verified
    finally:
        proxy.close()
