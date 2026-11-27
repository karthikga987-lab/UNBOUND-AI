"""Opt-in, authenticated browser-worker service primitives.

This module does not start a listener on import. The deployment entry point is
intended for the isolated Ubuntu guest and requires explicit configuration.
The Windows application remains authoritative for task state and verification.
"""
from __future__ import annotations

import hashlib
import base64
import ipaddress
import os
import signal
import socket
import sqlite3
import ssl
import stat
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import RLock, Thread
from typing import Callable
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import ValidationError

from backend.app.browser.worker_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    CloseBrowser,
    GuestPolicyAttestation,
    HealthChallenge,
    WorkerHealth,
    WorkerProtocolError,
    WorkerRequest,
    WorkerResponse,
    WorkerResult,
    WORKER_HOST_ONLY_NETWORK,
    canonical_json,
    checked_key_version,
    private_key_from_pem,
    public_key_from_pem,
    sign_payload,
    verify_envelope,
)


SYSTEMD_CREDENTIALS_ROOT = Path("/run/credentials")


def _systemd_credential_is_safe(
    path: Path,
    credentials_path: Path,
    file_info,
    credentials_info,
    *,
    effective_uid: int,
) -> bool:
    """Accept only private service files or systemd's root-owned per-unit copy.

    On Ubuntu 24.04, systemd exposes ``LoadCredential=`` files below
    ``/run/credentials/<unit>.service`` as root-owned, read-only files. The
    service can read them via systemd's access setup, even though their stat
    owner is root. Trust that layout only when it is the direct per-unit
    directory, root-owned, not writable by group/other, and readable by this
    process. This keeps ordinary environment-selected paths strict.
    """
    try:
        mode = stat.S_IMODE(file_info.st_mode)
        credentials_mode = stat.S_IMODE(credentials_info.st_mode)
        canonical_root = SYSTEMD_CREDENTIALS_ROOT.resolve(strict=True)
        canonical_directory = credentials_path.resolve(strict=True)
        canonical_file = path.resolve(strict=True)
        root_info = SYSTEMD_CREDENTIALS_ROOT.lstat()
        root_mode = stat.S_IMODE(root_info.st_mode)
        root_layout = (
            canonical_directory.parent == canonical_root
            and canonical_directory.name.endswith(".service")
            and canonical_file.parent == canonical_directory
            and stat.S_ISDIR(root_info.st_mode)
            and not stat.S_ISLNK(root_info.st_mode)
            and root_info.st_uid == 0
            and not root_mode & 0o022
        )
        systemd_owned = (
            root_layout
            and credentials_info.st_uid == 0
            and credentials_info.st_gid == 0
            and not credentials_mode & 0o027
            and file_info.st_uid == 0
            and file_info.st_gid == 0
            and not mode & 0o027
            and not mode & 0o111
            and os.access(path, os.R_OK, effective_ids=True)
        )
        service_private = (
            credentials_info.st_uid == effective_uid
            and not credentials_mode & 0o077
            and file_info.st_uid == effective_uid
            and not mode & 0o333
            and os.access(path, os.R_OK, effective_ids=True)
        )
        return bool(systemd_owned or service_private)
    except (OSError, TypeError, NotImplementedError):
        return False


def _read_worker_file(environment_name: str, *, public_material: bool = False) -> bytes:
    """Read a configured credential, applying confidentiality checks only to secrets.

    The host verification key is public material and may be readable inside
    systemd's credential directory. It must still be a regular, non-symlink
    file and must not be writable by group or other users.
    """
    configured_path = os.getenv(environment_name)
    if not configured_path:
        raise SystemExit(f"Required worker configuration is missing: {environment_name}")
    path = Path(configured_path)
    try:
        info = path.lstat()
    except OSError:
        raise SystemExit(f"Required worker file is unavailable: {environment_name}") from None
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit(f"Required worker file is unavailable: {environment_name}")
    mode = stat.S_IMODE(info.st_mode)
    # systemd LoadCredential material lives in a protected per-unit directory.
    # Ordinary paths remain strict; see _systemd_credential_is_safe.
    systemd_credentials = False
    credentials_directory = os.getenv("CREDENTIALS_DIRECTORY")
    if credentials_directory:
        credentials_path = Path(credentials_directory)
        try:
            credentials_info = credentials_path.lstat()
            effective_uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
            systemd_credentials = (
                stat.S_ISDIR(credentials_info.st_mode)
                and not stat.S_ISLNK(credentials_info.st_mode)
                and _systemd_credential_is_safe(
                    path,
                    credentials_path,
                    info,
                    credentials_info,
                    effective_uid=effective_uid,
                )
            )
        except OSError:
            systemd_credentials = False
    if public_material:
        if mode & 0o022 or mode & 0o111:
            raise SystemExit(f"Worker trust file permissions are unsafe: {environment_name}")
    elif mode & 0o077 and not systemd_credentials:
        raise SystemExit(f"Worker secret file permissions are too broad: {environment_name}")
    try:
        return path.read_bytes()
    except OSError:
        raise SystemExit(f"Required worker file is unavailable: {environment_name}") from None


