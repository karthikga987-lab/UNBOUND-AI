"""Private Linux IPC. Each connection carries one bounded operation and one observation."""
from __future__ import annotations

import os
import signal
import socket
import stat
import struct
import time
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field

from backend.app.browser.worker_protocol import (
    BrowserOperation, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, StrictModel, WorkerResult,
)

SOCKET = Path("/run/unbound-renderer/control.sock")
BROKER = "unbound-worker"
RENDERER = "unbound-renderer"


class RenderRequest(StrictModel):
    task_id: UUID
    session_id: UUID
    operation: BrowserOperation
    fetch_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", repr=False)


def identity(account):
    import pwd
    broker = pwd.getpwnam(BROKER).pw_uid
    renderer = pwd.getpwnam(RENDERER).pw_uid
    if min(broker, renderer) <= 0 or broker == renderer:
        raise PermissionError("distinct unprivileged identities required")
    expected, peer = (broker, renderer) if account == BROKER else (renderer, broker)
    if os.geteuid() != expected:
        raise PermissionError("incorrect service identity")
    return peer


def verify_peer(sock, expected):
    credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _, uid, _ = struct.unpack("3i", credentials)
    if uid != expected:
        raise PermissionError("unexpected IPC peer")


def frame(sock, maximum, deadline):
    def read_exact(size):
        buf = bytearray()
        while len(buf) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("IPC deadline")
            sock.settimeout(remaining)
            part = sock.recv(min(size - len(buf), 65536))
            if not part:
                raise ValueError("incomplete IPC frame")
            buf.extend(part)
        return bytes(buf)
    length, = struct.unpack("!I", read_exact(4))
    if not 0 < length <= maximum:
        raise ValueError("invalid IPC frame size")
    return read_exact(length)


def send(sock, model, maximum):
    body = model.model_dump_json().encode()
    if len(body) > maximum:
        raise ValueError("oversized IPC message")
    sock.sendall(struct.pack("!I", len(body)) + body)


class Ping(StrictModel):
    kind: Literal["ping"]


class BrokerClient:
    def __init__(self, *, fetch_grants=None):
        self.peer = identity(BROKER)
        self.fetch_grants = fetch_grants

    def _exchange(self, message):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(4)
            sock.connect(str(SOCKET))
            verify_peer(sock, self.peer)
            send(sock, message, MAX_REQUEST_BYTES)
            return frame(sock, MAX_RESPONSE_BYTES, time.monotonic() + 18)

    def browser_ready(self):
        try:
            return Ping.model_validate_json(self._exchange(Ping(kind="ping"))).kind == "ping"
        except (OSError, ValueError):
            return False

    def execute(self, request):
        token = None
        try:
            if self.fetch_grants is not None and request.operation.operation != "close":
                token = self.fetch_grants.issue(request)
            body = RenderRequest(task_id=request.task_id, session_id=request.session_id,
                                 operation=request.operation, fetch_token=token)
            return WorkerResult.model_validate_json(self._exchange(body), strict=True)
        finally:
            if token is not None:
                self.fetch_grants.revoke(token)


def _configured_renderer_mode() -> str:
    mode = os.getenv("UNBOUND_RENDERER_MODE", "fixture")
    if os.getenv("UNBOUND_BROWSER_WORKER_ENABLE") == "1" and mode != "broker_fetch":
        raise RuntimeError("networked worker requires broker_fetch renderer mode")
    if mode not in {"fixture", "broker_fetch"}:
        raise RuntimeError("unsupported renderer mode")
    return mode


def main():
    from backend.app.browser.worker_renderer import FixtureRenderer, BrokerFetchRenderer
    mode = _configured_renderer_mode()
    peer = identity(RENDERER)
    directory = SOCKET.parent.stat()
    if SOCKET.parent.is_symlink() or directory.st_uid != os.geteuid() or directory.st_gid != os.getegid() or stat.S_IMODE(directory.st_mode) != 0o750:
        raise PermissionError("unsafe IPC directory")
    if mode == "fixture":
        renderer = FixtureRenderer()
    elif mode == "broker_fetch":
        from backend.app.browser.worker_fetch import RendererFetchClient
        renderer = BrokerFetchRenderer(RendererFetchClient())
    else:
        raise ValueError("unsupported renderer mode")
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        # RuntimeDirectory is created fresh by systemd. Never unlink an existing path on startup.
        os.umask(0o117)
        listener.bind(str(SOCKET))
        os.chmod(SOCKET, 0o660)
        os.umask(0o077)
        listener.listen(2)
        listener.settimeout(1)
        try:
            while not stopping:
                renderer.expire_idle()
                try:
                    stream, _ = listener.accept()
                except TimeoutError:
                    continue
                with stream:
                    try:
                        verify_peer(stream, peer)
                        data = frame(stream, MAX_REQUEST_BYTES, time.monotonic() + 3)
                        if data == Ping(kind="ping").model_dump_json().encode():
                            if renderer.browser_ready():
                                send(stream, Ping(kind="ping"), 128)
                        else:
                            job = RenderRequest.model_validate_json(data, strict=True)
                            send(stream, renderer.execute(job), MAX_RESPONSE_BYTES)
                    except (OSError, ValueError):
                        continue
        finally:
            renderer.close_all()


if __name__ == "__main__":
    main()
