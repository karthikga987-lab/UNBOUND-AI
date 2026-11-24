"""Connection-pinned local HTTP proxy for browser egress.

The proxy is intentionally small and supports only HTTP/HTTPS (CONNECT on
443). DNS answers are validated as a complete set and the TCP socket connects
to the selected numeric address, avoiding a second resolver lookup.
"""
from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import select
import socket
import socketserver
import threading
from typing import Callable, Iterable
from urllib.parse import urlsplit


class EgressDenied(ConnectionError):
    """A destination was rejected by the browser egress policy."""


Resolver = Callable[[str, int], Iterable[str]]
Connector = Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address, int, float], socket.socket]

_BLOCKED_HOSTS = {
    "localhost", "local", "metadata", "metadata.google.internal", "metadata.azure.internal",
    "instance-data.ec2.internal", "169.254.169.254", "168.63.129.16",
    "100.100.100.200",
}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_NAT64_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
_IPV6_TRANSITION_PREFIXES = (
    ipaddress.ip_network("2002::/16"),  # 6to4 can encode a non-public IPv4 relay.
    ipaddress.ip_network("2001::/32"),  # Teredo embeds client and relay IPv4 addresses.
)

LOCAL_DEMO_HOST = "unbound-demo.invalid"
LOCAL_DEMO_PRODUCT_URL = f"http://{LOCAL_DEMO_HOST}/product"
_LOCAL_DEMO_PAGES = {
    "/product": (
        "Demo Product",
        "<main><h1>Demo Product</h1><p>Version: 2.1</p>"
        "<p>Features: document search, task planning, evidence summaries</p>"
        "<p>All information on this page is synthetic.</p>"
        "<a href='/details'>View details</a></main>",
    ),
    "/details": (
        "Demo Product details",
        "<main><h1>Demo Product details</h1><p>Version: 2.1</p>"
        "<p>Features: document search, task planning, evidence summaries</p>"
        "<p>All information on this page is synthetic.</p></main>",
    ),
}


def classify_address(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True only for ordinary globally routable unicast addresses."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return classify_address(address.ipv4_mapped)
        if any(address in network for network in _IPV6_TRANSITION_PREFIXES):
            return False
        if any(address in network for network in _NAT64_PREFIXES):
            return False
    return bool(address.is_global and not address.is_multicast and not address.is_unspecified)


def _system_resolver(host: str, port: int) -> tuple[str, ...]:
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(answer[4][0] for answer in answers))


def _pinned_connector(address: ipaddress.IPv4Address | ipaddress.IPv6Address, port: int, timeout: float) -> socket.socket:
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sockaddr = (str(address), port, 0, 0) if family == socket.AF_INET6 else (str(address), port)
        sock.connect(sockaddr)
        peer = sock.getpeername()[0]
        if ipaddress.ip_address(peer) != address:
            raise EgressDenied("Connected peer did not match the validated address")
        return sock
    except Exception:
        sock.close()
        raise


@dataclass(frozen=True)
class ValidatedDestination:
    hostname: str
    port: int
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]


class DestinationPolicy:
    """Resolve, classify, and connect to a vetted numeric destination."""

    def __init__(
        self,
        *,
        resolver: Resolver = _system_resolver,
        connector: Connector = _pinned_connector,
        connect_timeout: float = 10.0,
    ) -> None:
        self._resolver = resolver
        self._connector = connector
        self._connect_timeout = connect_timeout

    def validate(self, hostname: str, port: int) -> ValidatedDestination:
        if not isinstance(hostname, str) or not hostname or not (1 <= port <= 65535):
            raise EgressDenied("Invalid destination")
        host = hostname.rstrip(".").lower()
        if not host or host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
            raise EgressDenied("Destination is not allowed")
        if host.endswith(".metadata.google.internal") or host.endswith(".ec2.internal"):
            raise EgressDenied("Destination is not allowed")
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            try:
                ascii_host = host.encode("idna").decode("ascii")
                raw_addresses = tuple(self._resolver(ascii_host, port))
            except Exception:
                raise EgressDenied("Destination could not be resolved safely") from None
        else:
            raw_addresses = (str(literal),)
        if not raw_addresses:
            raise EgressDenied("Destination returned no addresses")
        try:
            addresses = tuple(dict.fromkeys(ipaddress.ip_address(item) for item in raw_addresses))
        except ValueError:
            raise EgressDenied("Destination returned an invalid address") from None
        # Reject the entire DNS answer if any member is unsafe. Never select a
        # convenient public member from a mixed public/private answer.
        if not addresses or not all(classify_address(item) for item in addresses):
            raise EgressDenied("Destination includes a non-public address")
        return ValidatedDestination(host, port, addresses)

    def connect(self, hostname: str, port: int) -> socket.socket:
        destination = self.validate(hostname, port)
        last_error: Exception | None = None
        for address in destination.addresses:
            try:
                # The connector receives an already validated literal address;
                # it must not resolve the original hostname again.
                sock = self._connector(address, port, self._connect_timeout)
                peer = ipaddress.ip_address(sock.getpeername()[0])
                if peer != address:
                    sock.close()
                    raise EgressDenied("Connected peer did not match the validated address")
                return sock
            except EgressDenied:
                raise
            except Exception as exc:
                last_error = exc
        raise EgressDenied("Could not connect to a validated public address") from last_error


