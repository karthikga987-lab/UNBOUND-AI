"""Registry-compatible browser tool definitions and private callable bindings.

This module exposes registrations but deliberately has no ToolRegistry
dependency. The orchestrator owns registration and approval decisions.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from functools import wraps
from threading import RLock
from time import monotonic
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, UUID4, model_validator

from backend.app.browser.controller import BrowserController, BrowserControllerError, _parse_target, _validate_http_url
from backend.app.browser.downloads import BrowserDownloadResult
from backend.app.browser.recorder import BrowserRecorder
from backend.app.browser.replay import BrowserSessionBinding, BrowserSessionManager, ReplayPolicyHandoff, WorkflowReplay
from backend.app.browser.replay_state import ReplayContinuation, ReplayExecutionResult, ReplayStepAuthorization
from backend.app.browser.verification import BrowserVerifier
from backend.app.browser.workflow_store import WorkflowStore
from backend.app.schemas.contracts import (
    EvidenceItem,
    EvidenceSourceType,
    EventStatus,
    ToolCall,
    ToolDefinition,
    ToolResult,
)


logger = logging.getLogger(__name__)


class BrowserInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def _validate_structured_target(target: dict[str, Any]) -> None:
    try:
        _parse_target(target)
    except (TypeError, ValueError):
        # Do not echo untrusted selectors or target text through Pydantic errors.
        raise ValueError("target must use a supported structured locator") from None


class BrowserNavigateInput(BrowserInput):
    url: AnyHttpUrl


class BrowserClickInput(BrowserInput):
    target: dict[str, Any]

    @model_validator(mode="after")
    def validate_target(self) -> "BrowserClickInput":
        _validate_structured_target(self.target)
        return self


class BrowserFillInput(BrowserInput):
    target: dict[str, Any]
    value: str

    @model_validator(mode="after")
    def validate_target(self) -> "BrowserFillInput":
        _validate_structured_target(self.target)
        return self


class BrowserRecordInput(BrowserInput):
    operation: Literal["start", "stop"]
    name: str | None = None
    description: str = ""
    start_url: AnyHttpUrl | None = None

    @model_validator(mode="after")
    def validate_operation(self) -> "BrowserRecordInput":
        if self.operation == "start" and (self.name is None or not self.name.strip()):
            raise ValueError("name is required when starting a recording")
        if self.operation == "stop" and (self.name is not None or self.start_url is not None):
            raise ValueError("name and start_url are only valid when starting a recording")
        return self


class BrowserReplayInput(BrowserInput):
    operation: Literal["start", "resume"]
    workflow_id: UUID4 | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_operation(self) -> "BrowserReplayInput":
        if self.operation == "start" and self.workflow_id is None:
            raise ValueError("workflow_id is required when starting a replay")
        if self.operation == "resume" and (self.workflow_id is not None or self.parameters):
            raise ValueError("workflow_id and parameters are not accepted when resuming")
        return self


class BrowserDownloadInput(BrowserInput):
    target: dict[str, Any]

    @model_validator(mode="after")
    def validate_target(self) -> "BrowserDownloadInput":
        _validate_structured_target(self.target)
        return self


@dataclass(frozen=True)
class BrowserToolBinding:
    """One serializable definition paired with a private executable binding."""

    definition: ToolDefinition
    input_model: type[BaseModel]
    execute: Callable[[BaseModel, ToolCall], ToolResult]


@dataclass(frozen=True)
class TrustedReplayResumeContext:
    """Trusted orchestration data; it is never accepted from tool arguments."""

    continuation: ReplayContinuation
    authorization: ReplayStepAuthorization
    parent_tool_call: ToolCall


class ReplayResumeContextProvider(Protocol):
    def resolve(self, tool_call: ToolCall) -> TrustedReplayResumeContext | None: ...


_TOOL_SPECS: tuple[tuple[str, str, type[BaseModel], bool], ...] = (
    ("browser.navigate", "Navigate the task's active browser session to an HTTP or HTTPS URL.", BrowserNavigateInput, False),
    ("browser.click", "Click one element identified by a structured browser locator.", BrowserClickInput, False),
    ("browser.fill", "Fill one input identified by a structured browser locator.", BrowserFillInput, False),
    ("browser.record", "Start or stop recording a reusable browser workflow.", BrowserRecordInput, False),
    ("browser.replay", "Start or resume one authorized action in a saved browser workflow.", BrowserReplayInput, True),
    ("browser.download", "Download a file from the active browser session to controlled storage.", BrowserDownloadInput, False),
)


def _session_serialized(method):
    """Hold the concrete manager's task lock throughout a browser operation."""
    @wraps(method)
    def wrapped(self, arguments, tool_call):
        manager = self.session_manager
        lease = getattr(manager, "session_lease", None) if manager is not None else None
        if not callable(lease):
            return method(self, arguments, tool_call)
        resume_without_session = (
            method.__name__ == "replay" and arguments.operation == "resume"
        )
        existing = None
        if not resume_without_session:
            resolver = getattr(manager, "get_session_for_tool_call", None)
            if callable(resolver):
                try:
                    existing = resolver(tool_call)
                except Exception:
                    existing = None
        launch_started_at = monotonic()
        launch_attempted = not resume_without_session and existing is None
        session_acquired = False
        if launch_attempted:
            self._emit_execution(
                tool_call, "browser.launch_started", EventStatus.RUNNING,
                "Starting a task-scoped browser session",
            )
        try:
            with lease(tool_call, create=not resume_without_session) as session:
                session_acquired = session is not None
                if session is None and not resume_without_session:
                    if launch_attempted:
                        self._emit_execution(
                            tool_call, "browser.launch_failed", EventStatus.FAILED,
                            "Browser session could not be launched",
                            duration_ms=max(0, int((monotonic() - launch_started_at) * 1000)),
                            error_category="browser_launch_failure",
                        )
                    return self._unavailable()
                if launch_attempted and session is not None:
                    self._emit_execution(
                        tool_call, "browser.launch_completed", EventStatus.RUNNING,
                        "Task-scoped browser session launched",
                        session=session,
                        duration_ms=max(0, int((monotonic() - launch_started_at) * 1000)),
                    )
                # Resume is allowed through to WorkflowReplay without a live
                # session so the encrypted store can mark continuity lost.
                return method(self, arguments, tool_call)
        except Exception:
            if launch_attempted and not session_acquired:
                self._emit_execution(
                    tool_call, "browser.launch_failed", EventStatus.FAILED,
                    "Browser session could not be launched",
                    duration_ms=max(0, int((monotonic() - launch_started_at) * 1000)),
                    error_category="browser_launch_failure",
                )
                return self._unavailable()
            return self._failed("Task-scoped browser operation could not be completed safely")

    return wrapped


