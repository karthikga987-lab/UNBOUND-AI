"""Explicit Browserless Cloud demo controller.

This provider is a separately labelled cloud demo. Its Playwright request guard
is application-level policy only and does not satisfy the self-hosted Windows
or Ubuntu process-containment gates.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import time
from typing import Callable, Iterable
from urllib.parse import urlencode, urlsplit, urlunsplit

from backend.app.browser.controller import BrowserController, BrowserControllerError, _validate_http_url


_EXPLICIT_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\])}>]+", re.IGNORECASE)
_BARE_DNS_NAME = re.compile(
    r"(?<![@\w-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)
_COMMON_PUBLIC_SUFFIXES = frozenset({
    "ai", "app", "au", "ca", "com", "co", "de", "dev", "edu", "es", "eu",
    "fr", "gov", "info", "int", "io", "it", "jp", "net", "nl", "org", "uk",
})


def normalize_allowed_hosts(raw: str | Iterable[str] | None) -> frozenset[str]:
    if raw is None:
        values = ["example.com"]
    elif isinstance(raw, str):
        values = raw.split(",")
    else:
        values = list(raw)
    hosts: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Browserless hostname allowlist contains an invalid entry")
        candidate = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
        if (
            len(candidate) > 253
            or "." not in candidate
            or candidate.startswith(".")
            or candidate.endswith(".")
            or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                   or not all(char.isalnum() or char == "-" for char in label)
                   for label in candidate.split("."))
        ):
            raise ValueError("Browserless hostname allowlist contains an invalid entry")
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            pass
        else:
            raise ValueError("Browserless hostname allowlist must contain DNS names, not IP addresses")
        hosts.add(candidate)
    if not hosts:
        raise ValueError("Browserless hostname allowlist must not be empty")
    return frozenset(hosts)


def validate_browserless_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("Browserless WebSocket endpoint is required")
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        raise ValueError("Browserless WebSocket endpoint is invalid") from None
    try:
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        ipaddress.ip_address(host)
    except ValueError:
        is_ip = False
    else:
        is_ip = True
    if (
        parts.scheme != "wss"
        or not host
        or is_ip
        or parts.username is not None
        or parts.password is not None
        or parts.path.rstrip("/") != "/chromium/playwright"
        or parts.query
        or parts.fragment
        or port not in {None, 443}
        or host in {"localhost", "localhost.localdomain"}
        or host.endswith((".localhost", ".local", ".internal"))
    ):
        raise ValueError("Browserless endpoint must be a credential-free wss /chromium/playwright URL on port 443")
    return urlunsplit(("wss", parts.netloc, "/chromium/playwright", "", ""))


def validate_cloud_target(
    url: str,
    allowed_hosts: Iterable[str],
    resolver: Callable[..., list] | None = None,
) -> str:
    value = _validate_http_url(url)
    parts = urlsplit(value)
    try:
        hostname = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = parts.port
    except (ValueError, UnicodeError):
        raise BrowserControllerError("policy", "Destination URL is invalid") from None
    if (
        parts.scheme.lower() != "https"
        or port not in {None, 443}
        or parts.username is not None
        or parts.password is not None
        or hostname not in allowed_hosts
    ):
        raise BrowserControllerError("policy", "Destination is outside the HTTPS hostname allowlist")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise BrowserControllerError("policy", "IP-literal destinations are not permitted")
    try:
        resolve = resolver or socket.getaddrinfo
        answers = resolve(hostname, 443, type=socket.SOCK_STREAM)
        addresses = {ipaddress.ip_address(answer[4][0].split("%", 1)[0]) for answer in answers}
    except Exception:
        raise BrowserControllerError("policy", "Destination DNS validation failed") from None
    if not addresses or any(not address.is_global for address in addresses):
        raise BrowserControllerError("policy", "Destination resolved to a prohibited network address")
    return value


def browserless_hostname(url: str) -> str:
    """Return a canonical exact hostname after URL-shape validation (no DNS lookup)."""
    value = _validate_http_url(url)
    try:
        parts = urlsplit(value)
        hostname = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = parts.port
    except (ValueError, UnicodeError):
        raise BrowserControllerError("policy", "Destination URL is invalid") from None
    if (
        parts.scheme.lower() != "https"
        or port not in {None, 443}
        or parts.username is not None
        or parts.password is not None
    ):
        raise BrowserControllerError("policy", "Only credential-free HTTPS destinations on port 443 are permitted")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise BrowserControllerError("policy", "IP-literal destinations are not permitted")
    try:
        return next(iter(normalize_allowed_hosts([hostname])))
    except (TypeError, ValueError):
        raise BrowserControllerError("policy", "Destination hostname is invalid") from None


def explicit_browserless_hosts(user_request: str) -> frozenset[str]:
    """Extract only literal HTTPS URLs and exact DNS names typed by the user.

    This is review metadata, not authorization. Effective permission is granted
    only by the explicit Run plan endpoint and is limited to the resulting task.
    Names found in webpage content are never passed here.
    """
    hosts: set[str] = set()
    urls = list(_EXPLICIT_URL.finditer(user_request))
    for match in urls:
        candidate = match.group(0).rstrip(".,;:!?)]}>")
        hosts.add(browserless_hostname(candidate))
    for match in _BARE_DNS_NAME.finditer(user_request):
        candidate = match.group(0).rstrip(".")
        if candidate.rsplit(".", 1)[-1].casefold() not in _COMMON_PUBLIC_SUFFIXES:
            continue
        try:
            host = next(iter(normalize_allowed_hosts([candidate])))
        except (TypeError, ValueError):
            continue
        hosts.add(host)
    return frozenset(hosts)


class BrowserlessController(BrowserController):
    """One remote Browserless Playwright connection per task-scoped session."""

    provider_name = "browserless_cloud_demo"

    def __init__(
        self,
        *,
        api_token: str,
        ws_endpoint: str,
        allowed_hosts: Iterable[str] = ("example.com",),
        timeout_ms: int = 15_000,
        resolver: Callable[..., list] | None = None,
        on_connection_status: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(headless=True, timeout_ms=timeout_ms)
        if not isinstance(api_token, str) or not api_token.strip() or len(api_token) > 4096:
            raise ValueError("Browserless API token is missing or invalid")
        self._api_token = api_token.strip()
        self._base_endpoint = validate_browserless_endpoint(ws_endpoint)
        self._baseline_allowed_hosts = normalize_allowed_hosts(allowed_hosts)
        self.allowed_hosts = self._baseline_allowed_hosts
        self._resolver = resolver or socket.getaddrinfo
        self._on_connection_status = on_connection_status
        self._last_policy_rejection: dict[str, str] | None = None

    def set_task_allowed_hosts(self, hosts: Iterable[str]) -> None:
        """Set this task session's exact allowlist while retaining configured hosts."""
        requested = normalize_allowed_hosts(hosts)
        self.allowed_hosts = frozenset(self._baseline_allowed_hosts | requested)
        self._last_policy_rejection = None

    @property
    def last_policy_rejection(self) -> dict[str, str] | None:
        """Sanitized reason for the most recent app-layer request rejection."""
        return dict(self._last_policy_rejection) if self._last_policy_rejection else None

    def _validate_cloud_request(self, url: str) -> str:
        try:
            return validate_cloud_target(url, self.allowed_hosts, self._resolver)
        except BrowserControllerError as exc:
            # Keep only the validator's fixed safe reason. Never retain the
            # complete URL, query, page content, token, or DNS answers.
            self._last_policy_rejection = {
                "category": "destination_policy_rejected",
                "reason": exc.reason,
            }
            raise

    def _report_connection(self, status: str) -> None:
        if self._on_connection_status is not None:
            try:
                self._on_connection_status(status)
            except Exception:
                pass

    def start(self) -> "BrowserlessController":
        if self.is_started:
            return self
        sync_playwright = self._load_playwright()
        try:
            self._playwright = sync_playwright().start()
            endpoint = f"{self._base_endpoint}?{urlencode({'token': self._api_token})}"
            self._browser = self._playwright.chromium.connect(endpoint, timeout=self.timeout_ms)
            self._api_token = ""
            self._context = self._browser.new_context(service_workers="block", accept_downloads=False)
            self._context.route("**/*", self._guard_cloud_request)
            route_web_socket = getattr(self._context, "route_web_socket", None)
            if not callable(route_web_socket):
                raise BrowserControllerError("connect", "Browserless runtime lacks required WebSocket request controls")
            route_web_socket("**/*", lambda websocket: websocket.close())
            self._context.add_init_script(
                "Object.defineProperty(window, 'RTCPeerConnection', { value: undefined, configurable: false });"
                "Object.defineProperty(window, 'webkitRTCPeerConnection', { value: undefined, configurable: false });"
            )
            self._page = self._context.new_page()
            self._page.set_default_timeout(self.timeout_ms)
            self._report_connection("connected")
            return self
        except Exception:
            self._api_token = ""
            try:
                self.close()
            except Exception:
                pass
            self._report_connection("failed")
            # Playwright exceptions can include the tokenized WebSocket URL.
            raise BrowserControllerError("connect", "Browserless session could not be established") from None

    def _guard_cloud_request(self, route) -> None:
        try:
            self._validate_cloud_request(route.request.url)
        except Exception:
            try:
                route.abort("blockedbyclient")
            except Exception:
                pass
            return
        try:
            route.continue_()
        except Exception:
            # A Playwright/provider routing error is not mislabeled as an
            # application policy denial.
            try:
                route.abort("failed")
            except Exception:
                pass

    def navigate(self, url: str) -> str:
        self._validate_cloud_request(url)
        return self.navigate_with_screenshots(url)

    def navigate_with_screenshots(
        self,
        url: str,
        on_frame: Callable[[bytes], None] | None = None,
        *,
        interval_ms: int = 500,
        max_frames: int = 6,
    ) -> str:
        """Navigate while sampling real page frames during document loading.

        Browserless Cloud's native Playwright endpoint does not expose a
        generally available video stream. This bounded same-session sampling
        keeps UI frames tied to the running page without a second browser or
        an unauthenticated screen-sharing endpoint.
        """
        if not isinstance(interval_ms, int) or not 100 <= interval_ms <= 2000:
            raise ValueError("screenshot interval is outside the supported range")
        if not isinstance(max_frames, int) or not 1 <= max_frames <= 12:
            raise ValueError("screenshot frame count is outside the supported range")
        url = self._validate_cloud_request(url)
        page = self._require_page("navigate")
        try:
            page.goto(url, wait_until="commit", timeout=self.timeout_ms)
            final_url = page.url
            self._validate_cloud_request(final_url)

            # Capture before checking readyState so the first frame reflects
            # the actual committed document. Continue at a bounded cadence
            # while resources/scripts load, then let the regular evidence pass
            # capture the final state.
            for frame_index in range(max_frames):
                if callable(on_frame):
                    try:
                        image = page.screenshot(type="png", full_page=False, timeout=self.timeout_ms)
                        if image and image.startswith(b"\x89PNG\r\n\x1a\n") and len(image) <= 512 * 1024:
                            on_frame(image)
                    except Exception:
                        # Live rendering is observability; final evidence
                        # capture below remains authoritative.
                        pass
                try:
                    ready = page.evaluate("document.readyState") == "complete"
                except Exception:
                    ready = False
                if ready or frame_index + 1 >= max_frames:
                    break
                page.wait_for_timeout(interval_ms)
            if not ready:
                page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
            return final_url
        except BrowserControllerError:
            raise
        except Exception as exc:
            # Never expose provider URLs, tokens, or page content in errors.
            raise BrowserControllerError("navigate", "Browserless navigation failed safely") from None

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._report_connection("closed")

    @staticmethod
    def _load_playwright():
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise BrowserControllerError("connect", "Playwright is not installed in the backend environment") from None
        return sync_playwright
