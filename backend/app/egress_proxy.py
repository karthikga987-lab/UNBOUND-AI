"""Connection-pinned local HTTP proxy for browser egress.

The proxy is intentionally small and supports only HTTP/HTTPS (CONNECT on
443). DNS answers are validated as a complete set and the TCP socket connects
to the selected numeric address, avoiding a second resolver lookup.
"""
from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import base64
import ipaddress
import logging
import re
import secrets
import select
import socket
import socketserver
import ssl
import threading
from typing import Callable, Iterable
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)


class EgressDenied(ConnectionError):
    """A destination was rejected by the browser egress policy."""


Resolver = Callable[[str, int], Iterable[str]]
Connector = Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address, int, float], socket.socket]
DEFAULT_DOT_PORT = 853
_MAX_DNS_ANSWERS = 128

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


class DNSOverTLSResolver:
    """Resolve hostnames over authenticated DoT to an explicitly pinned IP.

    The configured resolver address is numeric, so establishing the TLS
    connection never needs the Windows system resolver. The TLS server name is
    independently checked against a certificate trusted by the OS CA store.
    """

    def __init__(
        self,
        resolver_ip: str,
        tls_server_hostname: str,
        *,
        timeout: float = 3.0,
        query_tls: Callable[..., object] | None = None,
    ) -> None:
        try:
            address = ipaddress.ip_address(resolver_ip)
        except (TypeError, ValueError):
            raise ValueError("DNS-over-TLS resolver address must be a numeric public IP") from None
        if not address.is_global or address.is_multicast or address.is_unspecified:
            raise ValueError("DNS-over-TLS resolver address must be a numeric public IP")
        if not isinstance(tls_server_hostname, str):
            raise ValueError("DNS-over-TLS TLS server hostname is invalid")
        try:
            hostname = tls_server_hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError("DNS-over-TLS TLS server hostname is invalid") from None
        if not _valid_dns_hostname(hostname):
            raise ValueError("DNS-over-TLS TLS server hostname is invalid")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0.1 <= float(timeout) <= 30:
            raise ValueError("DNS-over-TLS timeout must be between 0.1 and 30 seconds")
        self.resolver_ip = str(address)
        self.tls_server_hostname = hostname
        self.timeout = float(timeout)
        self._query_tls = query_tls
        self._ssl_context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        if self._ssl_context.verify_mode != ssl.CERT_REQUIRED or not self._ssl_context.check_hostname:
            raise ValueError("DNS-over-TLS certificate verification is required")
        try:
            import dns.message  # noqa: F401
            import dns.query  # noqa: F401
        except ImportError:
            raise RuntimeError("DNS-over-TLS dependency is unavailable") from None

    @classmethod
    def from_environment(cls) -> "DNSOverTLSResolver":
        import os

        resolver_ip = os.getenv("UNBOUND_DOT_RESOLVER_IP", "")
        tls_hostname = os.getenv("UNBOUND_DOT_TLS_SERVER_NAME", "")
        timeout_text = os.getenv("UNBOUND_DOT_TIMEOUT_SECONDS", "")
        if not resolver_ip or not tls_hostname or not timeout_text:
            raise ValueError("DNS-over-TLS resolver IP, TLS server name, and timeout must be configured")
        try:
            timeout = float(timeout_text)
        except (TypeError, ValueError):
            raise ValueError("DNS-over-TLS timeout configuration is invalid") from None
        return cls(resolver_ip, tls_hostname, timeout=timeout)

    def __call__(self, host: str, _port: int) -> tuple[str, ...]:
        try:
            import dns.exception
            import dns.flags
            import dns.message
            import dns.name
            import dns.opcode
            import dns.query
            import dns.rcode
            import dns.rdatatype
            import dns.rdataclass
        except ImportError:
            raise RuntimeError("DNS-over-TLS dependency is unavailable") from None

        query_tls = self._query_tls or dns.query.tls
        try:
            qname = dns.name.from_text(host, origin=dns.name.root).canonicalize()
            if qname == dns.name.root or len(qname.to_wire()) > 255:
                raise ValueError
        except Exception:
            raise ValueError("DNS query name is invalid") from None

        addresses: list[str] = []
        for rdtype in (dns.rdatatype.A, dns.rdatatype.AAAA):
            query = dns.message.make_query(qname, rdtype, rdclass=dns.rdataclass.IN)
            try:
                response = query_tls(
                    query,
                    where=self.resolver_ip,
                    timeout=self.timeout,
                    port=DEFAULT_DOT_PORT,
                    server_hostname=self.tls_server_hostname,
                    ssl_context=self._ssl_context,
                )
            except Exception:
                # Do not reveal the hostname, resolver response, or TLS details.
                raise RuntimeError("DNS-over-TLS query failed") from None
            if (
                not hasattr(query, "is_response")
                or not query.is_response(response)
                or response.opcode() != dns.opcode.QUERY
                or response.rcode() != dns.rcode.NOERROR
                or response.flags & dns.flags.TC
            ):
                raise RuntimeError("DNS-over-TLS response was invalid")

            reachable_names = {qname}
            for _ in range(16):
                changed = False
                for rrset in response.answer:
                    if rrset.rdclass == dns.rdataclass.IN and rrset.rdtype == dns.rdatatype.CNAME and rrset.name in reachable_names:
                        for record in rrset:
                            target = record.target.canonicalize()
                            if target not in reachable_names:
                                reachable_names.add(target)
                                changed = True
                if not changed:
                    break

            for rrset in response.answer:
                if (
                    rrset.rdclass == dns.rdataclass.IN
                    and rrset.rdtype == rdtype
                    and rrset.name in reachable_names
                ):
                    for record in rrset:
                        addresses.append(record.address)
                        if len(addresses) > _MAX_DNS_ANSWERS:
                            raise RuntimeError("DNS-over-TLS response exceeded answer limit")

        unique = tuple(dict.fromkeys(addresses))
        if not unique:
            raise RuntimeError("DNS-over-TLS returned no address records")
        return unique


