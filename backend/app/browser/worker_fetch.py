"""Broker-mediated GET fetching; Chromium retains an AF_UNIX-only namespace.

Only the broker holds gateway TLS/authentication credentials. Renderer requests
need a short-lived grant issued while an authenticated host operation executes.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import stat
from threading import Event, RLock, Thread
import time
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from backend.app.browser.worker_protocol import StrictModel

FETCH_SOCKET = Path("/run/unbound-browser-worker/fetch.sock")
ALLOWED_HOSTS = frozenset({"example.com", "www.python.org", "docs.python.org"})
MAX_BODY = 4 * 1024 * 1024
MAX_WIRE = 6 * 1024 * 1024


def validate_fetch_url(url: str) -> str:
    if (not isinstance(url, str) or not 1 <= len(url) <= 4096 or not url.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url
            or any(code in url.lower() for code in ("%00", "%0d", "%0a"))):
        raise ValueError("invalid fetch destination")
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ValueError("invalid fetch destination") from None
    if (parts.scheme != "https" or host not in ALLOWED_HOSTS or port not in (None, 443)
            or parts.netloc not in {host, host + ":443"} or parts.fragment
            or parts.username is not None or parts.password is not None):
        raise ValueError("destination outside approved HTTPS origins")
    return url


class FetchRequest(StrictModel):
    task_id: UUID
    session_id: UUID
    token: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    url: str = Field(max_length=4096)

    _url = field_validator("url")(validate_fetch_url)


class FetchResponse(StrictModel):
    url: str
    status: int = Field(ge=200, le=599)
    content_type: str = Field(max_length=256)
    body_base64: str = Field(max_length=MAX_WIRE)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    location: str | None = None

    @model_validator(mode="after")
    def checked(self):
        validate_fetch_url(self.url)
        if any(ord(c) < 32 or ord(c) == 127 for c in self.content_type):
            raise ValueError("invalid response content type")
        body = base64.b64decode(self.body_base64, validate=True)
        if len(body) > MAX_BODY or hashlib.sha256(body).hexdigest() != self.sha256:
            raise ValueError("fetch response integrity failure")
        if 300 <= self.status < 400:
            if self.location is None:
                raise ValueError("missing redirect destination")
            validate_fetch_url(self.location)
        elif self.location is not None:
            raise ValueError("unexpected redirect destination")
        return self


class GatewayClient:
    """Fixed numeric host-only endpoint. No DNS or automatic redirect following."""
    def __init__(self, *, tls: ssl.SSLContext, password: str,
                 endpoint: str = "https://192.168.56.1:18765"):
        if endpoint != "https://192.168.56.1:18765":
            raise ValueError("invalid gateway endpoint")
        if tls.verify_mode != ssl.CERT_REQUIRED or not tls.check_hostname:
            raise ValueError("gateway TLS verification required")
        if not 32 <= len(password) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in password):
            raise ValueError("invalid gateway credential")
        self.tls = tls
        self._authorization = "Basic " + base64.b64encode(("unbound:" + password).encode()).decode()

    def _request(self, method, path, body=None):
        connection = http.client.HTTPSConnection("192.168.56.1", 18765, timeout=10, context=self.tls)
        try:
            connection.request(method, path, body=body, headers={
                "Authorization": self._authorization, "Content-Type": "application/json",
                "Accept": "application/json", "Connection": "close",
            })
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("gateway request denied")
            deadline = time.monotonic() + 10
            data = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("gateway response deadline")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, MAX_WIRE + 1 - len(data)))
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
                if len(data) > MAX_WIRE:
                    raise ValueError("oversized gateway response")
        finally:
            connection.close()

    def proxy_available(self):
        try:
            return json.loads(self._request("GET", "/v1/health")) == {"status": "ok"}
        except (OSError, ValueError, http.client.HTTPException):
            return False

    def fetch(self, url):
        validate_fetch_url(url)
        raw = self._request("POST", "/v1/fetch", json.dumps({"url": url, "method": "GET"}).encode())
        result = FetchResponse.model_validate_json(raw, strict=True)
        if result.url != url:
            raise ValueError("gateway response source mismatch")
        return result


class FetchGrants:
    def __init__(self, *, clock=time.time):
        self._clock, self._lock, self._grants = clock, RLock(), {}

    def issue(self, request):
        now = self._clock()
        expires = min(request.expires_at, now + 30)
        if expires <= now:
            raise ValueError("fetch authorization expired")
        token = secrets.token_hex(32)
        with self._lock:
            self._grants[token] = [request.task_id, request.session_id, expires, 0, 0]
        return token

    def claim(self, request: FetchRequest):
        validate_fetch_url(request.url)
        with self._lock:
            grant = self._grants.get(request.token)
            if (grant is None or grant[:2] != [request.task_id, request.session_id]
                    or grant[2] <= self._clock() or grant[3] >= 64 or grant[4] >= 16 * 1024 * 1024):
                raise PermissionError("fetch grant denied")
            grant[3] += 1

    def charge(self, request, size):
        with self._lock:
            grant = self._grants.get(request.token)
            if grant is None or grant[2] <= self._clock():
                raise PermissionError("fetch grant expired")
            grant[4] += size
            if grant[4] > 16 * 1024 * 1024:
                raise PermissionError("fetch budget exceeded")

    def revoke(self, token):
        with self._lock:
            self._grants.pop(token, None)


class RendererFetchClient:
    def __init__(self):
        from backend.app.browser.worker_ipc import identity, RENDERER
        self.peer = identity(RENDERER)

    def fetch(self, request):
        from backend.app.browser.worker_ipc import verify_peer, send, frame
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(15)
            stream.connect(str(FETCH_SOCKET))
            verify_peer(stream, self.peer)
            send(stream, request, 8192)
            result = FetchResponse.model_validate_json(frame(stream, MAX_WIRE, time.monotonic() + 15), strict=True)
            if result.url != request.url:
                raise ValueError("fetch source mismatch")
            return result


class BrokerFetchServer:
    def __init__(self, gateway: GatewayClient, grants: FetchGrants):
        self.gateway, self.grants = gateway, grants
        self._stop = Event()
        self._listener = self._thread = self._inode = None

    def start(self):
        from backend.app.browser.worker_ipc import identity, BROKER
        self.peer = identity(BROKER)
        parent = FETCH_SOCKET.parent.stat()
        if (FETCH_SOCKET.parent.is_symlink() or parent.st_uid != os.geteuid() or parent.st_gid != os.getegid()
                or stat.S_IMODE(parent.st_mode) != 0o750):
            raise PermissionError("unsafe fetch socket directory")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(FETCH_SOCKET))  # Never remove an existing socket on startup.
            self._inode = FETCH_SOCKET.lstat().st_ino
            os.chmod(FETCH_SOCKET, 0o660)
            listener.listen(4)
            listener.settimeout(0.25)
        except Exception:
            listener.close()
            self.close()
            raise
        self._listener = listener
        self._thread = Thread(target=self._serve, name="unbound-broker-fetch", daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        from backend.app.browser.worker_ipc import verify_peer, frame, send
        while not self._stop.is_set():
            try:
                stream, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with stream:
                try:
                    verify_peer(stream, self.peer)
                    request = FetchRequest.model_validate_json(frame(stream, 8192, time.monotonic() + 3), strict=True)
                    self.grants.claim(request)
                    response = self.gateway.fetch(request.url)
                    self.grants.charge(request, len(base64.b64decode(response.body_base64)))
                    stream.settimeout(3)
                    send(stream, response, MAX_WIRE)
                except (OSError, ValueError, http.client.HTTPException):
                    # Close on errors, never return upstream errors/credentials.
                    continue

    def close(self):
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
        if self._thread is not None:
            self._thread.join(timeout=15)
        try:
            info = FETCH_SOCKET.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_ino == self._inode and info.st_uid == os.geteuid():
                FETCH_SOCKET.unlink()
        except FileNotFoundError:
            pass
