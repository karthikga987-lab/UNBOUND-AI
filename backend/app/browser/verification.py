"""Deterministic, read-only checks of observable browser state.

Expectations are structured Python values. Page text and other webpage content
are compared as data only; this module never interprets them as instructions or
executes page-provided code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from backend.app.browser.controller import (
    BrowserController,
    BrowserControllerError,
    LocatorTarget,
    _parse_target,
    _validate_http_url,
)
from backend.app.browser.downloads import BrowserDownloadResult, BrowserDownloadStore


ElementState = Literal[
    "visible", "hidden", "enabled", "disabled", "checked", "unchecked",
    "present", "absent",
]


@dataclass(frozen=True)
class BrowserExpectation:
    """Observable conditions to compare against the current page.

    Set one or more of ``expected_url``, ``url_contains``, ``title_contains``,
    ``visible_text_contains``, and ``target``. A target defaults to a visible
    element check unless ``element_state`` is supplied.
    """

    expected_url: str | None = None
    url_contains: str | None = None
    title_contains: str | None = None
    visible_text_contains: str | None = None
    target: LocatorTarget | Mapping[str, Any] | None = None
    element_state: ElementState | None = None
    case_sensitive: bool = True


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    success: bool
    message: str


@dataclass
class BrowserVerificationResult:
    success: bool
    checks: list[VerificationCheck] = field(default_factory=list)
    # Evidence includes measured values/booleans but omits raw page text and
    # query strings so it cannot accidentally echo webpage secrets.
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class BrowserVerifier:
    """Evaluate a structured expectation using Playwright's read-only APIs."""

    def __init__(
        self,
        controller: BrowserController,
        *,
        download_directory: str | None = None,
    ) -> None:
        self.controller = controller
        self.download_store = BrowserDownloadStore(download_directory)

    def verify_download(self, download: BrowserDownloadResult) -> BrowserVerificationResult:
        """Verify that a successful download result refers to a complete stored file."""
        if not isinstance(download, BrowserDownloadResult) or not download.success:
            return self._download_failure("A successful browser download result is required")
        try:
            path = self.download_store.resolve_result_path(download)
            if not path.is_file():
                return self._download_failure("Downloaded file is missing")
            size = path.stat().st_size
            if size <= 0:
                return self._download_failure("Downloaded file is empty")
            if not isinstance(download.size_bytes, int) or download.size_bytes != size:
                return self._download_failure("Downloaded file size did not match the saved result")
        except (OSError, ValueError, TypeError):
            return self._download_failure("Download path is invalid or outside the download directory")

        return BrowserVerificationResult(
            success=True,
            checks=[VerificationCheck(
                name="download", success=True, message="Downloaded file exists and has the recorded size"
            )],
            evidence={"download": {"exists": True, "size_bytes": size}},
        )

    def verify(self, expectation: BrowserExpectation) -> BrowserVerificationResult:
        if not isinstance(expectation, BrowserExpectation):
            return self._invalid("A structured BrowserExpectation is required")
        requested = any((
            expectation.expected_url is not None,
            expectation.url_contains is not None,
            expectation.title_contains is not None,
            expectation.visible_text_contains is not None,
            expectation.target is not None,
        ))
        if not requested:
            return self._invalid("At least one browser condition must be specified")
        if expectation.expected_url is not None and expectation.url_contains is not None:
            return self._invalid("Specify expected_url or url_contains, not both")
        for field_name in ("url_contains", "title_contains", "visible_text_contains"):
            value = getattr(expectation, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                return self._invalid(f"{field_name} must be a non-empty string")
        if expectation.expected_url is not None:
            try:
                _validate_http_url(expectation.expected_url)
            except BrowserControllerError:
                return self._invalid("expected_url must be an absolute HTTP or HTTPS URL")
        if not isinstance(expectation.case_sensitive, bool):
            return self._invalid("case_sensitive must be a boolean")
        if expectation.target is not None:
            try:
                _parse_target(expectation.target)
            except (TypeError, ValueError):
                return self._invalid("target must use a supported structured locator")
        elif expectation.element_state is not None:
            return self._invalid("element_state requires a target")
        allowed_states = {
            "visible", "hidden", "enabled", "disabled", "checked", "unchecked", "present", "absent"
        }
        if expectation.element_state is not None and (
            not isinstance(expectation.element_state, str)
            or expectation.element_state not in allowed_states
        ):
            return self._invalid("element_state is not supported")
        if not self.controller.is_started:
            return self._invalid("Browser session is not started")

        result = BrowserVerificationResult(success=True)
        try:
            page = self.controller._require_page("verify browser state")
        except Exception:
            return self._invalid("Browser page is unavailable for verification")

        if expectation.expected_url is not None or expectation.url_contains is not None:
            self._verify_url(page, expectation, result)
        if expectation.title_contains is not None:
            self._verify_title(page, expectation, result)
        if expectation.visible_text_contains is not None:
            self._verify_text(page, expectation, result)
        if expectation.target is not None:
            self._verify_element(page, expectation, result)

        result.success = bool(result.checks) and all(check.success for check in result.checks)
        if not result.success:
            result.error = next(
                (check.message for check in result.checks if not check.success),
                "Browser state did not match the expected conditions",
            )
        return result

    def _verify_url(self, page, expectation: BrowserExpectation, result: BrowserVerificationResult) -> None:
        try:
            observed = page.url
            if expectation.expected_url is not None:
                matched = observed == expectation.expected_url
            else:
                matched = self._contains(observed, expectation.url_contains or "", expectation.case_sensitive)
            origin = self._safe_origin(observed)
            result.evidence["url"] = {"matched": matched, "observed_origin": origin}
            result.checks.append(VerificationCheck(
                name="url", success=matched,
                message="Current URL matched" if matched else "Current URL did not match the expected condition",
            ))
        except Exception:
            self._unavailable(result, "url", "Could not read the current URL")

    def _verify_title(self, page, expectation: BrowserExpectation, result: BrowserVerificationResult) -> None:
        try:
            title = page.title()
            matched = self._contains(title, expectation.title_contains or "", expectation.case_sensitive)
            result.evidence["title"] = {"matched": matched, "length": len(title)}
            result.checks.append(VerificationCheck(
                name="title", success=matched,
                message="Page title matched" if matched else "Page title did not contain the expected text",
            ))
        except Exception:
            self._unavailable(result, "title", "Could not read the page title")

    def _verify_text(self, page, expectation: BrowserExpectation, result: BrowserVerificationResult) -> None:
        try:
            visible_text = page.locator("body").inner_text(timeout=self.controller.timeout_ms)
            matched = self._contains(
                visible_text, expectation.visible_text_contains or "", expectation.case_sensitive
            )
            result.evidence["visible_text"] = {"matched": matched, "length": len(visible_text)}
            result.checks.append(VerificationCheck(
                name="visible_text", success=matched,
                message="Visible page text matched" if matched else "Visible page text did not contain the expected text",
            ))
        except Exception:
            self._unavailable(result, "visible_text", "Could not read visible page text")

    def _verify_element(self, page, expectation: BrowserExpectation, result: BrowserVerificationResult) -> None:
        state = expectation.element_state or "visible"
        try:
            locator = self.controller._locator(page, expectation.target)
            count = locator.count()
            if state in {"present", "absent"}:
                observed = "present" if count > 0 else "absent"
            elif count == 0:
                observed = "absent"
            elif state in {"visible", "hidden"}:
                visible = locator.is_visible()
                observed = "visible" if visible else "hidden"
            elif state in {"enabled", "disabled"}:
                enabled = locator.is_enabled()
                observed = "enabled" if enabled else "disabled"
            else:
                checked = locator.is_checked()
                observed = "checked" if checked else "unchecked"
            matched = observed == state or (state == "hidden" and observed == "absent") or (state == "absent" and observed == "absent")
            result.evidence["element"] = {"expected_state": state, "observed_state": observed, "matched": matched}
            result.checks.append(VerificationCheck(
                name="element_state", success=matched,
                message="Element state matched" if matched else "Element state did not match the expected condition",
            ))
        except Exception:
            self._unavailable(result, "element_state", "Could not read the requested element state")

    @staticmethod
    def _contains(observed: str, expected: str, case_sensitive: bool) -> bool:
        if case_sensitive:
            return expected in observed
        return expected.casefold() in observed.casefold()

    @staticmethod
    def _safe_origin(url: str) -> str | None:
        try:
            parts = urlsplit(url)
            if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
                return None
            host = parts.hostname
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = f":{parts.port}" if parts.port is not None else ""
            return f"{parts.scheme.lower()}://{host}{port}"
        except ValueError:
            return None

    @staticmethod
    def _unavailable(result: BrowserVerificationResult, name: str, message: str) -> None:
        result.evidence[name] = {"available": False}
        result.checks.append(VerificationCheck(name=name, success=False, message=message))

    @staticmethod
    def _invalid(message: str) -> BrowserVerificationResult:
        return BrowserVerificationResult(
            success=False,
            error=message,
            checks=[VerificationCheck(name="expectation", success=False, message=message)],
        )

    @staticmethod
    def _download_failure(message: str) -> BrowserVerificationResult:
        return BrowserVerificationResult(
            success=False,
            error=message,
            checks=[VerificationCheck(name="download", success=False, message=message)],
            evidence={"download": {"verified": False}},
        )
