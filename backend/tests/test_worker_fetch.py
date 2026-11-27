"""Offline tests: no gateway listener, guest changes or external destinations."""
import base64
import hashlib
import json
from types import SimpleNamespace
import ssl
from uuid import uuid4

import pytest

from backend.app.browser import worker_fetch as fetch
from backend.app.browser import worker_ipc as ipc
from backend.app.browser.worker_renderer import BrokerFetchRenderer, FixtureRenderer


def reply(url="https://example.com/", body=b"<h1>Synthetic fixture</h1>", **values):
    return fetch.FetchResponse(url=url, status=values.pop("status", 200), content_type="text/html",
                               body_base64=base64.b64encode(body).decode(),
                               sha256=hashlib.sha256(body).hexdigest(), **values)


def authorization():
    return SimpleNamespace(task_id=uuid4(), session_id=uuid4(), expires_at=130,
                           operation=SimpleNamespace(operation="navigate"))


@pytest.mark.parametrize("url", ["http://example.com/", "https://127.0.0.1/", "https://[::1]/",
                                 "https://10.0.0.1/", "https://169.254.169.254/",
                                 "https://evil.example/", "https://example.com:444/",
                                 "https://example.com./", "https://example.com@127.0.0.1/",
                                 "https://example.com\\evil/", "https://example.com/%0d",
                                 "https://example.com/#fragment", "https://EXAMPLE.COM/",
                                 "https://example.com:0443/", "https://example.com\n/"])
def test_destination_denied_without_transport(url):
    with pytest.raises(ValueError):
        fetch.validate_fetch_url(url)


@pytest.mark.parametrize("url", ["https://example.com/", "https://www.python.org:443/", "https://docs.python.org/3/"])
def test_only_approved_https_origins(url):
    assert fetch.validate_fetch_url(url) == url


def test_response_integrity_and_redirect_destination():
    good = reply()
    assert good.sha256 == hashlib.sha256(b"<h1>Synthetic fixture</h1>").hexdigest()
    for changes in ({"sha256": "0"*64}, {"body_base64": "!"},
                    {"status": 302}, {"status": 302, "location": "https://127.0.0.1/"},
                    {"location": "https://example.com/"}, {"content_type": "text/html\r\nX:1"}):
        with pytest.raises(ValueError):
            fetch.FetchResponse.model_validate(good.model_dump() | changes)
    assert reply(status=302, location="https://docs.python.org/3/").status == 302


def test_grants_bind_task_session_expiry_and_revoke():
    clock = [100]
    grants = fetch.FetchGrants(clock=lambda: clock[0])
    auth = authorization()
    token = grants.issue(auth)
    req = fetch.FetchRequest(task_id=auth.task_id, session_id=auth.session_id, token=token, url="https://example.com/")
    grants.claim(req)
    for changes in ({"task_id": uuid4()}, {"session_id": uuid4()}, {"token": "0"*64}):
        with pytest.raises(PermissionError):
            grants.claim(req.model_copy(update=changes))
    clock[0] = 130
    with pytest.raises(PermissionError):
        grants.claim(req)
    clock[0] = 101
    grants.revoke(token)
    with pytest.raises(PermissionError):
        grants.claim(req)


def test_grants_enforce_request_and_byte_budgets():
    grants = fetch.FetchGrants(clock=lambda: 100)
    auth = authorization()
    req = fetch.FetchRequest(task_id=auth.task_id, session_id=auth.session_id,
                             token=grants.issue(auth), url="https://example.com/")
    for _ in range(64):
        grants.claim(req)
    with pytest.raises(PermissionError):
        grants.claim(req)
    with pytest.raises(PermissionError):
        grants.charge(req, 16*1024*1024 + 1)


def test_gateway_requires_verified_tls_and_fixed_numeric_endpoint():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with pytest.raises(ValueError):
        fetch.GatewayClient(tls=context, password="x"*32, endpoint="https://evil.example:18765")
    context.check_hostname = False
    with pytest.raises(ValueError):
        fetch.GatewayClient(tls=context, password="x"*32)


def test_gateway_post_contract_and_no_site_headers(monkeypatch):
    calls = []
    data = [reply().model_dump_json().encode(), b""]
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
        def getresponse(self):
            return SimpleNamespace(status=200, read1=lambda _: data.pop(0))
        def close(self):
            calls.append("closed")
    monkeypatch.setattr(fetch.http.client, "HTTPSConnection", Connection)
    client = fetch.GatewayClient(tls=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), password="x"*32)
    assert client.fetch("https://example.com/").status == 200
    assert calls[0][0] == ("192.168.56.1", 18765)
    args, kwargs = calls[1]
    assert args == ("POST", "/v1/fetch")
    assert json.loads(kwargs["body"]) == {"url": "https://example.com/", "method": "GET"}
    assert set(kwargs["headers"]) == {"Authorization", "Content-Type", "Accept", "Connection"}
    assert calls[-1] == "closed"


