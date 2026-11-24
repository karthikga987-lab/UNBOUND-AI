from __future__ import annotations

import pytest

from backend.app.browser import BrowserController, BrowserControllerError
from backend.app.browser import controller as browser_module


class FakeLocator:
    def __init__(self, value: str = ""):
        self.value = value
        self.calls = []

    def click(self, **kwargs):
        self.calls.append(("click", kwargs))

    def fill(self, value, **kwargs):
        self.calls.append(("fill", value, kwargs))

    def inner_text(self, **kwargs):
        self.calls.append(("inner_text", kwargs))
        return "Visible page text"


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.locators = []
        self.default_timeout = None

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def goto(self, url, **kwargs):
        self.url = url

    def title(self):
        return "Example title"

    def _make_locator(self, method, value, **kwargs):
        locator = FakeLocator()
        locator.strategy = (method, value, kwargs)
        self.locators.append(locator)
        return locator

    def get_by_role(self, role, **kwargs):
        return self._make_locator("role", role, **kwargs)

    def get_by_label(self, value, **kwargs):
        return self._make_locator("label", value, **kwargs)

    def get_by_placeholder(self, value, **kwargs):
        return self._make_locator("placeholder", value, **kwargs)

    def get_by_text(self, value, **kwargs):
        return self._make_locator("text", value, **kwargs)

    def get_by_test_id(self, value):
        return self._make_locator("test_id", value)

    def locator(self, value):
        return self._make_locator("css", value)


class FakeResource:
    def __init__(self, page=None):
        self.page = page
        self.closed = 0

    def close(self):
        self.closed += 1

    def stop(self):
        self.closed += 1


class FakeContext(FakeResource):
    def new_page(self):
        return self.page


class FakeBrowser(FakeResource):
    def new_context(self):
        return self.context


class FakePlaywright(FakeResource):
    def __init__(self, browser):
        super().__init__()
        self.chromium = self
        self.browser = browser

    def launch(self, **kwargs):
        self.launch_options = kwargs
        return self.browser

    def start(self):
        return self


@pytest.fixture
def fake_session(monkeypatch):
    page = FakePage()
    context = FakeContext(page)
    browser = FakeBrowser()
    browser.context = context
    playwright = FakePlaywright(browser)
    monkeypatch.setattr(browser_module, "_load_playwright", lambda: lambda: playwright)
    controller = BrowserController(timeout_ms=1234)
    controller.start()
    return controller, page, context, browser, playwright


def test_start_navigation_and_read_page_state(fake_session):
    controller, page, _, _, _ = fake_session
    assert page.default_timeout == 1234
    assert controller.navigate("https://example.test/report") == "https://example.test/report"
    assert controller.current_url == "https://example.test/report"
    assert controller.title() == "Example title"
    assert page.locators == []


@pytest.mark.parametrize("url", ["file:///tmp/page", "javascript:alert(1)", "/relative", "ftp://example.test"])
def test_navigation_rejects_non_http_urls_before_calling_page(fake_session, url):
    controller, page, *_ = fake_session
    with pytest.raises(BrowserControllerError, match="Only absolute HTTP and HTTPS"):
        controller.navigate(url)
    assert page.url == "about:blank"


def test_click_and_fill_use_structured_safe_locators(fake_session):
    controller, page, *_ = fake_session
    controller.click({"type": "role", "role": "button", "name": "Search"})
    controller.fill({"type": "label", "value": "Email address"}, "user@example.test")
    assert page.locators[0].strategy == ("role", "button", {"name": "Search", "exact": False})
    assert page.locators[0].calls == [("click", {"timeout": 1234})]
    assert page.locators[1].strategy == ("label", "Email address", {"exact": False})
    assert page.locators[1].calls == [("fill", "user@example.test", {"timeout": 1234})]


def test_visible_content_reads_rendered_body_text(fake_session):
    controller, page, *_ = fake_session
    assert controller.visible_content() == "Visible page text"
    assert page.locators[0].strategy == ("css", "body", {})


def test_close_is_idempotent_and_releases_resources(fake_session):
    controller, _, context, browser, playwright = fake_session
    controller.close()
    controller.close()
    assert not controller.is_started
    assert (context.closed, browser.closed, playwright.closed) == (1, 1, 1)


def test_locator_failure_includes_operation_url_and_reason(fake_session):
    controller, page, *_ = fake_session

    locator = FakeLocator()
    def fail_click(**kwargs):
        raise TimeoutError("element was not visible")

    locator.click = fail_click
    page.get_by_role = lambda role, **kwargs: locator
    with pytest.raises(BrowserControllerError) as error:
        controller.click({"type": "role", "role": "button", "name": "Search"})
    assert error.value.operation == "click"
    assert "element was not visible" in error.value.reason
    assert "about:blank" in str(error.value)


def test_redirect_to_non_http_url_is_rejected(fake_session):
    controller, page, *_ = fake_session
    def redirect(url, **kwargs):
        page.url = "file:///unexpected"
    page.goto = redirect
    with pytest.raises(BrowserControllerError, match="Only absolute HTTP and HTTPS"):
        controller.navigate("https://example.test")