def _offline_demo_resolver(_host: str, _port: int) -> tuple[str, ...]:
    raise EgressDenied("DNS is unavailable in local demo mode")


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
        resolver: Resolver | None = None,
        connector: Connector = _pinned_connector,
        connect_timeout: float = 10.0,
    ) -> None:
        self._resolver = resolver if resolver is not None else DNSOverTLSResolver.from_environment()
        self._connector = connector
        self._connect_timeout = connect_timeout

    def validate(self, hostname: str, port: int) -> ValidatedDestination:
        if not isinstance(hostname, str) or not hostname or port not in {80, 443}:
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
                if not _valid_dns_hostname(ascii_host):
                    raise ValueError("invalid hostname")
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
        logger.info("browser egress allowed port=%s resolved_count=%s", port, len(addresses))
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


def _valid_dns_hostname(host: str) -> bool:
    """Accept ordinary multi-label DNS names, never resolver shorthand."""
    if len(host) > 253 or not host or host.endswith("."):
        return False
    numeric_parts = r"(?:0[xX][0-9a-fA-F]+|[0-9]+)"
    if re.fullmatch(rf"{numeric_parts}(?:\.{numeric_parts}){{0,3}}", host):
        return False
    labels = host.split(".")
    if len(labels) < 2:
        return False
    for label in labels:
        if not 1 <= len(label) <= 63 or label.startswith("-") or label.endswith("-"):
            return False
        if not all(character.isascii() and (character.isalnum() or character == "-") for character in label):
            return False
    return not labels[-1].isdigit()


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
    # Fixed proxy endpoints must have a single owner; SO_REUSEADDR can let a
    # second process bind the same loopback port on Windows.
    allow_reuse_address = False
    request_queue_size = 64

    def __init__(self, address, handler, policy: DestinationPolicy, max_body_bytes: int, demo_only: bool, proxy_secret: str):
        self.policy = policy
        self.max_body_bytes = max_body_bytes
        self.demo_only = demo_only
        self.proxy_secret = proxy_secret
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

    def _deny(self, status: int = 403, *, authenticate: bool = False) -> None:
        try:
            self.send_response(status)
            if authenticate:
                self.send_header("Proxy-Authenticate", 'Basic realm="UNBOUND managed proxy"')
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError:
            pass

    def _require_proxy_authentication(self) -> None:
        values = self.headers.get_all("Proxy-Authorization", [])
        if len(values) != 1 or not values[0].startswith("Basic "):
            raise EgressDenied("Proxy authentication required")
        try:
            decoded = base64.b64decode(values[0][6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise EgressDenied("Proxy authentication required") from None
        expected = f"unbound:{self.server.proxy_secret}"  # type: ignore[attr-defined]
        import hmac
        if not hmac.compare_digest(decoded, expected):
            raise EgressDenied("Proxy authentication required")

    def do_CONNECT(self) -> None:
        upstream = None
        try:
            self._require_proxy_authentication()
            if self.demo_only:
                raise EgressDenied("HTTPS is disabled in local demo mode")
            host, port = _parse_authority(self.path)
            if port != 443:
                raise EgressDenied("Only HTTPS CONNECT on port 443 is allowed")
            upstream = self.policy.connect(host, port)
            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._tunnel(upstream)
        except EgressDenied as exc:
            logger.warning("browser egress denied category=%s", str(exc))
            needs_auth = str(exc) == "Proxy authentication required"
            self._deny(407 if needs_auth else 403, authenticate=needs_auth)
        except (OSError, TimeoutError):
            self._deny(502)
        finally:
            if upstream is not None:
                upstream.close()

    def _plain_http(self) -> None:
        upstream = None
        try:
            self._require_proxy_authentication()
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
        except EgressDenied as exc:
            logger.warning("browser egress denied category=%s", str(exc))
            needs_auth = str(exc) == "Proxy authentication required"
            self._deny(407 if needs_auth else 403, authenticate=needs_auth)
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
        port: int = 0,
        proxy_secret: str | None = None,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("proxy port must be between 0 and 65535")
        if policy is not None:
            self.policy = policy
        elif demo_only:
            # The fixture is served in-process and is deliberately independent
            # of DNS configuration and network access.
            self.policy = DestinationPolicy(resolver=_offline_demo_resolver)
        else:
            self.policy = DestinationPolicy()
        self.max_body_bytes = max_body_bytes
        self.demo_only = bool(demo_only)
        self.port = port
        self._proxy_secret = proxy_secret or secrets.token_urlsafe(32)
        if not isinstance(self._proxy_secret, str) or len(self._proxy_secret) < 32:
            raise ValueError("proxy secret must contain at least 32 characters")
        self._server: _ThreadedHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def server(self) -> str:
        if self._server is None:
            raise RuntimeError("Egress proxy is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def proxy_credentials(self) -> dict[str, str]:
        return {"username": "unbound", "password": self._proxy_secret}

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> "LocalEgressProxy":
        if self.is_running:
            return self
        server = _ThreadedHTTPServer(
            ("127.0.0.1", self.port), _ProxyHandler, self.policy, self.max_body_bytes,
            self.demo_only, self._proxy_secret,
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
