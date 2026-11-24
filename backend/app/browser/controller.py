"""Synchronous Playwright controller with a small, explicit locator surface.

This module owns browser lifecycle and direct page operations. Tool registration,
workflow recording, persistence, and orchestration belong to other modules.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from backend.app.browser.downloads import BrowserDownloadResult, BrowserDownloadStore


LocatorKind = Literal["role", "label", "placeholder", "text", "test_id", "css"]


@dataclass(frozen=True)
class LocatorTarget:
    """A constrained locator description; it never accepts executable code."""

    kind: LocatorKind
    value: str
    name: str | None = None
    exact: bool = False


class BrowserControllerError(RuntimeError):
    """An operation failure with enough context for a task timeline or log."""

    def __init__(self, operation: str, message: str, *, url: str | None = None) -> None:
        self.operation = operation
        self.url = url
        self.reason = message
        context = f" at {url}" if url else ""
        super().__init__(f"Browser {operation} failed{context}: {message}")


def _load_playwright():
    """Load Playwright lazily so importing backend modules needs no browser install."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserControllerError(
            "start", "Playwright is not installed; install backend requirements"
        ) from exc
    return sync_playwright


def _validate_http_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise BrowserControllerError("navigate", "URL must be a non-empty string")
    value = url.strip()
    try:
        parts = urlsplit(value)
        # Accessing .port also rejects malformed port strings.
        _ = parts.port
    except ValueError as exc:
        raise BrowserControllerError("navigate", f"Invalid URL: {exc}", url=value) from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise BrowserControllerError("navigate", "Only absolute HTTP and HTTPS URLs are allowed", url=value)
    if parts.username is not None or parts.password is not None:
        raise BrowserControllerError("navigate", "URLs containing credentials are not allowed", url=value)
    return value


def _parse_target(target: LocatorTarget | Mapping[str, Any]) -> LocatorTarget:
    if isinstance(target, LocatorTarget):
        parsed = target
    elif isinstance(target, Mapping):
        # WorkflowAction.target uses structured dictionaries. Accept either
        # {"type":"role", "role":"button", "name":"Search"} or
        # {"type":"label", "value":"Email"} forms.
        kind = target.get("type", target.get("kind"))
        if kind == "role":
            value = target.get("role")
            name = target.get("name")
        else:
            value = target.get("value", target.get("selector"))
            name = target.get("name")
        if not isinstance(kind, str) or not isinstance(value, str) or not value.strip():
            raise ValueError("target must specify a supported locator type and non-empty value")
        if name is not None and not isinstance(name, str):
            raise ValueError("target name must be a string")
        exact = target.get("exact", False)
        if not isinstance(exact, bool):
            raise ValueError("target exact must be a boolean")
        parsed = LocatorTarget(kind=kind, value=value.strip(), name=name, exact=exact)  # type: ignore[arg-type]
    else:
        raise ValueError("target must be a LocatorTarget or structured mapping")

    if parsed.kind not in {"role", "label", "placeholder", "text", "test_id", "css"}:
        raise ValueError(f"unsupported locator type: {parsed.kind}")
    if not isinstance(parsed.value, str) or not parsed.value.strip():
        raise ValueError("locator value must be a non-empty string")
    if parsed.name is not None and not isinstance(parsed.name, str):
        raise ValueError("locator name must be a string")
    if not isinstance(parsed.exact, bool):
        raise ValueError("locator exact must be a boolean")
    return parsed