class ReplayGuard:
    """Durably reject reuse of a signed request id, including after restart."""

    def __init__(self, database: str | Path):
        self.database = str(database)
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        last_error = None
        for attempt in range(8):
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("""CREATE TABLE IF NOT EXISTS consumed_requests (
                    request_id TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER NOT NULL
                )""")
                connection.execute("CREATE INDEX IF NOT EXISTS ix_consumed_requests_expiry ON consumed_requests(expires_at)")
                connection.execute("CREATE TABLE IF NOT EXISTS clock_floor (id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL)")
                last_error = None
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).casefold() and "busy" not in str(exc).casefold():
                    raise
                last_error = exc
                time.sleep(min(0.01 * (2 ** attempt), 0.5))
            finally:
                connection.close()
        if last_error is not None:
            raise RuntimeError("replay store initialization is busy") from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def consume(self, request_id: UUID, expires_at: int, *, now: int) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    previous = connection.execute("SELECT value FROM clock_floor WHERE id=1").fetchone()
                    if previous and now < previous[0]:
                        raise WorkerProtocolError("clock_rollback", "worker clock requires reconciliation")
                    if expires_at <= now:
                        raise WorkerProtocolError("request_expired", "worker request is expired")
                    connection.execute("INSERT OR REPLACE INTO clock_floor VALUES (1, ?)", (now,))
                    connection.execute("DELETE FROM consumed_requests WHERE expires_at < ?", (now,))
                    connection.execute(
                        "INSERT INTO consumed_requests(request_id, expires_at, consumed_at) VALUES (?, ?, ?)",
                        (str(request_id), expires_at, now),
                    )
                    connection.execute("COMMIT")
                except sqlite3.IntegrityError:
                    connection.execute("ROLLBACK")
                    raise WorkerProtocolError("replay_rejected", "worker request was already consumed") from None
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()


