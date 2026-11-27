"""Authenticated, GET-only HTTPS gateway for the isolated Ubuntu browser worker.

This is deliberately not a general-purpose proxy: it accepts one JSON fetch
operation for a small exact hostname allowlist and never exposes CONNECT or
arbitrary tunnelling. Importing this module creates no listener.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import ssl
import stat
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from backend.app.browser.egress import DestinationPolicy, EgressDenied


DEFAULT_BIND_ADDRESS = "192.168.56.1"
DEFAULT_BIND_PORT = 18765
EXPECTED_GUEST_ADDRESS = "192.168.56.10"
GATEWAY_USERNAME = "unbound"
ALLOWED_HOSTS = frozenset({"example.com", "www.python.org", "docs.python.org"})
MAX_REQUEST_BYTES = 8 * 1024
MAX_URL_BYTES = 4 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_HEADER_BYTES = 32 * 1024
MAX_HEADER_COUNT = 64
MAX_LINE_BYTES = 8 * 1024

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_HEADER_NAME_RE = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_STATUS_RE = re.compile(rb"^HTTP/1\.[01] ([1-5][0-9]{2})(?: [^\r\n]{0,512})?\r\n$")
_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9!#$&^_.+-]{1,128}/[a-z0-9!#$&^_.+-]{1,128}$")


class GatewayRequestError(ValueError):
    """A safe client-facing request rejection with no sensitive detail."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


class GatewayUpstreamError(RuntimeError):
    """An upstream failure whose message never includes URL or page data."""

    def __init__(self, code: str = "upstream_failed"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ValidatedFetchURL:
    url: str
    hostname: str
    request_target: str


def validate_fetch_url(value: Any) -> ValidatedFetchURL:
    """Accept only unambiguous HTTPS URLs for the configured exact hosts."""
    if not isinstance(value, str) or not value or len(value.encode("utf-8", "ignore")) > MAX_URL_BYTES:
        raise GatewayRequestError("invalid_url")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f or ch.isspace() for ch in value):
        raise GatewayRequestError("invalid_url")
    if "\\" in value or "#" in value or re.search(r"%0[ad]", value, flags=re.IGNORECASE):
        raise GatewayRequestError("invalid_url")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise GatewayRequestError("invalid_url") from None
    if parts.scheme.casefold() != "https" or not parts.netloc or parts.username is not None or parts.password is not None:
        raise GatewayRequestError("invalid_url")
    hostname = parts.hostname
    if not hostname:
        raise GatewayRequestError("invalid_url")
    host = hostname.casefold()
    # Exact authority matching excludes IP literals, IDNs, trailing-dot aliases,
    # percent-encoded hostnames, userinfo, alternate numeric forms and odd ports.
    if host not in ALLOWED_HOSTS or parts.netloc.casefold() not in {host, f"{host}:443"} or port not in (None, 443):
        raise GatewayRequestError("destination_not_allowed", 403)
    path = parts.path or "/"
    target = path + (f"?{parts.query}" if parts.query else "")
    try:
        target.encode("ascii")
    except UnicodeEncodeError:
        raise GatewayRequestError("invalid_url") from None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in target) or re.search(r"%(?![0-9A-Fa-f]{2})", target):
        raise GatewayRequestError("invalid_url")
    canonical = urlunsplit(("https", host, path, parts.query, ""))
    return ValidatedFetchURL(canonical, host, target)


def load_gateway_token(path: str | Path) -> str:
    """Read an opaque token from a non-symlink file without exposing its value."""
    token_path = Path(path)
    try:
        info = token_path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError
        token = token_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError("gateway token file is unavailable or unsafe") from None
    if not _TOKEN_RE.fullmatch(token):
        raise RuntimeError("gateway token file is unavailable or unsafe")
    return token