def _parse_authority(authority: str) -> tuple[str, int]:
    try:
        parts = urlsplit("//" + authority)
        host, port = parts.hostname, parts.port
    except ValueError:
        host, port = None, None
    if not host or port is None or parts.path or parts.query or parts.fragment:
        raise EgressDenied("Invalid proxy destination")
    return host, port


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, handler, policy: DestinationPolicy, max_body_bytes: int, demo_only: bool):
        self.policy = policy
        self.max_body_bytes = max_body_bytes
        self.demo_only = demo_only
        super().__init__(address, handler)


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "UNBOUND-Egress"
    sys_version = ""
    _header_limit = 64 * 1024

    @property
    def policy(self) -> DestinationPolicy:
        return self.server.policy  # type: ignore[attr-defined,no-any-return]

    @property
    def max_body_bytes(self) -> int:
        return self.server.max_body_bytes  # type: ignore[attr-defined,no-any-return]

    @property
    def demo_only(self) -> bool:
        return self.server.demo_only  # type: ignore[attr-defined,no-any-return]

    def log_message(self, _format: str, *_args) -> None:
        # Do not log URLs, hostnames, headers, or page-provided content.
        return

    def _deny(self, status: int = 403) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            pass

    def do_CONNECT(self) -> None:
        upstream = None
        try:
            if self.demo_only:
                raise EgressDenied("HTTPS is disabled in local demo mode")
            host, port = _parse_authority(self.path)
            if port != 443:
                raise EgressDenied("Only HTTPS CONNECT on port 443 is allowed")
            upstream = self.policy.connect(host, port)
            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._tunnel(upstream)
        except EgressDenied:
            self._deny(403)
        except (OSError, TimeoutError):
            self._deny(502)
        finally:
            if upstream is not None:
                upstream.close()

    def _plain_http(self) -> None:
        upstream = None
        try:
            target = urlsplit(self.path)
            if target.scheme.lower() != "http" or not target.hostname:
                raise EgressDenied("Only absolute HTTP destinations are supported")
            if target.username is not None or target.password is not None:
                raise EgressDenied("Credentials in proxy URLs are not allowed")
            if self.demo_only:
                self._serve_local_demo(target)
                return
            if self.headers.get("Upgrade"):
                raise EgressDenied("HTTP protocol upgrades are not supported by the egress proxy")
            if self.headers.get("Expect", "").lower() == "100-continue":
                self.send_response_only(100)
                self.end_headers()
            port = target.port or 80
            if port != 80:
                raise EgressDenied("Only HTTP on port 80 is allowed")
            host_header = self.headers.get_all("Host", [])
            if len(host_header) != 1:
                raise EgressDenied("Invalid Host header")
            parsed_host, parsed_port = _parse_http_host(host_header[0])
            if parsed_host.rstrip(".").lower() != target.hostname.rstrip(".").lower() or parsed_port not in (None, 80):
                raise EgressDenied("Host header does not match request destination")
            upstream = self.policy.connect(target.hostname, port)
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            headers = []
            for name, value in self.headers.items():
                if name.lower() in {"connection", "proxy-connection", "proxy-authorization", "proxy-authenticate", "expect"}:
                    continue
                headers.append(f"{name}: {value}\r\n".encode("latin-1"))
            request_line = f"{self.command} {path} {self.request_version}\r\n".encode("ascii")
            upstream.sendall(request_line + b"".join(headers) + b"Connection: close\r\n\r\n")
            self._copy_request_body(upstream)
            self._tunnel(upstream)
        except EgressDenied:
            self._deny(403)
        except (OSError, TimeoutError, ValueError):
            self._deny(502)
        finally:
            if upstream is not None:
                upstream.close()

    def _serve_local_demo(self, target) -> None:
        """Serve fixed synthetic pages and deny every network destination."""
        host_header = self.headers.get_all("Host", [])
        if (
            self.command not in {"GET", "HEAD"}
            or target.hostname.rstrip(".").lower() != LOCAL_DEMO_HOST
            or (target.port or 80) != 80
            or len(host_header) != 1
        ):
            raise EgressDenied("Request is outside the local demo fixture")
        host, port = _parse_http_host(host_header[0])
        if host.rstrip(".").lower() != LOCAL_DEMO_HOST or port not in (None, 80):
            raise EgressDenied("Request is outside the local demo fixture")
        path = target.path or "/"
        if target.query or target.fragment:
            raise EgressDenied("Query strings are disabled for the local demo fixture")
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        page = _LOCAL_DEMO_PAGES.get(path)
        if page is None:
            raise EgressDenied("Path is outside the local demo fixture")
        title, body = page
        content = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title><meta http-equiv='Content-Security-Policy' "
            "content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'\"></head>"
            f"<body>{body}</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    do_GET = _plain_http
    do_POST = _plain_http
    do_HEAD = _plain_http
    do_PUT = _plain_http
    do_PATCH = _plain_http
    do_DELETE = _plain_http
    do_OPTIONS = _plain_http

    def _copy_request_body(self, upstream: socket.socket) -> None:
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        content_length = self.headers.get("Content-Length")
        if transfer and transfer != "chunked":
            raise EgressDenied("Unsupported transfer encoding")
        if transfer and content_length is not None:
            raise EgressDenied("Ambiguous request framing")
        if transfer == "chunked":
            total = 0
            while True:
                line = self.rfile.readline(8192)
                if not line or not line.endswith(b"\r\n"):
                    raise EgressDenied("Invalid chunked request")
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise EgressDenied("Invalid chunked request") from None
                total += size
                if total > self.max_body_bytes:
                    raise EgressDenied("Request body exceeds proxy limit")
                upstream.sendall(line)
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(8192)
                        if not trailer or not trailer.endswith(b"\r\n"):
                            raise EgressDenied("Invalid chunk trailer")
                        upstream.sendall(trailer)
                        if trailer == b"\r\n":
                            return
                chunk = self.rfile.read(size + 2)
                if len(chunk) != size + 2 or not chunk.endswith(b"\r\n"):
                    raise EgressDenied("Invalid chunked request")
                upstream.sendall(chunk)
        elif content_length is not None:
            try:
                length = int(content_length)
            except ValueError:
                raise EgressDenied("Invalid request body length") from None
            if length < 0 or length > self.max_body_bytes:
                raise EgressDenied("Request body exceeds proxy limit")
            remaining = length
            while remaining:
                block = self.rfile.read(min(64 * 1024, remaining))
                if not block:
                    raise EgressDenied("Incomplete request body")
                upstream.sendall(block)
                remaining -= len(block)

    def _tunnel(self, upstream: socket.socket) -> None:
        upstream.settimeout(30.0)
        self.connection.settimeout(30.0)
        sockets = [self.connection, upstream]
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 30.0)
            if exceptional or not readable:
                return
            for source in readable:
                try:
                    data = source.recv(64 * 1024)
                except OSError:
                    return
                if not data:
                    return
                destination = upstream if source is self.connection else self.connection
                destination.sendall(data)


def _parse_http_host(value: str) -> tuple[str, int | None]:
    try:
        parts = urlsplit("//" + value.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        host, port = None, None
    if not host or parts.path or parts.query or parts.fragment:
        raise EgressDenied("Invalid Host header")
    return host, port


class LocalEgressProxy:
    """Loopback-only, managed proxy used by a single Playwright runtime."""

    def __init__(
        self,
        *,
        policy: DestinationPolicy | None = None,
        max_body_bytes: int = 20 * 1024 * 1024,
        demo_only: bool = False,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.policy = policy or DestinationPolicy()
        self.max_body_bytes = max_body_bytes
        self.demo_only = bool(demo_only)
        self._server: _ThreadedHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def server(self) -> str:
        if self._server is None:
            raise RuntimeError("Egress proxy is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> "LocalEgressProxy":
        if self.is_running:
            return self
        server = _ThreadedHTTPServer(
            ("127.0.0.1", 0), _ProxyHandler, self.policy, self.max_body_bytes, self.demo_only
        )
        thread = threading.Thread(target=server.serve_forever, name="unbound-egress-proxy", daemon=True)
        try:
            thread.start()
        except Exception:
            server.server_close()
            raise
        self._server, self._thread = server, thread
        return self

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)

    shutdown = close