class BrokerProxy:
    """Broker-only probe for the future host proxy. Credentials never enter renderer IPC."""

    def __init__(self, *, proxy_endpoint: str, proxy_username: str, proxy_password: str, tls=None):
        parts = urlsplit(proxy_endpoint)
        try:
            address = ipaddress.ip_address(parts.hostname or "")
        except ValueError:
            raise ValueError("worker proxy must use the configured host-only address") from None
        if (parts.scheme != "https" or address != ipaddress.ip_address("192.168.56.1")
                or parts.port != 18765 or parts.username is not None or parts.password is not None or parts.path or parts.query or parts.fragment):
            raise ValueError("worker proxy endpoint is outside the host-only policy")
        if proxy_username != "unbound" or not proxy_password or len(proxy_password) > 256:
            raise ValueError("worker proxy credentials are required")
        self.proxy_endpoint = proxy_endpoint
        self.proxy_username = proxy_username
        if tls is None or not tls.check_hostname or tls.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("trusted guest-proxy TLS context required")
        self.tls = tls
        self.proxy_password = proxy_password

    def proxy_available(self) -> bool:
        parts = urlsplit(self.proxy_endpoint)
        token = base64.b64encode(f"{self.proxy_username}:{self.proxy_password}".encode("utf-8")).decode("ascii")
        return (
            self._probe_status(parts, "", b"HTTP/1.1 407 ")
            and self._probe_status(parts, "Proxy-Authorization: Basic " + token + "\r\n", b"HTTP/1.1 403 ")
        )

    def _probe_status(self, parts, authorization: str, expected: bytes) -> bool:
        probe = ("GET http://127.0.0.1/ HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 + authorization + "Connection: close\r\n\r\n").encode("ascii")
        deadline = time.monotonic() + 2
        try:
            with socket.create_connection((parts.hostname, parts.port), timeout=1.0) as stream:
                with self.tls.wrap_socket(stream, server_hostname=parts.hostname) as connection:
                    connection.sendall(probe)
                    response = bytearray()
                    while len(response) < 4096 and b"\r\n" not in response:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return False
                        connection.settimeout(min(1.0, remaining))
                        chunk = connection.recv(256)
                        if not chunk:
                            break
                        response.extend(chunk)
                    return b"\r\n" in response and response.startswith(expected)
        except OSError:
            return False


class WorkerService:
    """Authenticated request processor; signatures authorize only one operation."""

    def __init__(self, *, worker_id: str, host_verification_key: Ed25519PublicKey,
                 worker_signing_key: Ed25519PrivateKey, executor, replay_guard: ReplayGuard,
                 browser_available: Callable[[], bool] | None = None,
                 proxy_available: Callable[[], bool] | None = None, proxy_configured: bool = True,
                 guest_policy_attestor: Callable[[HealthChallenge], GuestPolicyAttestation | None] | None = None,
                 host_key_version: int = 1, worker_key_version: int = 1):
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 128:
            raise ValueError("worker identity is invalid")
        self.host_key_version = checked_key_version(host_key_version)
        self.worker_key_version = checked_key_version(worker_key_version)
        self.worker_id = worker_id
        self.host_verification_key = host_verification_key
        self.worker_signing_key = worker_signing_key
        self.executor = executor
        self.replay_guard = replay_guard
        self.browser_available = browser_available or (lambda: False)
        self.proxy_available = proxy_available or (lambda: False)
        self.proxy_configured = bool(proxy_configured)
        self.guest_policy_attestor = guest_policy_attestor

    def handle_health(self, body: bytes, *, now: int | None = None) -> bytes:
        current = int(time.time()) if now is None else now
        payload = verify_envelope(body, self.host_verification_key, max_bytes=4096, key_version=self.host_key_version)
        try:
            challenge = HealthChallenge.model_validate_json(canonical_json(payload), strict=True)
        except ValidationError:
            raise WorkerProtocolError("invalid_message", "health challenge is malformed") from None
        if abs(current - challenge.issued_at) > 15:
            raise WorkerProtocolError("request_expired", "health challenge is expired")
        self.replay_guard.consume(challenge.request_id, challenge.issued_at + 15, now=current)
        browser_available = bool(self.browser_available())
        proxy_available = bool(self.proxy_configured and self.proxy_available())
        # Guest containment is a separate, optional attestation. The worker's
        # ordinary readiness cannot imply it, and any collector error fails
        # closed without leaking platform details to the caller.
        policy_attestation = None
        if self.guest_policy_attestor is not None:
            try:
                candidate = self.guest_policy_attestor(challenge)
                if candidate is not None:
                    policy_attestation = GuestPolicyAttestation.model_validate(candidate, strict=True)
                    if (policy_attestation.request_id != challenge.request_id
                            or policy_attestation.worker_id != self.worker_id):
                        policy_attestation = None
            except Exception:
                policy_attestation = None
        response = WorkerHealth(
            request_id=challenge.request_id, challenge=challenge.challenge,
            worker_id=self.worker_id, ready=browser_available and proxy_available,
            browser_available=browser_available, proxy_configured=self.proxy_configured,
            guest_policy_attestation=policy_attestation,
        )
        return sign_payload(response, self.worker_signing_key, key_version=self.worker_key_version)

    def handle_execute(self, body: bytes, *, now: int | None = None) -> bytes:
        current = int(time.time()) if now is None else now
        if len(body) > MAX_REQUEST_BYTES:
            raise WorkerProtocolError("request_too_large", "worker request exceeds the allowed size")
        payload = verify_envelope(body, self.host_verification_key, max_bytes=MAX_REQUEST_BYTES, key_version=self.host_key_version)
        try:
            request = WorkerRequest.model_validate_json(canonical_json(payload), strict=True)
        except ValidationError:
            raise WorkerProtocolError("invalid_message", "worker request is malformed") from None
        if request.worker_id != self.worker_id:
            raise WorkerProtocolError("wrong_worker", "request has another recipient")
        if request.issued_at > current + 15 or request.expires_at <= current:
            raise WorkerProtocolError("request_expired", "worker request is expired")
        self.replay_guard.consume(request.request_id, request.expires_at, now=current)
        if not isinstance(request.operation, CloseBrowser) and (not self.proxy_configured or not self.proxy_available()):
            raise WorkerProtocolError("proxy_unavailable", "managed proxy is unavailable")
        dispatch_time = int(time.time()) if now is None else now
        if dispatch_time < current or dispatch_time >= request.expires_at:
            raise WorkerProtocolError("request_expired", "worker authorization expired before dispatch")
        try:
            result = self.executor.execute(request)
            result = WorkerResult.model_validate(result)
        except Exception:
            result = WorkerResult(success=False, failure_code="worker_operation_failed")
        response = WorkerResponse(
            request_sha256=hashlib.sha256(body).hexdigest(),
            request_id=request.request_id, task_id=request.task_id, step_id=request.step_id,
            session_id=request.session_id, worker_id=self.worker_id, completed_at=int(time.time()), result=result,
        )
        encoded = sign_payload(response, self.worker_signing_key, key_version=self.worker_key_version)
        if len(encoded) > MAX_RESPONSE_BYTES:
            failure = response.model_copy(update={"result": WorkerResult(success=False, failure_code="worker_operation_failed")})
            encoded = sign_payload(failure, self.worker_signing_key, key_version=self.worker_key_version)
        return encoded


def build_https_server(service: WorkerService, *, bind_host: str, port: int, cert_file: str, key_file: str, client_ca: str | None = None) -> HTTPServer:
    """Build (but do not start) a TLS server bound only to an explicit host-only IP."""
    try:
        address = ipaddress.ip_address(bind_host)
        subnet = ipaddress.ip_network(WORKER_HOST_ONLY_NETWORK, strict=True)
    except ValueError:
        raise ValueError("worker bind address must be a literal host-only IP") from None
    if (address not in subnet or not address.is_private
            or address in {subnet.network_address, subnet.broadcast_address}
            or str(address) != "192.168.56.10" or port != 9443):
        raise ValueError("worker listener is outside its host-only policy")
    if not Path(cert_file).is_file() or not Path(key_file).is_file():
        raise ValueError("worker TLS certificate and key are required")
    cert_path = Path(cert_file)
    key_path = Path(key_file)
    configured_key = os.getenv("UNBOUND_WORKER_TLS_KEY_FILE")
    if cert_path.is_symlink() or key_path.is_symlink():
        raise ValueError("worker TLS key path or permissions are unsafe")
    if configured_key and Path(configured_key) == key_path:
        # systemd LoadCredential may expose a root-owned 0440 key with a
        # service-specific read ACL. Validate that exact protected credential
        # path with the same strict checks used for other worker secrets.
        _read_worker_file("UNBOUND_WORKER_TLS_KEY_FILE")
    elif stat.S_IMODE(key_path.stat().st_mode) & 0o077:
        raise ValueError("worker TLS key path or permissions are unsafe")

    if not client_ca:
        raise ValueError("dedicated host-client CA required")
    context = worker_tls_context(cert_file, key_file, client_ca)

    class Handler(BaseHTTPRequestHandler):
        server_version = "UNBOUNDWorker"
        sys_version = ""

        def log_message(self, _format, *_args):
            # Default HTTP logs include client-supplied paths and can leak data.
            return

        def do_POST(self):
            if self.path not in {"/v1/health", "/v1/execute"}:
                self.send_error(404)
                return
            if len(self.headers.get_all("Content-Length", [])) != 1 or "Transfer-Encoding" in self.headers:
                self.send_error(400)
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                self.send_error(400)
                return
            limit = 4096 if self.path.endswith("health") else MAX_REQUEST_BYTES
            if length < 1 or length > limit:
                self.send_error(413)
                return
            deadline = time.monotonic() + 5
            chunks = bytearray()
            while len(chunks) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.send_error(408)
                    return
                self.connection.settimeout(remaining)
                part = self.rfile.read1(min(65536, length - len(chunks)))
                if not part:
                    self.send_error(400)
                    return
                chunks.extend(part)
            body = bytes(chunks)
            self.connection.settimeout(5)
            try:
                response = (service.handle_health(body) if self.path.endswith("health")
                            else service.handle_execute(body))
            except WorkerProtocolError as exc:
                status = 401 if exc.code == "authentication_failed" else (503 if exc.code == "proxy_unavailable" else 400)
                self.send_error(status, exc.code)
                return
            except Exception:
                self.send_error(500, "worker_internal_error")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(response)

    # Serial control dispatch bounds concurrent work at this listener.
    class BoundedWorkerHTTPServer(HTTPServer):
        request_queue_size = 8

        def get_request(self):
            connection, client = super().get_request()
            try:
                if client[0] != "192.168.56.1":
                    raise PermissionError("unexpected control peer")
                connection.settimeout(5)
                return context.wrap_socket(connection, server_side=True), client
            except Exception:
                connection.close()
                raise

    return BoundedWorkerHTTPServer((str(address), port), Handler)


def worker_tls_context(cert_file, key_file, client_ca):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=client_ca)
    context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    return context