def authenticate_control_request(peer_ip: str, authorization_values: list[str], token: str) -> bool:
    """Require the exact guest address and one constant-time Basic credential."""
    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    if str(peer) != EXPECTED_GUEST_ADDRESS or len(authorization_values) != 1:
        return False
    header = authorization_values[0]
    if not isinstance(header, str) or not header.startswith("Basic "):
        return False
    try:
        supplied = base64.b64decode(header[6:].encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return False
    expected = f"{GATEWAY_USERNAME}:{token}".encode("ascii")
    return hmac.compare_digest(supplied, expected)


def _bounded_line(reader, limit: int = MAX_LINE_BYTES) -> bytes:
    line = reader.readline(limit + 1)
    if len(line) > limit or not line.endswith(b"\r\n"):
        raise GatewayUpstreamError("invalid_upstream_response")
    return line


def _read_exact(reader, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        part = reader.read(min(64 * 1024, size - len(output)))
        if not part:
            raise GatewayUpstreamError("invalid_upstream_response")
        output.extend(part)
    return bytes(output)


def _read_chunked_body(reader) -> bytes:
    body = bytearray()
    while True:
        line = _bounded_line(reader, 256)
        size_text = line[0:-2].split(b";", 1)[0]
        if not size_text or not re.fullmatch(rb"[0-9A-Fa-f]+", size_text):
            raise GatewayUpstreamError("invalid_upstream_response")
        size = int(size_text, 16)
        if size == 0:
            trailer_bytes = 0
            for _ in range(MAX_HEADER_COUNT):
                trailer = _bounded_line(reader)
                trailer_bytes += len(trailer)
                if trailer_bytes > MAX_HEADER_BYTES:
                    raise GatewayUpstreamError("invalid_upstream_response")
                if trailer == b"\r\n":
                    return bytes(body)
                if b":" not in trailer or trailer[:1] in (b" ", b"\t"):
                    raise GatewayUpstreamError("invalid_upstream_response")
            raise GatewayUpstreamError("invalid_upstream_response")
        if size > MAX_RESPONSE_BYTES - len(body):
            raise GatewayUpstreamError("response_too_large")
        body.extend(_read_exact(reader, size))
        if _read_exact(reader, 2) != b"\r\n":
            raise GatewayUpstreamError("invalid_upstream_response")


def _read_http_response(stream) -> tuple[int, dict[str, list[str]], bytes]:
    """Parse a bounded origin response; reject ambiguous or unbounded framing."""
    try:
        reader = stream.makefile("rb")
    except (AttributeError, OSError):
        raise GatewayUpstreamError("invalid_upstream_response") from None
    try:
        status_line = _bounded_line(reader, 4096)
        match = _STATUS_RE.fullmatch(status_line)
        if not match:
            raise GatewayUpstreamError("invalid_upstream_response")
        status = int(match.group(1))
        if status < 200:
            raise GatewayUpstreamError("invalid_upstream_response")
        headers: dict[str, list[str]] = {}
        total = 0
        for _ in range(MAX_HEADER_COUNT):
            line = _bounded_line(reader)
            total += len(line)
            if total > MAX_HEADER_BYTES:
                raise GatewayUpstreamError("invalid_upstream_response")
            if line == b"\r\n":
                break
            field, separator, raw_value = line[:-2].partition(b":")
            if not separator or not _HEADER_NAME_RE.fullmatch(field) or line[:1] in (b" ", b"\t"):
                raise GatewayUpstreamError("invalid_upstream_response")
            try:
                name = field.decode("ascii").lower()
                value = raw_value.strip().decode("latin-1")
            except UnicodeError:
                raise GatewayUpstreamError("invalid_upstream_response") from None
            if any(ord(ch) < 0x20 and ch != "\t" or ord(ch) == 0x7f for ch in value):
                raise GatewayUpstreamError("invalid_upstream_response")
            headers.setdefault(name, []).append(value)
        else:
            raise GatewayUpstreamError("invalid_upstream_response")

        content_lengths = headers.get("content-length", [])
        transfer_encodings = headers.get("transfer-encoding", [])
        if len(content_lengths) > 1 or len(transfer_encodings) > 1 or (content_lengths and transfer_encodings):
            raise GatewayUpstreamError("invalid_upstream_response")
        if transfer_encodings:
            if transfer_encodings[0].casefold() != "chunked":
                raise GatewayUpstreamError("invalid_upstream_response")
            body = _read_chunked_body(reader)
        elif content_lengths:
            value = content_lengths[0]
            if not re.fullmatch(r"[0-9]{1,10}", value):
                raise GatewayUpstreamError("invalid_upstream_response")
            size = int(value)
            if size > MAX_RESPONSE_BYTES:
                raise GatewayUpstreamError("response_too_large")
            body = _read_exact(reader, size)
        elif status in {204, 304}:
            body = b""
        else:
            raise GatewayUpstreamError("unbounded_response")
        return status, headers, body
    finally:
        reader.close()


class HostFetchGateway:
    """A single-operation fetcher; it has no listener until explicitly served."""

    def __init__(self, *, token: str, policy: DestinationPolicy, tls_context: ssl.SSLContext | None = None,
                 timeout_seconds: float = 12.0):
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise ValueError("gateway token is invalid")
        if not 0.5 <= timeout_seconds <= 30:
            raise ValueError("gateway timeout must be between 0.5 and 30 seconds")
        context = tls_context or ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError("origin TLS certificate and hostname verification are required")
        self.token = token
        self.policy = policy
        self.tls_context = context
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls, *, policy: DestinationPolicy | None = None) -> "HostFetchGateway":
        token_path = os.getenv("UNBOUND_FETCH_GATEWAY_TOKEN_FILE", "")
        if not token_path:
            raise RuntimeError("gateway token file configuration is required")
        selected_policy = policy if policy is not None else DestinationPolicy()
        return cls(token=load_gateway_token(token_path), policy=selected_policy)

    def fetch(self, url: str) -> dict[str, Any]:
        requested = validate_fetch_url(url)
        raw_socket = None
        secure_socket = None
        try:
            raw_socket = self.policy.connect(requested.hostname, 443)
            raw_socket.settimeout(self.timeout_seconds)
            secure_socket = self.tls_context.wrap_socket(raw_socket, server_hostname=requested.hostname)
            secure_socket.settimeout(self.timeout_seconds)
            request = (
                f"GET {requested.request_target} HTTP/1.1\r\n"
                f"Host: {requested.hostname}\r\n"
                "User-Agent: UNBOUND-Isolated-Fetch/1.0\r\n"
                "Accept: text/html,application/xhtml+xml,application/json;q=0.8,*/*;q=0.2\r\n"
                "Accept-Encoding: identity\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            secure_socket.sendall(request)
            status, headers, body = _read_http_response(secure_socket)
        except (EgressDenied, GatewayUpstreamError):
            raise
        except (OSError, ssl.SSLError, TimeoutError, ValueError):
            raise GatewayUpstreamError() from None
        finally:
            if secure_socket is not None:
                try:
                    secure_socket.close()
                except OSError:
                    pass
            elif raw_socket is not None:
                try:
                    raw_socket.close()
                except OSError:
                    pass

        content_types = headers.get("content-type", [])
        if len(content_types) > 1:
            raise GatewayUpstreamError("invalid_upstream_response")
        content_type = "application/octet-stream"
        if content_types:
            candidate = content_types[0].split(";", 1)[0].strip().casefold()
            if _MEDIA_TYPE_RE.fullmatch(candidate):
                content_type = candidate

        locations = headers.get("location", [])
        if len(locations) > 1:
            raise GatewayUpstreamError("invalid_upstream_response")
        result: dict[str, Any] = {
            "url": requested.url,
            "status": status,
            "content_type": content_type,
            "body_base64": base64.b64encode(body).decode("ascii"),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        if locations:
            target = urljoin(requested.url, locations[0])
            redirect = validate_fetch_url(target)
            # Validate the whole DNS answer now. The browser must request the
            # returned target again through this same gateway; we never follow it.
            try:
                self.policy.validate(redirect.hostname, 443)
            except EgressDenied:
                raise GatewayRequestError("redirect_destination_not_allowed", 403) from None
            result["location"] = redirect.url
        return result


def build_server_tls_context(cert_file: str | Path, key_file: str | Path,
                             client_ca_file: str | Path) -> ssl.SSLContext:
    cert_path, key_path, ca_path = Path(cert_file), Path(key_file), Path(client_ca_file)
    for path in (cert_path, key_path, ca_path):
        try:
            info = path.lstat()
        except OSError:
            raise RuntimeError("gateway TLS configuration is unavailable") from None
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError("gateway TLS configuration is unavailable")
    if os.name != "nt" and stat.S_IMODE(key_path.stat().st_mode) & 0o077:
        raise RuntimeError("gateway TLS configuration is unavailable")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(ca_path))
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


class _GatewayServer(HTTPServer):
    allow_reuse_address = False
    request_queue_size = 8

    def __init__(self, gateway: HostFetchGateway, server_address: tuple[str, int], tls_context: ssl.SSLContext):
        self.gateway = gateway
        super().__init__(server_address, _GatewayHandler, bind_and_activate=True)
        try:
            self.socket = tls_context.wrap_socket(self.socket, server_side=True)
        except Exception:
            self.server_close()
            raise

    def get_request(self):
        request, address = super().get_request()
        if not isinstance(address, tuple) or not address or address[0] != EXPECTED_GUEST_ADDRESS:
            request.close()
            raise PermissionError("unexpected gateway peer")
        request.settimeout(self.gateway.timeout_seconds)
        return request, address

    def handle_error(self, request, client_address):
        # Avoid logging request targets, TLS details, or peer-supplied content.
        return


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "UNBOUND-Fetch-Gateway"
    sys_version = ""

    @property
    def gateway(self) -> HostFetchGateway:
        return self.server.gateway  # type: ignore[attr-defined,no-any-return]

    def log_message(self, _format: str, *_args) -> None:
        return

    def handle_expect_100(self) -> bool:
        self._reply(417, {"error": "expectation_not_supported"})
        return False

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass
        self.close_connection = True

    def _authorized(self) -> bool:
        values = self.headers.get_all("Authorization", [])
        if not authenticate_control_request(self.client_address[0], values, self.gateway.token):
            self._reply(401, {"error": "authentication_failed"})
            return False
        if len(self.headers.as_bytes()) > MAX_HEADER_BYTES or len(self.headers) > MAX_HEADER_COUNT:
            self._reply(431, {"error": "headers_too_large"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._authorized():
            return
        if self.path != "/v1/health" or self.headers.get("Transfer-Encoding"):
            self._reply(404, {"error": "not_found"})
            return
        content_lengths = self.headers.get_all("Content-Length", [])
        if len(content_lengths) > 1 or (content_lengths and content_lengths[0] != "0"):
            self._reply(400, {"error": "invalid_request"})
            return
        self._reply(200, {"status": "ok"})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if self.path != "/v1/fetch":
            self._reply(404, {"error": "not_found"})
            return
        if self.headers.get("Transfer-Encoding"):
            self._reply(400, {"error": "invalid_request"})
            return
        content_types = self.headers.get_all("Content-Type", [])
        lengths = self.headers.get_all("Content-Length", [])
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().casefold() != "application/json":
            self._reply(415, {"error": "unsupported_content_type"})
            return
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,5}", lengths[0]):
            self._reply(400, {"error": "invalid_request"})
            return
        length = int(lengths[0])
        if length < 1 or length > MAX_REQUEST_BYTES:
            self._reply(413, {"error": "request_too_large"})
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise GatewayRequestError("invalid_request")
            payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
            if not isinstance(payload, dict) or set(payload) != {"url", "method"} or payload.get("method") != "GET":
                raise GatewayRequestError("invalid_request")
            result = self.gateway.fetch(payload["url"])
        except GatewayRequestError as exc:
            self._reply(exc.status, {"error": exc.code})
            return
        except EgressDenied:
            self._reply(403, {"error": "destination_denied"})
            return
        except GatewayUpstreamError as exc:
            self._reply(502, {"error": exc.code})
            return
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
            self._reply(400, {"error": "invalid_request"})
            return
        self._reply(200, result)

    def _method_not_allowed(self) -> None:
        self._reply(405, {"error": "method_not_allowed"})

    do_HEAD = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_CONNECT = _method_not_allowed


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON member")
        result[name] = value
    return result


def build_https_server(gateway: HostFetchGateway, *, cert_file: str | Path, key_file: str | Path,
                       client_ca_file: str | Path, bind_address: str = DEFAULT_BIND_ADDRESS,
                       port: int = DEFAULT_BIND_PORT) -> HTTPServer:
    """Construct (but do not start) the narrowly bound mutual-TLS listener."""
    try:
        address = ipaddress.ip_address(bind_address)
    except ValueError:
        raise ValueError("gateway bind address must be the fixed host-only address") from None
    if str(address) != DEFAULT_BIND_ADDRESS or type(port) is not int or port != DEFAULT_BIND_PORT:
        raise ValueError("gateway listener must use the fixed host-only endpoint")
    tls_context = build_server_tls_context(cert_file, key_file, client_ca_file)
    return _GatewayServer(gateway, (DEFAULT_BIND_ADDRESS, DEFAULT_BIND_PORT), tls_context)
