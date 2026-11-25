from __future__ import annotations

import os
import pytest

from backend.app.browser.controller import BrowserController, BrowserControllerError


def test_controller_refuses_to_start_without_managed_loopback_proxy(monkeypatch):
    def unexpected_playwright_load():
        raise AssertionError("Playwright must not load without a configured proxy")

    monkeypatch.setattr("backend.app.browser.controller._load_playwright", unexpected_playwright_load)
    for proxy in (None, "http://127.0.0.1.evil:8888", "http://10.0.0.1:8000", "https://127.0.0.1:8000"):
        with pytest.raises(BrowserControllerError, match="managed loopback egress proxy"):
            BrowserController(proxy_server=proxy).start()


def test_playwright_uses_installed_project_browser_cache_when_localappdata_is_isolated(monkeypatch):
    from pathlib import Path
    from backend.app.browser import controller

    cache = Path(controller.__file__).resolve().parents[3] / ".playwright-browsers"
    if not cache.is_dir():
        pytest.skip("Project-local Playwright browser cache is not installed")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    controller._load_playwright()

    assert Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]) == cache


def test_controller_configures_proxy_and_context_request_guard(monkeypatch):
    captured = {}

    class Page:
        def set_default_timeout(self, timeout):
            captured["timeout"] = timeout

    class Context:
        def route(self, pattern, callback):
            captured["route"] = (pattern, callback)

        def new_page(self):
            return Page()

        def close(self):
            pass

    class Browser:
        def new_context(self, **kwargs):
            captured["context"] = kwargs
            return Context()

        def close(self):
            pass

    class Chromium:
        def launch(self, **kwargs):
            captured["launch"] = kwargs
            return Browser()

    class Playwright:
        chromium = Chromium()

        def stop(self):
            pass

    class SyncPlaywright:
        def start(self):
            return Playwright()

    monkeypatch.setattr("backend.app.browser.controller._load_playwright", lambda: lambda: SyncPlaywright())
    controller = BrowserController(
        proxy_server="http://127.0.0.1:8123",
        proxy_credentials={"username": "unbound", "password": "synthetic-secret"},
    ).start()
    try:
        assert captured["launch"]["proxy"] == {
            "server": "http://127.0.0.1:8123", "bypass": "<-loopback>",
            "username": "unbound", "password": "synthetic-secret",
        }
        assert "--disable-quic" in captured["launch"]["args"]
        assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in captured["launch"]["args"]
        assert "--disable-background-networking" in captured["launch"]["args"]
        assert captured["context"] == {"service_workers": "block"}
        pattern, handler = captured["route"]
        assert pattern == "**/*"

        class Request:
            url = "file:///C:/private.txt"

        class Route:
            request = Request()
            aborted = False

            def abort(self, *_args):
                self.aborted = True

            def continue_(self):
                raise AssertionError("Unsafe scheme continued")

        route = Route()
        handler(route)
        assert route.aborted
    finally:
        controller.close()
