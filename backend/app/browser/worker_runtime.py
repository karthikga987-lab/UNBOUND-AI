"""Explicit startup helpers for the host-only isolated-browser path.

Nothing binds or contacts a worker at import time. The FastAPI runtime calls
these helpers only when the operator selects isolated mode and supplies the
complete identity/configuration set.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
from threading import Thread

from backend.app.browser.egress import DestinationPolicy
from backend.app.browser.host_fetch_gateway import HostFetchGateway, build_https_server
from backend.app.browser.worker_protocol import (
    BrowserWorkerClient,
    HttpsWorkerTransport,
    private_key_from_pem,
    public_key_from_pem,
)


class WorkerConfigurationError(RuntimeError):
    """Safe configuration failure without paths or credential material."""


def _regular_file(directory: Path, name: str) -> Path:
    path = directory / name
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise OSError
        return path
    except OSError:
        raise WorkerConfigurationError("worker identity material is incomplete") from None


def build_worker_client(identity_directory: str | Path) -> BrowserWorkerClient:
    """Load the host-side identity and create a disabled-until-probed client."""
    directory = Path(identity_directory)
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise WorkerConfigurationError("worker identity directory is unavailable")
    manifest_path = _regular_file(directory, "manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expiry = datetime.fromisoformat(manifest["expires_at"].replace("Z", "+00:00"))
        if (manifest.get("purpose") != "development only" or expiry.tzinfo is None
                or expiry <= datetime.now(timezone.utc)):
            raise ValueError
        host_key = private_key_from_pem(_regular_file(directory, "host-signing-key.pem").read_bytes())
        worker_key = public_key_from_pem(_regular_file(directory, "worker-signing-public.pem").read_bytes())
        transport = HttpsWorkerTransport.from_identity_files(
            os.getenv("UNBOUND_WORKER_ENDPOINT", "https://192.168.56.10:9443"),
            guest_ca=str(_regular_file(directory, "worker-server-ca.pem")),
            client_cert=str(_regular_file(directory, "host-client-cert.pem")),
            client_key=str(_regular_file(directory, "host-client-key.pem")),
        )
        return BrowserWorkerClient(
            endpoint=transport.endpoint,
            host_signing_key=host_key,
            worker_verification_key=worker_key,
            worker_id=os.getenv("UNBOUND_WORKER_ID", "unbound-ubuntu-worker"),
            transport=transport,
            enabled=True,
        )
    except WorkerConfigurationError:
        raise
    except Exception:
        raise WorkerConfigurationError("worker identity validation failed") from None


@dataclass
class GatewayServerHandle:
    server: object
    thread: Thread

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("host-only fetch gateway did not stop")


def start_fetch_gateway(identity_directory: str | Path) -> tuple[GatewayServerHandle, HostFetchGateway]:
    """Start the mTLS/Basic authenticated host-only fetch API explicitly."""
    directory = Path(identity_directory)
    token_path = _regular_file(directory, "proxy.password")
    token = token_path.read_text(encoding="ascii").strip()
    gateway = HostFetchGateway(token=token, policy=DestinationPolicy())
    server = build_https_server(
        gateway,
        cert_file=_regular_file(directory, "gateway-server-cert.pem"),
        key_file=_regular_file(directory, "gateway-server-key.pem"),
        client_ca_file=_regular_file(directory, "broker-client-ca.pem"),
    )
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25},
                    name="unbound-host-fetch-gateway", daemon=True)
    thread.start()
    return GatewayServerHandle(server=server, thread=thread), gateway


def build_gateway_client_tls(identity_directory: str | Path) -> ssl.SSLContext:
    """Create the broker's guest-side TLS context with cert and hostname checks."""
    directory = Path(identity_directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cafile=str(_regular_file(directory, "gateway-server-ca.pem")))
    context.load_cert_chain(
        certfile=str(_regular_file(directory, "broker-client-cert.pem")),
        keyfile=str(_regular_file(directory, "broker-client-key.pem")),
    )
    return context