class BrowserToolAdapter:
    """Build browser tool bindings around injected task-scoped dependencies."""

    def __init__(
        self,
        *,
        workflow_store: WorkflowStore,
        session_manager: BrowserSessionManager | None = None,
        replay_engine: WorkflowReplay | None = None,
        policy_handoff: ReplayPolicyHandoff | None = None,
        resume_context_provider: ReplayResumeContextProvider | None = None,
        live_event_publisher: Callable[..., Any] | None = None,
        execution_event_publisher: Callable[..., Any] | None = None,
        task_allowed_hosts_provider: Callable[[ToolCall], Any] | None = None,
        provider_name: str = "local_chromium",
    ) -> None:
        self.workflow_store = workflow_store
        self.session_manager = session_manager
        self.replay_engine = replay_engine
        self.policy_handoff = policy_handoff
        self.resume_context_provider = resume_context_provider
        self.live_event_publisher = live_event_publisher
        self.execution_event_publisher = execution_event_publisher
        self.task_allowed_hosts_provider = task_allowed_hosts_provider
        self.provider_name = provider_name
        self._recorders: dict[UUID, tuple[UUID, BrowserRecorder]] = {}
        # Verification context is private to the adapter and is consumed by the
        # executor's independent verifier immediately after each tool call.
        self._verification_context: dict[UUID, dict[str, Any]] = {}
        self._lock = RLock()
        self._bindings = self._make_bindings()

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(binding.definition.model_copy(deep=True) for binding in self._bindings)

    @property
    def bindings(self) -> tuple[BrowserToolBinding, ...]:
        """Return definitions, validators and callables for external registration."""
        return self._bindings

    def cleanup_task(self, task_id: UUID) -> None:
        """Discard task-owned recorder state during terminal task cleanup."""
        if not isinstance(task_id, UUID) or task_id.version != 4:
            return
        with self._lock:
            self._recorders.pop(task_id, None)
            self._verification_context.pop(task_id, None)

    def cleanup_all(self) -> None:
        """Discard recorder bookkeeping during application shutdown."""
        with self._lock:
            self._recorders.clear()
            self._verification_context.clear()

    def _make_bindings(self) -> tuple[BrowserToolBinding, ...]:
        methods = {
            "browser.navigate": self.navigate,
            "browser.click": self.click,
            "browser.fill": self.fill,
            "browser.record": self.record,
            "browser.replay": self.replay,
            "browser.download": self.download,
        }
        return tuple(
            BrowserToolBinding(
                definition=ToolDefinition(
                    tool_name=name,
                    description=description,
                    input_schema=model.model_json_schema(),
                    requires_approval=requires_approval,
                ),
                input_model=model,
                execute=methods[name],
            )
            for name, description, model, requires_approval in _TOOL_SPECS
        )

    @_session_serialized
    def navigate(self, arguments: BrowserNavigateInput, tool_call: ToolCall) -> ToolResult:
        self._emit_live(tool_call, "connecting_to_browserless" if self.provider_name == "browserless_cloud_demo" else "connecting_to_browser", "Connecting to browser provider")
        session = self._session(tool_call)
        if session is None:
            self._emit_live(tool_call, "failed", "Browser session could not be created")
            return self._unavailable()
        try:
            if getattr(session.controller, "provider_name", None) == "browserless_cloud_demo":
                provider = self.task_allowed_hosts_provider
                setter = getattr(session.controller, "set_task_allowed_hosts", None)
                if not callable(provider) or not callable(setter):
                    raise BrowserControllerError("policy", "Task-scoped Browserless authorization is unavailable")
                setter(provider(tool_call))
            self._emit_live(tool_call, "browser_session_created", "Browser session created", session=session)
            requested_url = str(arguments.url)
            self._emit_live(tool_call, "navigating_to_website", "Navigating to website", session=session, url=self._safe_page_url(requested_url))
            navigation_started_at = monotonic()
            self._emit_execution(
                tool_call, "browser.navigation_started", EventStatus.RUNNING,
                "Navigating the active browser session to the requested URL",
                session=session, diagnostic={"url": self._safe_page_url(requested_url)},
            )
            navigate_with_screenshots = getattr(session.controller, "navigate_with_screenshots", None)
            try:
                if getattr(session.controller, "provider_name", None) == "browserless_cloud_demo" and callable(navigate_with_screenshots):
                    final_url = navigate_with_screenshots(
                        str(arguments.url),
                        lambda frame: self._emit_live(
                            tool_call,
                            "screenshot_updated",
                            "Live browser screenshot updated",
                            session=session,
                            url=self._safe_page_url(session.controller.current_url),
                            title=session.controller.title(),
                            screenshot_png=frame,
                        ),
                    )
                else:
                    final_url = session.controller.navigate(str(arguments.url))
            except Exception:
                rejection = getattr(session.controller, "last_policy_rejection", None)
                is_policy_rejection = (
                    getattr(session.controller, "provider_name", None) == "browserless_cloud_demo"
                    and isinstance(rejection, dict)
                    and isinstance(rejection.get("reason"), str)
                )
                self._emit_execution(
                    tool_call, "browser.navigation_failed", EventStatus.FAILED,
                    (
                        f"Browser request blocked by task policy: {rejection['reason']}"
                        if is_policy_rejection
                        else "Browser navigation failed before a page load was confirmed"
                    ),
                    session=session,
                    duration_ms=max(0, int((monotonic() - navigation_started_at) * 1000)),
                    error_category=(
                        "destination_policy_rejected" if is_policy_rejection
                        else "browserless_provider_or_transport_failure"
                        if getattr(session.controller, "provider_name", None) == "browserless_cloud_demo"
                        else "navigation_failure"
                    ),
                    diagnostic=(
                        {"policy": "browserless_task_scoped", "reason": rejection["reason"]}
                        if is_policy_rejection
                        else {"failure_source": "provider_or_transport"}
                        if getattr(session.controller, "provider_name", None) == "browserless_cloud_demo"
                        else None
                    ),
                )
                raise
            self._emit_execution(
                tool_call, "browser.navigation_completed", EventStatus.COMPLETED,
                "Browser navigation reached a committed page",
                session=session,
                duration_ms=max(0, int((monotonic() - navigation_started_at) * 1000)),
                diagnostic={"final_url": self._safe_page_url(final_url)},
            )
            self._emit_live(tool_call, "page_loaded", "Page loaded", session=session, url=self._safe_page_url(final_url), title=session.controller.title())
            self._emit_live(tool_call, "extracting_content", "Extracting visible page content", session=session, url=self._safe_page_url(final_url), title=session.controller.title())
            extraction_started_at = monotonic()
            try:
                observation = self._observe(session.controller)
            except Exception:
                self._emit_execution(
                    tool_call, "browser.extraction_failed", EventStatus.FAILED,
                    "Visible page content could not be extracted",
                    session=session,
                    duration_ms=max(0, int((monotonic() - extraction_started_at) * 1000)),
                    error_category="result_extraction_failure",
                )
                raise
            self._emit_execution(
                tool_call, "browser.results_extracted", EventStatus.COMPLETED,
                "Visible page information was extracted from the active browser",
                session=session,
                duration_ms=max(0, int((monotonic() - extraction_started_at) * 1000)),
                diagnostic={"extracted_fields": sorted(observation.keys())},
            )
            self._emit_live(tool_call, "capturing_screenshot", "Capturing screenshot from the active browser page", session=session, url=observation["url"], title=observation["title"])
            screenshot_started_at = monotonic()
            try:
                image = session.controller.screenshot_png(max_bytes=512 * 1024)
            except Exception:
                self._emit_execution(
                    tool_call, "browser.screenshot_failed", EventStatus.FAILED,
                    "Screenshot capture failed for the observed page",
                    session=session,
                    duration_ms=max(0, int((monotonic() - screenshot_started_at) * 1000)),
                    error_category="screenshot_capture_failure",
                )
                raise
            screenshot_digest = hashlib.sha256(image).hexdigest()
            self._emit_execution(
                tool_call, "browser.screenshot_captured", EventStatus.COMPLETED,
                "A bounded screenshot was captured from the active browser page",
                session=session,
                duration_ms=max(0, int((monotonic() - screenshot_started_at) * 1000)),
                screenshot_reference=f"sha256:{screenshot_digest}",
            )
            self._emit_live(tool_call, "screenshot_updated", "Live browser screenshot updated", session=session, url=observation["url"], title=observation["title"], screenshot_png=image)
            screenshot_evidence = self._attach_local_demo_screenshot(session.controller, observation, tool_call.task_id, image=image)
            if getattr(session.controller, "provider_name", "") == "browserless_cloud_demo":
                screenshot_evidence = self._attach_page_screenshot(observation, tool_call.task_id, image, "Browserless Cloud PNG screenshot")
            self._set_verification_context(tool_call, {
                "operation": "navigate",
                "requested_url": requested_url,
                "provider": getattr(session.controller, "provider_name", self.provider_name),
            })
            return self._success(
                {"navigated": True, "page_observation": observation},
                task_call=tool_call,
                claim="The browser reached the requested page and captured its visible content",
                source_url=self._safe_page_url(final_url),
                additional_evidence=screenshot_evidence,
            )
        except Exception:
            if getattr(session.controller, "provider_name", None) == "browserless_cloud_demo":
                result = self._browserless_navigation_failure(session.controller)
                self._emit_live(tool_call, "failed", result.error or "Browserless navigation failed", session=session)
                return result
            self._emit_live(tool_call, "failed", "Browser navigation or observation failed", session=session)
            return self._failed("Browser navigation failed safely")

    @_session_serialized
    def click(self, arguments: BrowserClickInput, tool_call: ToolCall) -> ToolResult:
        session = self._session(tool_call)
        if session is None:
            return self._unavailable()
        try:
            self._emit_live(tool_call, "extracting_content", "Reading the current page before the click", session=session, url=self._safe_page_url(session.controller.current_url), title=session.controller.title())
            before = self._observe(session.controller)
            action_started_at = monotonic()
            self._emit_execution(
                tool_call, "browser.interaction_started", EventStatus.RUNNING,
                "Performing the approved browser interaction", session=session,
            )
            try:
                session.controller.click(arguments.target)
            except Exception:
                self._emit_execution(
                    tool_call, "browser.interaction_failed", EventStatus.FAILED,
                    "The approved browser interaction failed",
                    session=session,
                    duration_ms=max(0, int((monotonic() - action_started_at) * 1000)),
                    error_category="browser_action_failure",
                )
                raise
            self._emit_execution(
                tool_call, "browser.interaction_completed", EventStatus.COMPLETED,
                "The approved browser interaction completed",
                session=session,
                duration_ms=max(0, int((monotonic() - action_started_at) * 1000)),
            )
            extraction_started_at = monotonic()
            try:
                observation = self._observe(session.controller)
            except Exception:
                self._emit_execution(
                    tool_call, "browser.extraction_failed", EventStatus.FAILED,
                    "Visible page content could not be extracted after the interaction",
                    session=session,
                    duration_ms=max(0, int((monotonic() - extraction_started_at) * 1000)),
                    error_category="result_extraction_failure",
                )
                raise
            self._emit_execution(
                tool_call, "browser.results_extracted", EventStatus.COMPLETED,
                "Visible page information was extracted after the browser interaction",
                session=session,
                duration_ms=max(0, int((monotonic() - extraction_started_at) * 1000)),
                diagnostic={"extracted_fields": sorted(observation.keys())},
            )
            self._emit_live(tool_call, "page_loaded", "Page updated after click", session=session, url=observation["url"], title=observation["title"])
            self._emit_live(tool_call, "capturing_screenshot", "Capturing screenshot from the active browser page", session=session, url=observation["url"], title=observation["title"])
            screenshot_started_at = monotonic()
            try:
                image = session.controller.screenshot_png(max_bytes=512 * 1024)
            except Exception:
                self._emit_execution(
                    tool_call, "browser.screenshot_failed", EventStatus.FAILED,
                    "Screenshot capture failed for the observed page",
                    session=session,
                    duration_ms=max(0, int((monotonic() - screenshot_started_at) * 1000)),
                    error_category="screenshot_capture_failure",
                )
                raise
            self._emit_execution(
                tool_call, "browser.screenshot_captured", EventStatus.COMPLETED,
                "A bounded screenshot was captured after the browser interaction",
                session=session,
                duration_ms=max(0, int((monotonic() - screenshot_started_at) * 1000)),
                screenshot_reference=f"sha256:{hashlib.sha256(image).hexdigest()}",
            )
            self._emit_live(tool_call, "screenshot_updated", "Live browser screenshot updated", session=session, url=observation["url"], title=observation["title"], screenshot_png=image)
            screenshot_evidence = self._attach_local_demo_screenshot(session.controller, observation, tool_call.task_id, image=image)
            if getattr(session.controller, "provider_name", "") == "browserless_cloud_demo":
                screenshot_evidence = self._attach_page_screenshot(observation, tool_call.task_id, image, "Browserless Cloud PNG screenshot")
            self._set_verification_context(tool_call, {
                "operation": "click", "before": before, "target": dict(arguments.target),
                "provider": getattr(session.controller, "provider_name", "local_chromium"),
            })
            return self._success(
                {"clicked": True, "page_observation": observation}, task_call=tool_call,
                claim="The browser click changed the observed page state", source_url=observation["url"],
                additional_evidence=screenshot_evidence,
            )
        except Exception:
            self._emit_live(tool_call, "failed", "Browser click failed", session=session)
            return self._failed("Browser click failed safely")

    @_session_serialized
    def fill(self, arguments: BrowserFillInput, tool_call: ToolCall) -> ToolResult:
        session = self._session(tool_call)
        if session is None:
            return self._unavailable()
        try:
            session.controller.fill(arguments.target, arguments.value)
            observation = self._observe(session.controller)
            self._set_verification_context(tool_call, {
                "operation": "fill", "target": dict(arguments.target), "value": arguments.value,
            })
            # The supplied value is never included in data, evidence, or errors.
            return self._success(
                {"filled": True, "page_observation": observation}, task_call=tool_call,
                claim="The browser input contains the requested value", source_url=observation["url"],
            )
        except Exception:
            return self._failed("Browser fill failed safely")

    @_session_serialized
    def record(self, arguments: BrowserRecordInput, tool_call: ToolCall) -> ToolResult:
        session = self._session(tool_call)
        if session is None:
            return self._unavailable()
        with self._lock:
            current = self._recorders.get(tool_call.task_id)
            if arguments.operation == "start":
                if current is not None:
                    return self._failed("A workflow recording is already active for this task")
                recorder = BrowserRecorder(session.controller)
                try:
                    start_url = str(arguments.start_url) if arguments.start_url else session.controller.current_url
                    _validate_http_url(start_url)
                    recorder.start_recording(
                        arguments.name or "",
                        description=arguments.description,
                        start_url=start_url,
                    )
                except Exception:
                    return self._failed("Workflow recording could not be started safely")
                self._recorders[tool_call.task_id] = (session.session_id, recorder)
                self._set_verification_context(tool_call, {"operation": "record_start", "session_id": session.session_id})
                return self._success({"recording": True}, task_call=tool_call, claim="Workflow recording started")

            if current is None or current[0] != session.session_id:
                return self._failed("No active workflow recording exists for this browser session")
            _, recorder = current
            try:
                workflow = recorder.stop_recording()
                saved = self.workflow_store.save(workflow)
            except Exception:
                self._recorders.pop(tool_call.task_id, None)
                return self._failed("Workflow recording could not be saved safely")
            self._recorders.pop(tool_call.task_id, None)
            self._set_verification_context(tool_call, {"operation": "record_stop", "workflow_id": saved.workflow_id})
            return self._success(
                {"recording": False, "workflow_id": str(saved.workflow_id), "name": saved.name},
                task_call=tool_call,
                claim="Recorded workflow was saved",
                source_url=self._safe_origin(str(saved.start_url)),
            )

    @_session_serialized
    def replay(self, arguments: BrowserReplayInput, tool_call: ToolCall) -> ToolResult:
        if self.session_manager is None:
            return self._unavailable()
        if arguments.operation == "start" and self._session(tool_call) is None:
            return self._unavailable()
        if self.replay_engine is None:
            return self._unavailable("Browser replay engine is not configured")
        try:
            if arguments.operation == "start":
                if self.policy_handoff is None:
                    return self._unavailable("Trusted replay policy handoff is not configured")
                result = self.replay_engine.start_replay(
                    arguments.workflow_id,
                    arguments.parameters,
                    tool_call=tool_call,
                    policy_handoff=self.policy_handoff,
                )
            else:
                if self.resume_context_provider is None:
                    return self._unavailable("Trusted replay continuation provider is not configured")
                context = self.resume_context_provider.resolve(tool_call)
                if context is None:
                    return self._failed("Trusted replay continuation is unavailable")
                if (
                    context.parent_tool_call.task_id != tool_call.task_id
                    or context.parent_tool_call.tool_name != "browser.replay"
                    or context.continuation.task_id != tool_call.task_id
                    or context.authorization.task_id != tool_call.task_id
                    or context.authorization.tool_call_id != context.parent_tool_call.tool_call_id
                    or context.authorization.replay_run_id != context.continuation.replay_run_id
                    or context.authorization.continuation_id != context.continuation.continuation_id
                    or context.authorization.workflow_id != context.continuation.workflow_id
                ):
                    return self._failed("Trusted replay continuation does not belong to this task")
                result = self.replay_engine.resume_replay(
                    context.continuation,
                    context.authorization,
                    tool_call=context.parent_tool_call,
                )
            if not isinstance(result, ReplayExecutionResult):
                return self._failed("Replay engine returned an invalid result")
            if result.status == "completed":
                session = self._session(tool_call, create=False)
                if session is not None:
                    self._set_verification_context(tool_call, {
                        "operation": "replay", "replay_status": result.status,
                    })
            # Result models intentionally omit prepared arguments and authorization.
            return ToolResult(success=result.status != "failed", data=result.model_dump(mode="json"))
        except Exception:
            return self._failed("Browser workflow replay failed safely")

    @_session_serialized
    def download(self, arguments: BrowserDownloadInput, tool_call: ToolCall) -> ToolResult:
        session = self._session(tool_call)
        if session is None:
            return self._unavailable()
        try:
            downloaded = session.controller.download(arguments.target)
            if not isinstance(downloaded, BrowserDownloadResult) or not downloaded.success:
                return self._failed("Browser download failed safely")
            verifier = BrowserVerifier(
                session.controller,
                download_directory=str(session.controller.download_store.directory),
            )
            verification = verifier.verify_download(downloaded)
            if not verification.success:
                return self._failed("Downloaded file could not be verified")
            self._set_verification_context(tool_call, {
                "operation": "download", "file_path": downloaded.file_path,
                "file_name": downloaded.file_name, "size_bytes": downloaded.size_bytes,
            })
            return self._success(
                {
                    "downloaded": True,
                    "file_name": downloaded.file_name,
                    "file_ref": downloaded.file_path,
                    "size_bytes": downloaded.size_bytes,
                    "verification": {
                        "success": True,
                        "checks": [check.name for check in verification.checks if check.success],
                    },
                },
                task_call=tool_call,
                claim="Browser download was stored and verified",
            )
        except Exception:
            return self._failed("Browser download could not be completed safely")

    def verify_result(self, tool_name: str, _expected_outcome: str, result: ToolResult) -> bool:
        """Independently compare a tool result with the live browser state."""
        if not result.success or not isinstance(result.data, dict):
            return False
        task_id_value = result.data.get("task_id")
        try:
            task_id = UUID(str(task_id_value))
        except (ValueError, TypeError):
            return False
        with self._lock:
            context = self._verification_context.pop(task_id, None)
        if not context:
            return False
        if context.get("operation") == "download":
            try:
                from pathlib import Path
                file_path = Path(str(context["file_path"]))
                return file_path.is_file() and file_path.stat().st_size == context["size_bytes"] > 0
            except (OSError, KeyError, TypeError, ValueError):
                return False
        session = self.session_manager.get_session(task_id) if self.session_manager is not None else None
        if session is None:
            return False
        try:
            actual = self._observe(session.controller)
        except Exception:
            return False
        operation = context.get("operation")
        observed = result.data.get("page_observation")
        if operation == "navigate" and not isinstance(observed, dict):
            return False
        if isinstance(observed, dict):
            required_page_fields = {"url", "title", "visible_text", "truncated", "content_trust"}
            if operation == "navigate" and not required_page_fields.issubset(observed):
                return False
            if operation == "navigate" and (
                not isinstance(observed.get("url"), str)
                or not isinstance(observed.get("title"), str)
                or not observed["title"].strip()
                or not isinstance(observed.get("visible_text"), str)
                or not observed["visible_text"].strip()
                or not isinstance(observed.get("truncated"), bool)
                or observed.get("content_trust") != "untrusted_webpage_data"
            ):
                return False
            screenshot_fields = {"screenshot_format", "screenshot_sha256", "screenshot_base64"}
            if operation == "navigate" and context.get("provider") == "browserless_cloud_demo":
                if not screenshot_fields.issubset(observed):
                    return False
            if any(key in observed for key in ("screenshot_format", "screenshot_sha256", "screenshot_base64")):
                if not self._valid_screenshot_observation(observed, result.evidence):
                    return False
            observed_page = {key: value for key, value in observed.items() if key not in {"screenshot_base64", "screenshot_sha256", "screenshot_format"}}
            if observed_page != actual:
                return False
        elif observed is not None and observed != actual:
            return False
        if operation == "navigate":
            requested_origin = self._safe_page_origin(context.get("requested_url", ""))
            observed_origin = self._safe_page_origin(actual.get("url", ""))
            return (
                tool_name == "browser.navigate"
                and result.data.get("navigated") is True
                and requested_origin is not None
                and observed_origin == requested_origin
                and bool(actual["title"])
                and bool(actual["visible_text"].strip())
            )
        if operation == "click":
            before = context.get("before")
            target = context.get("target", {})
            if context.get("provider") == "browserless_cloud_demo":
                return (
                    tool_name == "browser.click"
                    and isinstance(before, dict)
                    and (actual["url"] != before.get("url") or actual["visible_text"] != before.get("visible_text"))
                )
            return (
                tool_name == "browser.click"
                and isinstance(before, dict)
                and (actual["url"] != before.get("url") or actual["visible_text"] != before.get("visible_text"))
                and self._is_local_demo_details_state(actual, target)
            )
        if operation == "fill":
            try:
                locator = session.controller._locator(session.controller._require_page("verify fill"), context["target"])
                return tool_name == "browser.fill" and locator.input_value(timeout=session.controller.timeout_ms) == context["value"]
            except Exception:
                return False
        if operation == "replay":
            return tool_name == "browser.replay" and result.data.get("status") == "completed"
        if operation in {"record_start", "record_stop"}:
            if tool_name != "browser.record":
                return False
            if operation == "record_start":
                return task_id in self._recorders and self._recorders[task_id][0] == context.get("session_id")
            try:
                return self.workflow_store.load(context["workflow_id"]) is not None
            except Exception:
                return False
        return False

    @staticmethod
    def _valid_screenshot_observation(observation: dict[str, Any], evidence: list[EvidenceItem]) -> bool:
        try:
            if observation.get("screenshot_format") != "image/png":
                return False
            encoded = observation.get("screenshot_base64")
            expected = observation.get("screenshot_sha256")
            if not isinstance(encoded, str) or len(encoded) > 700_000 or not isinstance(expected, str) or len(expected) != 64:
                return False
            image = base64.b64decode(encoded, validate=True)
            if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > 512 * 1024:
                return False
            digest = hashlib.sha256(image).hexdigest()
            if digest != expected:
                return False
            source_url = observation.get("url")
            return any(
                item.source_url is not None
                and str(item.source_url) == source_url
                and isinstance(item.excerpt, str)
                and digest in item.excerpt
                for item in evidence
            )
        except (ValueError, TypeError):
            return False

    def _set_verification_context(self, tool_call: ToolCall, context: dict[str, Any]) -> None:
        with self._lock:
            self._verification_context[tool_call.task_id] = context

    @staticmethod
    def _observe(controller: BrowserController) -> dict[str, Any]:
        url = BrowserToolAdapter._safe_page_url(controller.current_url)
        title = controller.title()[:300]
        visible = controller.visible_content()
        limit = 4000
        return {
            "url": url,
            "title": title,
            "visible_text": visible[:limit],
            "truncated": len(visible) > limit,
            "content_trust": "untrusted_webpage_data",
        }

    @staticmethod
    def _attach_local_demo_screenshot(controller: BrowserController, observation: dict[str, Any], task_id: UUID, *, image: bytes | None = None) -> list[EvidenceItem]:
        """Include screenshot evidence only for the fixed synthetic fixture."""
        try:
            parts = urlsplit(observation.get("url", ""))
            if (parts.scheme, parts.hostname, parts.port, parts.path) not in {
                ("http", "unbound-demo.invalid", None, "/details"),
            }:
                return []
            image = image or controller.screenshot_png(max_bytes=1_000_000)
        except Exception:
            # The fixture workflow must not report success without its requested evidence.
            raise
        digest = hashlib.sha256(image).hexdigest()
        observation["screenshot_format"] = "image/png"
        observation["screenshot_sha256"] = digest
        observation["screenshot_base64"] = base64.b64encode(image).decode("ascii")
        return [EvidenceItem(
            task_id=task_id,
            source_type=EvidenceSourceType.BROWSER,
            source_name="Local Demo PNG screenshot",
            source_url=observation["url"],
            excerpt=f"PNG SHA-256: {digest}",
            supports_claim="A bounded screenshot was captured from the fixed synthetic Local Demo page.",
        )]

    @staticmethod
    def _attach_page_screenshot(observation: dict[str, Any], task_id: UUID, image: bytes, source_name: str) -> list[EvidenceItem]:
        digest = hashlib.sha256(image).hexdigest()
        observation["screenshot_format"] = "image/png"
        observation["screenshot_sha256"] = digest
        observation["screenshot_base64"] = base64.b64encode(image).decode("ascii")
        return [EvidenceItem(
            task_id=task_id,
            source_type=EvidenceSourceType.BROWSER,
            source_name=source_name,
            source_url=observation["url"],
            excerpt=f"PNG SHA-256: {digest}",
            supports_claim="A bounded screenshot was captured from the observed browser page.",
        )]

    def _emit_live(
        self,
        tool_call: ToolCall,
        event_type: str,
        description: str,
        *,
        session: BrowserSessionBinding | None = None,
        url: str | None = None,
        title: str | None = None,
        screenshot_png: bytes | None = None,
    ) -> None:
        if not callable(self.live_event_publisher):
            return
        try:
            self.live_event_publisher(
                tool_call.task_id,
                event_type,
                description,
                step_id=tool_call.step_id,
                session_id=session.session_id if session else None,
                url=url,
                title=title,
                provider=getattr(session.controller, "provider_name", self.provider_name) if session else self.provider_name,
                screenshot_png=screenshot_png,
            )
        except Exception:
            # Live view is observability; event transport failure must never
            # change the browser operation's security or execution result.
            return

    def _emit_execution(
        self,
        tool_call: ToolCall,
        event_type: str,
        status: EventStatus,
        message: str,
        *,
        session: BrowserSessionBinding | None = None,
        duration_ms: int | None = None,
        result_count: int | None = None,
        screenshot_reference: str | None = None,
        error_category: str | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        if not callable(self.execution_event_publisher):
            return
        controller = session.controller if session is not None else None
        provider_session_id = getattr(controller, "provider_session_id", None) if controller is not None else None
        try:
            self.execution_event_publisher(
                tool_call.task_id,
                event_type,
                status,
                message,
                step_id=tool_call.step_id,
                tool_name=tool_call.tool_name,
                browser_session_id=str(session.session_id) if session is not None else None,
                provider_session_id=str(provider_session_id)[:160] if provider_session_id else None,
                duration_ms=duration_ms,
                result_count=result_count,
                screenshot_reference=screenshot_reference,
                error_category=error_category,
                diagnostic=diagnostic,
            )
        except Exception as exc:
            # Diagnostic persistence failure must not alter browser security or
            # claim an operation result. The executor's own result checkpoint
            # independently records whether the operation was persisted.
            logger.warning(
                "Browser execution event could not be persisted: event_type=%s exception_class=%s",
                event_type, type(exc).__name__,
            )

    @staticmethod
    def _safe_page_url(value: str) -> str:
        try:
            parts = urlsplit(_validate_http_url(value))
            # Drop credentials, fragments, and query values before persisting or
            # returning browser provenance. The origin and path remain useful.
            return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", "", ""))
        except Exception:
            return ""

    @classmethod
    def _safe_page_origin(cls, value: str) -> tuple[str, str, int] | None:
        """Return a normalized origin for safe same-origin redirect checks."""
        try:
            safe_url = cls._safe_page_url(value)
            if not safe_url:
                return None
            parts = urlsplit(safe_url)
            scheme = parts.scheme.lower()
            hostname = parts.hostname
            if not hostname:
                return None
            port = parts.port or (443 if scheme == "https" else 80)
            return scheme, hostname.casefold().rstrip("."), port
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _is_local_demo_details_state(actual: dict[str, Any], target: dict[str, Any]) -> bool:
        try:
            parts = urlsplit(actual["url"])
            target_value = target.get("value") if target.get("type", target.get("kind")) != "role" else target.get("name")
            return (
                parts.scheme == "http" and parts.netloc == "unbound-demo.invalid"
                and parts.path == "/details" and target_value == "View details"
                and "Demo Product details" in actual["title"]
            )
        except (KeyError, TypeError, ValueError):
            return False

    def _session(self, tool_call: ToolCall, *, create: bool = True) -> BrowserSessionBinding | None:
        if self.session_manager is None:
            return None
        try:
            get_or_create = getattr(self.session_manager, "get_or_create_session", None)
            if create and callable(get_or_create):
                session = get_or_create(tool_call)
            else:
                resolve_for_call = getattr(self.session_manager, "get_session_for_tool_call", None)
                session = (
                    resolve_for_call(tool_call)
                    if callable(resolve_for_call)
                    else self.session_manager.get_session(tool_call.task_id)
                )
        except Exception:
            return None
        try:
            if session is None or not isinstance(session.session_id, UUID) or not session.controller.is_started:
                return None
        except Exception:
            return None
        return session

    @staticmethod
    def _success(
        data: dict[str, Any],
        *,
        task_call: ToolCall,
        claim: str,
        source_url: str | None = None,
        additional_evidence: list[EvidenceItem] | None = None,
    ) -> ToolResult:
        evidence = []
        data = {**data, "task_id": str(task_call.task_id)}
        if source_url:
            try:
                evidence = [EvidenceItem(
                    task_id=task_call.task_id,
                    source_type=EvidenceSourceType.BROWSER,
                    source_name="Active browser page",
                    source_url=source_url,
                    supports_claim=claim,
                )]
            except Exception:
                evidence = []
        if additional_evidence:
            evidence.extend(additional_evidence)
        return ToolResult(success=True, data=data, evidence=evidence)

    @staticmethod
    def _safe_origin(url: str | None) -> str | None:
        if not isinstance(url, str):
            return None
        try:
            parts = urlsplit(_validate_http_url(url))
            return f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        except (BrowserControllerError, ValueError):
            return None

    @classmethod
    def _current_origin(cls, controller: BrowserController) -> str | None:
        try:
            return cls._safe_origin(controller.current_url)
        except Exception:
            return None

    @staticmethod
    def _unavailable(message: str = "Task-scoped browser session is unavailable") -> ToolResult:
        return ToolResult(success=False, error=message)

    @staticmethod
    def _failed(message: str) -> ToolResult:
        return ToolResult(success=False, error=message)

    @staticmethod
    def _browserless_navigation_failure(controller: Any) -> ToolResult:
        """Distinguish a proven local policy block from provider/transport failure."""
        rejection = getattr(controller, "last_policy_rejection", None)
        if isinstance(rejection, dict) and isinstance(rejection.get("reason"), str):
            reason = rejection["reason"]
            return ToolResult(
                success=False,
                data={"failure_code": "browserless_destination_rejected", "rejection_reason": reason},
                error=f"Browser request blocked by task policy: {reason}",
            )
        message = "Browserless navigation failed; no application policy rejection was recorded"
        return ToolResult(
            success=False,
            data={"failure_code": "browserless_provider_or_transport_failure"},
            error=message,
        )


def browser_tool_bindings(adapter: BrowserToolAdapter) -> tuple[BrowserToolBinding, ...]:
    """Return the six compatible bindings for the host's registry to register."""
    return adapter.bindings


def browser_tool_definitions(adapter: BrowserToolAdapter) -> tuple[ToolDefinition, ...]:
    """Return serializable definitions without exposing callable objects."""
    return adapter.definitions
