"""Unprivileged renderer: offline fixtures by default, opt-in broker-mediated GETs."""
from __future__ import annotations

import base64
import time
from threading import RLock
from uuid import uuid4

from backend.app.egress_proxy import _LOCAL_DEMO_PAGES, LOCAL_DEMO_PRODUCT_URL
from backend.app.browser.worker_protocol import (
    BrowserOperation, Click, CloseBrowser, ExtractVisible, Navigate, Screenshot,
    Scroll, TypeText, WaitNavigation, WorkerProtocolError, WorkerResult,
)
from backend.app.browser.worker_ipc import RenderRequest


def serve_fixture(route):
    for path, (title, html) in _LOCAL_DEMO_PAGES.items():
        if route.request.url == "http://unbound-demo.invalid" + path and route.request.method == "GET":
            route.fulfill(status=200, content_type="text/html", body=f"<title>{title}</title>{html}")
            return
    route.abort()


class FixtureRenderer:
    def __init__(self):
        self.max_sessions = 2
        self._sessions = {}
        self._last_used = {}
        self._lock = RLock()
        self._playwright_launch_ready = None

    def browser_ready(self):
        if any(browser.is_connected() for _, browser, _ in self._sessions.values()):
            return True
        if self._playwright_launch_ready is None:
            probe = None
            ready = False
            try:
                # Readiness means Chromium can launch with the configured
                # sandbox and then cleanly exit. No page is navigated here.
                # It must not depend on an already-open session, or the first
                # approved task can never start.
                probe = self._create_session((uuid4(), uuid4()))
                playwright, browser, _page = probe
                ready = bool(browser.is_connected())
            except Exception:
                ready = False
            finally:
                if probe is not None:
                    playwright, browser, _page = probe
                    try:
                        browser.close()
                    except Exception:
                        ready = False
                    try:
                        playwright.stop()
                    except Exception:
                        ready = False
            self._playwright_launch_ready = ready
        return self._playwright_launch_ready

    def expire_idle(self):
        for key, last in tuple(self._last_used.items()):
            if time.monotonic() - last >= 60:
                self._close(key)

    def execute(self, request: RenderRequest) -> WorkerResult:
        self.expire_idle()

        key = (request.task_id, request.session_id)
        try:
            with self._lock:
                if isinstance(request.operation, Navigate) and not self._navigation_allowed(request.operation.url):
                    return WorkerResult(success=False, failure_code="invalid_destination")
                if isinstance(request.operation, CloseBrowser):
                    self._close(key)
                    return WorkerResult(success=True)
                if key not in self._sessions:
                    if not isinstance(request.operation, Navigate):
                        return WorkerResult(success=False, failure_code="invalid_session")
                    if len(self._sessions) >= self.max_sessions:
                        return WorkerResult(success=False, failure_code="browser_unavailable")
                    self._sessions[key] = self._create_session(key)
                _playwright, browser, page = self._sessions[key]
            self._last_used[key] = time.monotonic()
            return self._perform(page, request.operation)
        except WorkerProtocolError:
            self._close(key)
            return WorkerResult(success=False, failure_code="invalid_destination")
        except TimeoutError:
            self._close(key)
            return WorkerResult(success=False, failure_code="timeout")
        except Exception as exc:
            # Exception text can contain URLs or page content; never return/log it.
            try:
                self._close(key)
            except Exception:
                pass
            category = "proxy_unavailable" if "proxy" in type(exc).__name__.casefold() else "worker_operation_failed"
            return WorkerResult(success=False, failure_code=category)

    def _navigation_allowed(self, url):
        return url == LOCAL_DEMO_PRODUCT_URL

    def _route_handler(self, _key):
        return serve_fixture

    def _create_session(self, key):
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        browser = None
        try:
            browser = playwright.chromium.launch(
                headless=True,
                chromium_sandbox=True,
                proxy={"server": "http://127.0.0.1:9", "bypass": "<-loopback>"},
                args=["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-background-networking"],
                timeout=15000,
            )
            context = browser.new_context(accept_downloads=False, service_workers="block", java_script_enabled=False)
            context.route("**/*", self._route_handler(key))
            context.route_web_socket("**/*", lambda websocket: websocket.close())
            page = context.new_page()
            page.set_default_timeout(10000)
            return playwright, browser, page
        except Exception:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            try:
                playwright.stop()
            except Exception:
                pass
            raise

    @staticmethod
    def _locator(page, target):
        if target.type == "role":
            return page.get_by_role(target.value, name=target.name, exact=target.exact)
        if target.type == "label":
            return page.get_by_label(target.value, exact=target.exact)
        if target.type == "placeholder":
            return page.get_by_placeholder(target.value, exact=target.exact)
        if target.type == "text":
            return page.get_by_text(target.value, exact=target.exact)
        if target.type == "test_id":
            return page.get_by_test_id(target.value)
        return page.locator(target.value)

    def _perform(self, page, operation: BrowserOperation) -> WorkerResult:
        if isinstance(operation, Navigate):
            if not self._navigation_allowed(operation.url):
                raise WorkerProtocolError("invalid_destination", "invalid navigation target")
            page.goto(operation.url, wait_until="domcontentloaded", timeout=15000)
        elif isinstance(operation, Click):
            self._locator(page, operation.target).click()
        elif isinstance(operation, TypeText):
            self._locator(page, operation.target).fill(operation.text)
        elif isinstance(operation, Scroll):
            page.mouse.wheel(operation.delta_x, operation.delta_y)
        elif isinstance(operation, Screenshot):
            raw = page.screenshot(type="png", full_page=False, timeout=10000)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("screenshot limit exceeded")
            return WorkerResult(success=True, current_url=page.url[:4096], title=page.title()[:1024], screenshot_base64=base64.b64encode(raw).decode("ascii"))
        elif isinstance(operation, ExtractVisible):
            text = page.locator("body").inner_text(timeout=10000)
            bounded = text[:operation.max_chars]
            return WorkerResult(success=True, current_url=page.url[:4096], title=page.title()[:1024], visible_text=bounded, visible_text_truncated=len(text) > len(bounded))
        elif isinstance(operation, WaitNavigation):
            page.wait_for_load_state(operation.state, timeout=operation.timeout_ms)
        elif not isinstance(operation, CloseBrowser):
            return WorkerResult(success=False, failure_code="worker_operation_failed")
        return WorkerResult(success=True, current_url=page.url[:4096], title=page.title()[:1024])

    def _close(self, key) -> None:
        self._last_used.pop(key, None)
        session = self._sessions.pop(key, None)
        if session is not None:
            playwright, browser, _page = session
            try:
                browser.close()
            finally:
                playwright.stop()

    def close_all(self) -> None:
        with self._lock:
            for key in list(self._sessions):
                try:
                    self._close(key)
                except Exception:
                    continue


