"""Stdio-controlled entry point for the packaged egress proxy process."""
from __future__ import annotations

import json
import logging
import os
import sys

from backend.app.egress_proxy import LocalEgressProxy


def _emit(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    """Read one private startup frame, then accept only parent-pipe shutdown."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    raw = sys.stdin.readline(16_385)
    if not raw or len(raw) > 16_384:
        _emit({"event": "error", "code": "invalid_startup_frame"})
        return 2
    try:
        config = json.loads(raw)
        if not isinstance(config, dict) or set(config) != {
            "host", "port", "demo_only", "proxy_secret", "max_body_bytes"
        }:
            raise ValueError
        if config["host"] != "127.0.0.1":
            raise ValueError
        port = config["port"]
        max_body = config["max_body_bytes"]
        secret = config["proxy_secret"]
        demo_only = config["demo_only"]
        if (
            not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
            or not isinstance(max_body, int) or isinstance(max_body, bool) or max_body <= 0
            or not isinstance(secret, str) or len(secret) < 32
            or not isinstance(demo_only, bool)
        ):
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError):
        _emit({"event": "error", "code": "invalid_startup_configuration"})
        return 2

    proxy = LocalEgressProxy(
        demo_only=demo_only,
        port=port,
        proxy_secret=secret,
        max_body_bytes=max_body,
    )
    try:
        proxy.start()
    except Exception as exc:
        _emit({"event": "error", "code": "proxy_start_failed", "error_type": type(exc).__name__})
        return 3

    _emit({
        "event": "ready",
        "pid": os.getpid(),
        "host": "127.0.0.1",
        "port": port,
    })
    try:
        for line in sys.stdin:
            if len(line) > 1024:
                return 4
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                return 4
            if command == {"command": "shutdown"}:
                break
            return 4
    finally:
        proxy.close()
    _emit({"event": "stopped", "pid": os.getpid()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
