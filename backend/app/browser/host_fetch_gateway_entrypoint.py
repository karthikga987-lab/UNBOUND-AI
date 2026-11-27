"""Standalone host-only fetch gateway entrypoint for a dedicated executable.

Packaging this module gives Windows Firewall a program identity separate from
FastAPI's python.exe. Installation and OS egress policy are separate gates.
"""
from __future__ import annotations

import os
from pathlib import Path

from backend.app.browser.egress import DestinationPolicy
from backend.app.browser.host_fetch_gateway import HostFetchGateway, build_https_server, load_gateway_token


def main() -> int:
    identity = os.environ.get("UNBOUND_WORKER_IDENTITY_DIRECTORY", "")
    directory = Path(identity)
    if not identity or not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("Gateway identity directory is unavailable")
    gateway = HostFetchGateway(
        token=load_gateway_token(directory / "proxy.password"),
        policy=DestinationPolicy(),
    )
    server = build_https_server(
        gateway,
        cert_file=directory / "gateway-server-cert.pem",
        key_file=directory / "gateway-server-key.pem",
        client_ca_file=directory / "broker-client-ca.pem",
    )
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