class BrokerFetchRenderer(FixtureRenderer):
    """No browser network sockets or gateway secrets; every GET goes over private IPC."""

    def __init__(self, fetch_client):
        super().__init__()
        self.fetch_client = fetch_client
        self._active_requests = {}

    def _navigation_allowed(self, url):
        from backend.app.browser.worker_fetch import validate_fetch_url
        try:
            validate_fetch_url(url)
            return True
        except ValueError:
            return False

    def execute(self, request):
        key = (request.task_id, request.session_id)
        if not isinstance(request.operation, CloseBrowser) and request.fetch_token is None:
            return WorkerResult(success=False, failure_code="proxy_unavailable")
        self._active_requests[key] = request
        try:
            result = super().execute(request)
            if result.success and result.current_url is not None and not self._navigation_allowed(result.current_url):
                self._close(key)
                return WorkerResult(success=False, failure_code="invalid_destination")
            return result
        finally:
            self._active_requests.pop(key, None)

    def _route_handler(self, key):
        from backend.app.browser.worker_fetch import FetchRequest, validate_fetch_url

        def forward(route):
            try:
                operation = self._active_requests.get(key)
                if operation is None or operation.fetch_token is None or route.request.method != "GET":
                    raise PermissionError("request not authorized")
                validate_fetch_url(route.request.url)
                response = self.fetch_client.fetch(FetchRequest(
                    task_id=operation.task_id, session_id=operation.session_id,
                    token=operation.fetch_token, url=route.request.url,
                ))
                headers = {
                    "Content-Security-Policy": "script-src 'none'; connect-src 'none'; object-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'",
                    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                }
                if response.location is not None:
                    headers["Location"] = response.location
                route.fulfill(status=response.status, content_type=response.content_type,
                              body=base64.b64decode(response.body_base64, validate=True), headers=headers)
            except Exception:
                # Never permit Playwright's direct request path as an error fallback.
                route.abort("blockedbyclient")

        return forward
