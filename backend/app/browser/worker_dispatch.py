"""Fail-closed TaskExecutor adapter for the authenticated Ubuntu browser worker.

The worker's Ed25519 response authenticates the sender and binds the response
to an authorized request. It does not prove that browser observations are true;
an independent host verifier is therefore optional and defaults to false.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from html.parser import HTMLParser
import logging
import re
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from backend.app.browser.worker_protocol import (
    MAX_SCREENSHOT_BYTES,
    MAX_REQUEST_TTL_SECONDS,
    BrowserWorkerClient,
    Click,
    CloseBrowser,
    ExtractVisible,
    Locator,
    Navigate,
    Screenshot,
    WorkerProtocolError,
    WorkerResult,
    make_worker_request,
)
from backend.app.orchestrator.tool_registry import ToolExecutionCancelled, ToolExecutionOutcomeUnknown
from backend.app.schemas.contracts import (
    ApprovalRequest,
    ApprovalStatus,
    EvidenceItem,
    EvidenceSourceType,
    ToolCall,
    ToolResult,
)


logger = logging.getLogger(__name__)
DEFAULT_ALLOWED_HOSTS = frozenset({"example.com", "www.python.org", "docs.python.org"})
_HOST_PATTERN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}\Z")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_VISIBLE_CHARS = 12000
_EVIDENCE_EXCERPT_CHARS = 4000

IndependentVerifier = Callable[[str, str, ToolResult], bool]


@dataclass
class _TaskBrowserSession:
    session_id: UUID
    last_approval: ApprovalRequest | None = None
    last_tool_name: str | None = None
    last_step_id: str | None = None


class _OriginTextParser(HTMLParser):
    """Collect HTML title and non-script text for host-side cross-checks."""

    _IGNORED = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._ignored = 0
        self._in_title = False

    def handle_starttag(self, tag, _attrs):
        tag = tag.casefold()
        if tag in self._IGNORED:
            self._ignored += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in self._IGNORED and self._ignored:
            self._ignored -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif self._ignored == 0:
            self.text_parts.append(data)


class GatewayObservationVerifier:
    """Independently cross-check worker text against a fresh pinned origin GET.

    This verifies that the page observation is grounded in the allowlisted
    origin response. It does not prove the guest process was uncompromised or
    attest to pixels; those remain separate OS-level checks.
    """

    def __init__(self, gateway):
        if not callable(getattr(gateway, "fetch", None)):
            raise TypeError("a controlled host fetch gateway is required")
        self.gateway = gateway

    def __call__(self, tool_name: str, _expected: str, result: ToolResult) -> bool:
        if tool_name not in {"browser.navigate", "browser.click"} or not result.success:
            return False
        if not isinstance(result.data, dict):
            return False
        observation = result.data.get("page_observation")
        if not isinstance(observation, dict):
            return False
        url, title, visible = observation.get("url"), observation.get("title"), observation.get("visible_text")
        if not isinstance(url, str) or not isinstance(title, str) or not title.strip():
            return False
        if not isinstance(visible, str) or not visible.strip() or len(visible) > _MAX_VISIBLE_CHARS:
            return False
        try:
            safe_url = WorkerDispatcher._validated_url_for_hosts(url, DEFAULT_ALLOWED_HOSTS)
            fetched = self.gateway.fetch(safe_url)
            if fetched.get("url") != safe_url or fetched.get("status") != 200:
                return False
            if fetched.get("content_type") not in {"text/html", "application/xhtml+xml"}:
                return False
            body = base64.b64decode(fetched.get("body_base64", ""), validate=True)
            parser = _OriginTextParser()
            parser.feed(body.decode("utf-8", errors="strict"))
            parser.close()
            remote_title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()
            remote_text = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()
            observed_title = re.sub(r"\s+", " ", title).strip()
            observed_text = re.sub(r"\s+", " ", visible).strip()
            if remote_title.casefold() != observed_title.casefold() or not remote_text:
                return False
            # Require each meaningful observed token to exist in the independently
            # fetched HTML text. Short tokens are included to catch fabricated
            # numbers/product facts as well as prose.
            observed_tokens = re.findall(r"[\w]+", observed_text.casefold())
            source_tokens = set(re.findall(r"[\w]+", remote_text.casefold()))
            return bool(observed_tokens) and all(token in source_tokens for token in observed_tokens)
        except Exception:
            logger.info("Remote browser origin cross-check failed: tool_name=%s", tool_name)
            return False


class WorkerDispatcher:
    """Execute approved navigation/click operations through BrowserWorkerClient.

    ``enabled`` is intentionally opt-in. Supplying this dispatcher to
    TaskExecutor selects the remote path even when it is disabled, preventing
    accidental fallback to local Chromium.
    """

    def __init__(
        self,
        client: BrowserWorkerClient | None,
        *,
        verifier: IndependentVerifier | None = None,
        allowed_hosts: Iterable[str] = DEFAULT_ALLOWED_HOSTS,
        enabled: bool | None = None,
    ) -> None:
        hosts = frozenset(allowed_hosts)
        if not hosts or any(
            not isinstance(host, str)
            or host != host.casefold()
            or not host.isascii()
            or not _HOST_PATTERN.fullmatch(host)
            or host.startswith("*.")
            for host in hosts
        ):
            raise ValueError("worker allowlist must contain exact ASCII DNS hostnames")
        if verifier is not None and not callable(verifier):
            raise TypeError("independent worker verifier must be callable")
        self.client = client
        self.verifier = verifier
        self.allowed_hosts = hosts
        self.enabled = (
            bool(getattr(client, "enabled", False)) if enabled is None else bool(enabled)
        )
        self._sessions: dict[UUID, _TaskBrowserSession] = {}

    def execute(
        self,
        call: ToolCall,
        approval: ApprovalRequest | None,
        *,
        before_execute: Callable[[], bool] | None = None,
    ) -> ToolResult:
        """Run one approved browser.navigate or browser.click action.

        The cancellation/claim callback is invoked exactly once immediately
        before the first request that can affect the browser.
        """
        if not self.enabled or self.client is None or not getattr(self.client, "enabled", False):
            return self._failure("worker_disabled", "Remote browser worker is disabled")
        if call.tool_name not in {"browser.navigate", "browser.click"}:
            return self._failure("unsupported_operation", "This browser operation is not supported by the remote worker")
        if not self._approval_matches(approval, call):
            return self._failure("approval_required", "An exact approved browser action is required")

        try:
            operation = self._operation(call)
            if isinstance(operation, Navigate):
                self._validated_url(operation.url)
        except (ValueError, TypeError, WorkerProtocolError):
            return self._failure("invalid_destination", "Remote browser destination or action is invalid")

        if before_execute is not None and not before_execute():
            raise ToolExecutionCancelled("Task was cancelled before remote browser execution")

        state = self._get_or_create_session(call.task_id)
        state.last_approval = approval
        state.last_tool_name = call.tool_name
        state.last_step_id = call.step_id
        action = self._make_request(call, approval, state.session_id, operation)
        try:
            action_receipt = self._observe(action)
        except ToolExecutionOutcomeUnknown:
            raise
        except WorkerProtocolError as exc:
            # Request validation errors are safe to report only if the request
            # was rejected before transport. A response/authentication failure
            # after transport leaves the browser outcome uncertain.
            if exc.code in {"worker_disabled", "wrong_worker", "request_expired", "request_too_large", "invalid_request"}:
                return self._failure(exc.code, "Remote browser request was rejected safely")
            raise ToolExecutionOutcomeUnknown(
                f"Remote browser outcome is unknown ({exc.code})"
            ) from None

        action_result = action_receipt.response.result
        if not action_result.success:
            return self._failure(
                action_result.failure_code or "worker_operation_failed",
                "Remote browser worker reported that the action failed",
            )
        if not action_result.current_url:
            raise ToolExecutionOutcomeUnknown("Remote browser returned no verified page source")
        try:
            action_source = self._validated_url(action_result.current_url)
        except ValueError:
            raise ToolExecutionOutcomeUnknown("Remote browser returned a disallowed page source") from None

        extract = self._make_request(
            call, approval, state.session_id,
            ExtractVisible(operation="extract_visible", max_chars=_MAX_VISIBLE_CHARS),
        )
        screenshot = self._make_request(
            call, approval, state.session_id, Screenshot(operation="screenshot"),
        )
        try:
            extract_receipt = self._observe(extract)
            screenshot_receipt = self._observe(screenshot)
        except ToolExecutionOutcomeUnknown:
            raise
        except Exception as exc:
            # The initial browser action already ran; do not turn a failed
            # observation into a retryable known failure.
            category = exc.code if isinstance(exc, WorkerProtocolError) else type(exc).__name__
            raise ToolExecutionOutcomeUnknown(
                f"Remote browser observation is incomplete ({category})"
            ) from None

        observed = extract_receipt.response.result
        image_result = screenshot_receipt.response.result
        if not observed.success or not image_result.success:
            raise ToolExecutionOutcomeUnknown("Remote browser observation is incomplete")
        if not observed.current_url or not image_result.current_url:
            raise ToolExecutionOutcomeUnknown("Remote browser observation has no source URL")
        try:
            extract_source = self._validated_url(observed.current_url)
            screenshot_source = self._validated_url(image_result.current_url)
        except ValueError:
            raise ToolExecutionOutcomeUnknown("Remote browser observation has a disallowed source") from None
        if extract_source != action_source or screenshot_source != action_source:
            raise ToolExecutionOutcomeUnknown("Remote browser observations do not match the executed page")
        if not isinstance(observed.visible_text, str) or len(observed.visible_text) > _MAX_VISIBLE_CHARS:
            raise ToolExecutionOutcomeUnknown("Remote browser returned an invalid page observation")
        if not isinstance(image_result.screenshot_base64, str):
            raise ToolExecutionOutcomeUnknown("Remote browser returned no screenshot")
        try:
            png = base64.b64decode(image_result.screenshot_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ToolExecutionOutcomeUnknown("Remote browser returned an invalid screenshot") from None
        if not png.startswith(_PNG_SIGNATURE) or not 0 < len(png) <= MAX_SCREENSHOT_BYTES:
            raise ToolExecutionOutcomeUnknown("Remote browser returned an invalid screenshot")

        screenshot_sha256 = hashlib.sha256(png).hexdigest()
        source_url = self._evidence_url(action_source)
        title = (observed.title or action_result.title or "")[:1024]
        page_observation = {
            "url": source_url,
            "title": title,
            "visible_text": observed.visible_text,
            "visible_text_truncated": observed.visible_text_truncated,
            "content_trust": "untrusted_webpage_data",
            "screenshot_format": "image/png",
            "screenshot_sha256": screenshot_sha256,
            "screenshot_base64": image_result.screenshot_base64,
            "worker_id": getattr(self.client, "worker_id", "unknown"),
            "worker_receipts": [
                self._receipt_metadata(item)
                for item in (action_receipt, extract_receipt, screenshot_receipt)
            ],
        }
        excerpt = (title + ("\n" if title else "") + observed.visible_text)[:_EVIDENCE_EXCERPT_CHARS]
        evidence = [
            EvidenceItem(
                task_id=call.task_id,
                source_type=EvidenceSourceType.BROWSER,
                source_name="Authenticated remote browser observation",
                source_url=source_url,
                excerpt=excerpt,
                supports_claim="The authenticated worker reported this page title and visible text; independent verification is pending.",
            ),
            EvidenceItem(
                task_id=call.task_id,
                source_type=EvidenceSourceType.BROWSER,
                source_name="Remote browser PNG screenshot",
                source_url=source_url,
                excerpt=f"PNG SHA-256: {screenshot_sha256}",
                supports_claim="The authenticated worker supplied a PNG observation with this hash; page truth is not established by its signature.",
            ),
        ]
        data = {
            "task_id": str(call.task_id),
            "page_observation": page_observation,
        }
        if call.tool_name == "browser.navigate":
            data["navigated"] = True
        else:
            data["clicked"] = True
        return ToolResult(success=True, data=data, evidence=evidence)

    def verify_result(self, tool_name: str, expected: str, result: ToolResult) -> bool:
        """Run a separate host-owned verifier; signatures are not verification."""
        if self.verifier is None or not result.success:
            return False
        try:
            return bool(self.verifier(tool_name, expected, result))
        except Exception:
            logger.warning("Remote browser verifier failed: tool_name=%s", tool_name)
            return False

    def cleanup_task(self, task_id: UUID) -> None:
        """Best-effort single signed close using the last real task approval."""
        state = self._sessions.pop(task_id, None)
        if (
            state is None or state.last_approval is None or state.last_tool_name is None
            or state.last_step_id is None or self.client is None
            or not self.enabled or not getattr(self.client, "enabled", False)
        ):
            return
        try:
            close = make_worker_request(
                task_id=task_id,
                step_id=state.last_step_id,
                session_id=state.session_id,
                operation=CloseBrowser(operation="close"),
                worker_id=self.client.worker_id,
                approval_id=state.last_approval.approval_id,
                tool_name=state.last_tool_name,
                ttl_seconds=MAX_REQUEST_TTL_SECONDS,
            )
            self.client.observe(close)
        except Exception as exc:
            # The entry is removed before attempting cleanup so a terminal
            # callback cannot replay the close request.
            logger.warning("Remote browser cleanup failed: exception_class=%s", type(exc).__name__)

    def _observe(self, request):
        if self.client is None or not callable(getattr(self.client, "observe", None)):
            raise WorkerProtocolError("worker_disabled", "remote browser worker is disabled")
        try:
            return self.client.observe(request)
        except (ToolExecutionCancelled, ToolExecutionOutcomeUnknown):
            raise
        except Exception as exc:
            category = exc.code if isinstance(exc, WorkerProtocolError) else type(exc).__name__
            raise ToolExecutionOutcomeUnknown(
                f"Remote browser request outcome is unknown ({category})"
            ) from None

    def _make_request(self, call: ToolCall, approval: ApprovalRequest, session_id: UUID, operation):
        return make_worker_request(
            task_id=call.task_id,
            step_id=call.step_id,
            session_id=session_id,
            operation=operation,
            worker_id=self.client.worker_id,
            approval_id=approval.approval_id,
            tool_name=call.tool_name,
            ttl_seconds=MAX_REQUEST_TTL_SECONDS,
        )

    def _get_or_create_session(self, task_id: UUID) -> _TaskBrowserSession:
        state = self._sessions.get(task_id)
        if state is None:
            state = _TaskBrowserSession(session_id=uuid4())
            self._sessions[task_id] = state
        return state

    def _operation(self, call: ToolCall):
        if call.tool_name == "browser.navigate":
            from backend.app.browser.tools import BrowserNavigateInput

            arguments = BrowserNavigateInput.model_validate(call.arguments)
            return Navigate(operation="navigate", url=str(arguments.url))
        if call.tool_name == "browser.click":
            from backend.app.browser.tools import BrowserClickInput

            arguments = BrowserClickInput.model_validate(call.arguments)
            return Click(operation="click", target=self._locator(arguments.target))
        raise ValueError("unsupported browser operation")

    @staticmethod
    def _locator(value: dict) -> Locator:
        accepted = {"type", "kind", "role", "value", "selector", "name", "exact"}
        if set(value) - accepted:
            raise ValueError("unsupported locator fields")
        if "type" in value and "kind" in value and value["type"] != value["kind"]:
            raise ValueError("ambiguous locator type")
        kind = value.get("type", value.get("kind"))
        if kind == "role":
            if "role" not in value or "value" in value or "selector" in value:
                raise ValueError("invalid role locator")
            target = value["role"]
        else:
            if "role" in value or ("value" in value and "selector" in value):
                raise ValueError("ambiguous locator value")
            target = value.get("value", value.get("selector"))
        if not isinstance(target, str) or not target.strip():
            raise ValueError("invalid locator value")
        name = value.get("name")
        exact = value.get("exact", False)
        if name is not None and not isinstance(name, str):
            raise ValueError("invalid locator name")
        if not isinstance(exact, bool):
            raise ValueError("invalid locator exact flag")
        return Locator(type=kind, value=target.strip(), name=name, exact=exact)

    def _validated_url(self, url: str) -> str:
        return self._validated_url_for_hosts(url, self.allowed_hosts)

    @staticmethod
    def _validated_url_for_hosts(url: str, allowed_hosts: Iterable[str]) -> str:
        allowed = frozenset(allowed_hosts)
        if not isinstance(url, str) or len(url) > 4096 or url != url.strip():
            raise ValueError("invalid URL")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url) or "\\" in url:
            raise ValueError("invalid URL")
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            raise ValueError("invalid URL") from None
        host = parts.hostname or ""
        if (
            parts.scheme.casefold() != "https"
            or not host.isascii()
            or host != host.casefold()
            or host not in allowed
            or port not in {None, 443}
            or parts.username is not None
            or parts.password is not None
            or not _HOST_PATTERN.fullmatch(host)
        ):
            raise ValueError("URL is outside the exact HTTPS allowlist")
        # Return the browser URL with query/fragment intact; those are needed
        # for navigation but are removed from persisted provenance below.
        return url

    @staticmethod
    def _evidence_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit(("https", parts.hostname or "", parts.path or "/", "", ""))

    @staticmethod
    def _receipt_metadata(receipt) -> dict[str, str | int]:
        return {
            "receipt_id": str(receipt.receipt_id),
            "request_sha256": receipt.request_sha256,
            "response_sha256": receipt.response_sha256,
            "worker_key_id": receipt.worker_key_id,
            "received_at": receipt.received_at,
        }

    @staticmethod
    def _approval_matches(approval: ApprovalRequest | None, call: ToolCall) -> bool:
        return bool(
            approval is not None
            and approval.status == ApprovalStatus.APPROVED
            and approval.task_id == call.task_id
            and approval.step_id == call.step_id
            and approval.tool_name == call.tool_name
            and approval.arguments == call.arguments
        )

    @staticmethod
    def _failure(code: str, message: str) -> ToolResult:
        return ToolResult(success=False, data={"failure_code": code}, error=message)