def main() -> int:
    """Opt-in entry point for a future guest deployment; never auto-started."""
    if os.getenv("UNBOUND_BROWSER_WORKER_ENABLE") != "1":
        raise SystemExit("UNBOUND browser worker is disabled; set explicit guest-only configuration to start it")
    # The networked worker service must never start with the offline fixture
    # renderer. That renderer is reserved for the separate disconnected demo.
    # Broker-fetch keeps Chromium in its AF_UNIX-only namespace and routes each
    # approved request through the authenticated host gateway.
    if os.getenv("UNBOUND_RENDERER_MODE") != "broker_fetch":
        raise SystemExit("Worker requires broker_fetch renderer mode")
    def read_secret(name: str) -> bytes:
        return _read_worker_file(name)

    fetch_server = None
    server = None
    try:
        host_key = public_key_from_pem(_read_worker_file(
            "UNBOUND_WORKER_HOST_PUBLIC_KEY_FILE", public_material=True
        ))
        worker_key = private_key_from_pem(read_secret("UNBOUND_WORKER_PRIVATE_KEY_FILE"))
        proxy_password = read_secret("UNBOUND_WORKER_PROXY_PASSWORD_FILE").decode("utf-8").strip()
        proxy_user = os.getenv("UNBOUND_WORKER_PROXY_USERNAME", "")
        proxy_endpoint = os.getenv("UNBOUND_WORKER_PROXY_ENDPOINT", "https://192.168.56.1:18765")
        worker_id = os.getenv("UNBOUND_WORKER_ID", "unbound-ubuntu-worker")
        bind_host = os.getenv("UNBOUND_WORKER_BIND_ADDRESS", "")
        port = int(os.getenv("UNBOUND_WORKER_PORT", "9443"))
        cert_file = os.getenv("UNBOUND_WORKER_TLS_CERT_FILE", "")
        tls_key_file = os.getenv("UNBOUND_WORKER_TLS_KEY_FILE", "")
        replay_db = os.getenv("UNBOUND_WORKER_REPLAY_DB", "/var/lib/unbound-browser-worker/replay.sqlite3")
        from backend.app.browser.worker_ipc import BrokerClient
        proxy_ca = os.environ.get("UNBOUND_WORKER_PROXY_CA_FILE")
        if not proxy_ca:
            raise ValueError("guest proxy CA is required")
        proxy_tls = ssl.create_default_context(cafile=proxy_ca)
        proxy_tls.minimum_version = ssl.TLSVersion.TLSv1_2
        from backend.app.browser.worker_fetch import BrokerFetchServer, FetchGrants, GatewayClient
        if proxy_user != "unbound":
            raise ValueError("gateway username required")
        # Read validates private-key ownership permissions before SSL opens it.
        read_secret("UNBOUND_WORKER_PROXY_CLIENT_KEY_FILE")
        client_cert = os.getenv("UNBOUND_WORKER_PROXY_CLIENT_CERT_FILE", "")
        if not Path(client_cert).is_file() or Path(client_cert).is_symlink():
            raise ValueError("gateway client certificate required")
        proxy_tls.load_cert_chain(client_cert, os.environ["UNBOUND_WORKER_PROXY_CLIENT_KEY_FILE"])
        proxy = GatewayClient(tls=proxy_tls, password=proxy_password, endpoint=proxy_endpoint)
        grants = FetchGrants()
        executor = BrokerClient(fetch_grants=grants)
        fetch_server = BrokerFetchServer(proxy, grants)
        if not Path(replay_db).is_file():
            raise ValueError("provisioned replay state required")
        service = WorkerService(
            worker_id=worker_id, host_verification_key=host_key, worker_signing_key=worker_key,
            executor=executor, replay_guard=ReplayGuard(replay_db),
            browser_available=executor.browser_ready, proxy_available=proxy.proxy_available,
            proxy_configured=True,
            host_key_version=int(os.getenv("UNBOUND_HOST_SIGNING_KEY_VERSION", "1")),
            worker_key_version=int(os.getenv("UNBOUND_WORKER_SIGNING_KEY_VERSION", "1")),
        )
        server = build_https_server(service, bind_host=bind_host, port=port,
                                    cert_file=cert_file, key_file=tls_key_file,
                                    client_ca=os.getenv("UNBOUND_WORKER_CLIENT_CA_FILE"))
        if fetch_server is not None:
            fetch_server.start()
    except (OSError, ValueError, WorkerProtocolError) as exc:
        if fetch_server is not None:
            fetch_server.close()
        if server is not None:
            server.server_close()
        raise SystemExit(f"Worker startup failed closed: {type(exc).__name__}") from None

    def stop(_signum, _frame):
        Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        if fetch_server is not None:
            fetch_server.close()
        # The renderer unit is PartOf this service. systemd cleans the cgroup.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