class BrowserController:
    """Own one Playwright browser context and page.

    Create one controller per independently managed browser session. ``close``
    is idempotent and attempts to release every resource even if one close fails.
    """

    def __init__(
        self,
        *,
        proxy_server: str | None = None,
        headless: bool = True,
        timeout_ms: int = 10_000,
        download_store: BrowserDownloadStore | None = None,
    ) -> None:
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
            raise ValueError("timeout_ms must be a positive integer")
        self.headless = headless
        self.proxy_server = proxy_server
        self.timeout_ms = timeout_ms
        self.download_store = download_store or BrowserDownloadStore()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    @property
    def is_started(self) -> bool:
        return self._page is not None

    def start(self) -> "BrowserController":
        if self.is_started:
            return self
        proxy = None
        try:
            proxy = urlsplit(self.proxy_server) if isinstance(self.proxy_server, str) else None
            proxy_ip = ipaddress.ip_address(proxy.hostname) if proxy and proxy.hostname else None
            proxy_port = proxy.port if proxy else None
        except ValueError:
            proxy_ip, proxy_port = None, None
        if (
            proxy is None
            or proxy.scheme != "http"
            or proxy.username is not None
            or proxy.password is not None
            or proxy.path not in {"", "/"}
            or proxy.query
            or proxy.fragment
            or not isinstance(proxy_ip, ipaddress.IPv4Address)
            or not proxy_ip.is_loopback
            or proxy_port is None
        ):
            raise BrowserControllerError(
                "start", "A managed loopback egress proxy is required for browser sessions"
            )
        sync_playwright = _load_playwright()
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=self.headless,
                proxy={"server": self.proxy_server, "bypass": "<-loopback>"},
                args=[
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
            self._context = self._browser.new_context(service_workers="block")
            # Context-level interception applies to navigations, frames, popups,
            # subresources, and downloads. The proxy independently pins the
            # actual connection destination; this handler blocks non-web schemes.
            self._context.route("**/*", self._guard_request)
            self._page = self._context.new_page()
            self._page.set_default_timeout(self.timeout_ms)
            return self
        except Exception as exc:
            try:
                self.close()
            except BrowserControllerError as close_error:
                raise BrowserControllerError(
                    "start", f"{exc}; cleanup also failed: {close_error}"
                ) from exc
            if isinstance(exc, BrowserControllerError):
                raise
            raise BrowserControllerError("start", str(exc)) from exc

    @staticmethod
    def _guard_request(route) -> None:
        """Reject non-HTTP(S) URLs before they can access local browser resources."""
        try:
            request_url = route.request.url
            parts = urlsplit(request_url)
            if (
                parts.scheme.lower() not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
            ):
                route.abort("blockedbyclient")
                return
            route.continue_()
        except Exception:
            # Any interception failure denies the request; never fall back to
            # Chromium's direct networking path.
            try:
                route.abort("blockedbyclient")
            except Exception:
                return

    def close(self) -> None:
        """Close page resources in reverse order; safe to call more than once."""
        errors: list[str] = []
        for attribute in ("_context", "_browser", "_playwright"):
            resource = getattr(self, attribute)
            setattr(self, attribute, None)
            if resource is None:
                continue
            try:
                resource.stop() if attribute == "_playwright" else resource.close()
            except Exception as exc:
                errors.append(str(exc))
        self._page = None
        if errors:
            raise BrowserControllerError("close", "; ".join(errors))

    def __enter__(self) -> "BrowserController":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _require_page(self, operation: str):
        if self._page is None:
            raise BrowserControllerError(operation, "browser session is not started")
        return self._page

    def navigate(self, url: str) -> str:
        url = _validate_http_url(url)
        page = self._require_page("navigate")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            final_url = page.url
            _validate_http_url(final_url)
            return final_url
        except BrowserControllerError:
            raise
        except Exception as exc:
            raise BrowserControllerError("navigate", str(exc), url=url) from exc

    @property
    def current_url(self) -> str:
        page = self._require_page("read current URL")
        try:
            return page.url
        except Exception as exc:
            raise BrowserControllerError("read current URL", str(exc)) from exc

    def title(self) -> str:
        page = self._require_page("read title")
        try:
            return page.title()
        except Exception as exc:
            raise BrowserControllerError("read title", str(exc), url=page.url) from exc

    @staticmethod
    def _locator(page, target: LocatorTarget | Mapping[str, Any]):
        parsed = _parse_target(target)
        if parsed.kind == "role":
            options = {"name": parsed.name, "exact": parsed.exact}
            return page.get_by_role(parsed.value, **options)
        if parsed.kind == "label":
            return page.get_by_label(parsed.value, exact=parsed.exact)
        if parsed.kind == "placeholder":
            return page.get_by_placeholder(parsed.value, exact=parsed.exact)
        if parsed.kind == "text":
            return page.get_by_text(parsed.value, exact=parsed.exact)
        if parsed.kind == "test_id":
            return page.get_by_test_id(parsed.value)
        return page.locator(parsed.value)

    def click(self, target: LocatorTarget | Mapping[str, Any]) -> None:
        page = self._require_page("click")
        try:
            self._locator(page, target).click(timeout=self.timeout_ms)
        except Exception as exc:
            raise BrowserControllerError("click", str(exc), url=page.url) from exc

    def fill(self, target: LocatorTarget | Mapping[str, Any], value: str) -> None:
        if not isinstance(value, str):
            raise BrowserControllerError("fill", "input value must be a string")
        page = self._require_page("fill")
        try:
            self._locator(page, target).fill(value, timeout=self.timeout_ms)
        except Exception as exc:
            raise BrowserControllerError("fill", str(exc), url=page.url) from exc

    def download(self, target: LocatorTarget | Mapping[str, Any]) -> BrowserDownloadResult:
        """Click a target while waiting for and safely persisting its download.

        The operation uses Playwright's download event and returns only safe
        generated-file metadata; raw browser diagnostics and URLs are omitted.
        Approval classification remains the responsibility of the existing
        registered-tool boundary before this controller method is called.
        """
        page = self._require_page("download")
        try:
            with page.expect_download(timeout=self.timeout_ms) as download_info:
                self._locator(page, target).click(timeout=self.timeout_ms)
            download = download_info.value
        except Exception:
            return BrowserDownloadResult.failed("No completed browser download was captured")
        return self.download_store.save(download)

    def visible_content(self) -> str:
        """Return rendered text from the page body, excluding hidden DOM text."""
        page = self._require_page("read visible content")
        try:
            return page.locator("body").inner_text(timeout=self.timeout_ms)
        except Exception as exc:
            raise BrowserControllerError("read visible content", str(exc), url=page.url) from exc
