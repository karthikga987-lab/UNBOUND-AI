from __future__ import annotations

import io
import json

from backend.app import egress_proxy_process
from backend.app.egress_proxy import DNSOverTLSResolver


def _frame(config: dict[str, object]) -> str:
    return json.dumps(config) + "\n{\"command\":\"shutdown\"}\n"


def test_proxy_process_constructs_dot_policy_from_explicit_private_pipe_settings(monkeypatch):
    config = {
        "host": "127.0.0.1",
        "port": 18765,
        "demo_only": False,
        "proxy_secret": "synthetic-private-secret-0123456789abcdef",
        "max_body_bytes": 4096,
        "dns_resolver_ip": "1.1.1.1",
        "dns_tls_server_name": "resolver.example",
        "dns_timeout_seconds": 2.0,
    }
    stdin = io.StringIO(_frame(config))
    stdout = io.StringIO()
    monkeypatch.setattr(egress_proxy_process.sys, "stdin", stdin)
    monkeypatch.setattr(egress_proxy_process.sys, "stdout", stdout)
    captured = {}

    class FakeProxy:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.policy = kwargs["policy"]

        def start(self):
            return self

        def close(self):
            pass

    monkeypatch.setattr(egress_proxy_process, "LocalEgressProxy", FakeProxy)
    assert egress_proxy_process.main() == 0
    assert isinstance(captured["policy"]._resolver, DNSOverTLSResolver)
    assert captured["policy"]._resolver.resolver_ip == "1.1.1.1"
    messages = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert messages[0]["dns_mode"] == "dot"
    assert "proxy_secret" not in messages[0]


def test_proxy_process_local_demo_does_not_require_or_construct_dns(monkeypatch):
    config = {
        "host": "127.0.0.1",
        "port": 18765,
        "demo_only": True,
        "proxy_secret": "synthetic-private-secret-0123456789abcdef",
        "max_body_bytes": 4096,
    }
    monkeypatch.setattr(egress_proxy_process.sys, "stdin", io.StringIO(_frame(config)))
    stdout = io.StringIO()
    monkeypatch.setattr(egress_proxy_process.sys, "stdout", stdout)
    captured = {}

    class FakeProxy:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            return self

        def close(self):
            pass

    monkeypatch.setattr(egress_proxy_process, "LocalEgressProxy", FakeProxy)
    assert egress_proxy_process.main() == 0
    assert captured["demo_only"] is True
    assert captured["policy"] is None
    assert json.loads(stdout.getvalue().splitlines()[0])["dns_mode"] == "offline_demo"


def test_proxy_process_rejects_missing_dot_settings_for_public_mode(monkeypatch):
    config = {
        "host": "127.0.0.1",
        "port": 18765,
        "demo_only": False,
        "proxy_secret": "synthetic-private-secret-0123456789abcdef",
        "max_body_bytes": 4096,
    }
    monkeypatch.setattr(egress_proxy_process.sys, "stdin", io.StringIO(_frame(config)))
    stdout = io.StringIO()
    monkeypatch.setattr(egress_proxy_process.sys, "stdout", stdout)
    assert egress_proxy_process.main() == 2
    assert json.loads(stdout.getvalue())["code"] == "invalid_startup_configuration"