@pytest.mark.parametrize("failure", [OSError("offline"), ssl.SSLError("TLS failure"), ValueError("bad response")])
def test_gateway_failure_has_no_fallback(monkeypatch, failure):
    client = fetch.GatewayClient(tls=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), password="x"*32)
    def failed(*args):
        raise failure
    monkeypatch.setattr(client, "_request", failed)
    assert client.proxy_available() is False
    with pytest.raises(type(failure)):
        client.fetch("https://example.com/")


def test_broker_revokes_grant_on_renderer_crash(monkeypatch):
    monkeypatch.setattr(ipc, "identity", lambda _: 900)
    grants = fetch.FetchGrants(clock=lambda: 100)
    auth = authorization()
    auth.operation = {"operation": "navigate", "url": "https://example.com/"}
    auth.operation = ipc.RenderRequest(task_id=auth.task_id, session_id=auth.session_id, operation=auth.operation).operation
    client = ipc.BrokerClient(fetch_grants=grants)
    def crash(message):
        assert message.fetch_token in grants._grants
        raise OSError("renderer offline")
    monkeypatch.setattr(client, "_exchange", crash)
    with pytest.raises(OSError):
        client.execute(auth)
    assert not grants._grants


def test_renderer_missing_grant_and_fixture_default_never_launch(monkeypatch):
    req = ipc.RenderRequest(task_id=uuid4(), session_id=uuid4(), operation={"operation": "navigate", "url": "https://example.com/"})
    renderer = BrokerFetchRenderer(None)
    monkeypatch.setattr(renderer, "_create_session", lambda: pytest.fail("unexpected launch"))
    assert renderer.execute(req).failure_code == "proxy_unavailable"
    assert FixtureRenderer().execute(req).failure_code == "invalid_destination"


@pytest.mark.parametrize("case", ["valid", "post", "private", "no_grant", "gateway_crash"])
def test_renderer_routes_only_authorized_get_through_ipc(case):
    sent = []
    class Client:
        def fetch(self, request):
            sent.append(request)
            if case == "gateway_crash":
                raise OSError("gateway unavailable")
            return reply()
    renderer = BrokerFetchRenderer(Client())
    req = ipc.RenderRequest(task_id=uuid4(), session_id=uuid4(), fetch_token="a"*64,
                            operation={"operation": "navigate", "url": "https://example.com/"})
    key = (req.task_id, req.session_id)
    if case != "no_grant":
        renderer._active_requests[key] = req
    fulfilled, aborted = [], []
    route = SimpleNamespace(request=SimpleNamespace(url="https://127.0.0.1/" if case == "private" else "https://example.com/",
                                                    method="POST" if case == "post" else "GET"),
                            fulfill=lambda **kwargs: fulfilled.append(kwargs), abort=aborted.append)
    renderer._route_handler(key)(route)
    if case == "valid":
        assert fulfilled[0]["body"] == b"<h1>Synthetic fixture</h1>"
        assert "form-action 'none'" in fulfilled[0]["headers"]["Content-Security-Policy"]
        assert sent[0].token == req.fetch_token
        assert not aborted
    else:
        assert aborted == ["blockedbyclient"]
        assert not fulfilled


def test_close_never_needs_proxy_grant(monkeypatch):
    renderer = BrokerFetchRenderer(None)
    req = ipc.RenderRequest(task_id=uuid4(), session_id=uuid4(), operation={"operation": "close"})
    assert renderer.execute(req).success


def test_fetch_ipc_peer_mismatch_is_denied_before_write(monkeypatch):
    monkeypatch.setattr(ipc, "identity", lambda _: 900)
    monkeypatch.setattr(fetch.socket, "AF_UNIX", 1, raising=False)
    writes = []
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, _): pass
        def connect(self, _): pass
        def sendall(self, data): writes.append(data)
    monkeypatch.setattr(fetch.socket, "socket", lambda *args: Stream())
    def rejected(*args): raise PermissionError("wrong peer")
    monkeypatch.setattr(ipc, "verify_peer", rejected)
    client = fetch.RendererFetchClient()
    with pytest.raises(PermissionError):
        client.fetch(fetch.FetchRequest(task_id=uuid4(), session_id=uuid4(), token="a"*64, url="https://example.com/"))
    assert not writes
