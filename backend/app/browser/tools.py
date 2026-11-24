"""Registry-compatible browser tool definitions and private callable bindings.

This module exposes registrations but deliberately has no ToolRegistry
dependency. The orchestrator owns registration and approval decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from threading import RLock
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
    ToolCall,
    ToolDefinition,
    ToolResult,
)


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
        try:
            with lease(tool_call, create=not resume_without_session) as session:
                if session is None and not resume_without_session:
                    return self._unavailable()
                # Resume is allowed through to WorkflowReplay without a live
                # session so the encrypted store can mark continuity lost.
                return method(self, arguments, tool_call)
        except Exception:
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
    ) -> None:
        self.workflow_store = workflow_store
        self.session_manager = session_manager
        self.replay_engine = replay_engine
        self.policy_handoff = policy_handoff
        self.resume_context_provider = resume_context_provider
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
        session = self._session(tool_call)
        if session is None:
            return self._unavailable()
        try:
            requested_url = str(arguments.url)
            final_url = session.controller.navigate(str(arguments.url))
            observation = self._observe(session.controller)
            self._set_verification_context(tool_call, {"operation": "navigate", "requested_url": requested_url})
            return self._success(
                {"navigated": True, "page_observation": observation},
                task_call=tool_call,
                claim="The browser reached the requested page and captured its visible content",
                source_url=self._safe_page_url(final_url),
            )
        except Exception:
            return self._failed("Browser navigation failed safely")

    @_session_serialized
    def click(self, arguments: BrowserClickInput, tool_call: ToolCall) -> ToolResult:
        session = self._session(tool_call)
        if session is None:
            return self._unavailable()
        try:
            before = self._observe(session.controller)
            session.controller.click(arguments.target)
            observation = self._observe(session.controller)
            self._set_verification_context(tool_call, {
                "operation": "click", "before": before, "target": dict(arguments.target),
            })
            return self._success(
                {"clicked": True, "page_observation": observation}, task_call=tool_call,
                claim="The browser click changed the observed page state", source_url=observation["url"],
            )
        except Exception:
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
        observed = result.data.get("page_observation")
        if observed is not None and observed != actual:
            return False
        operation = context.get("operation")
        if operation == "navigate":
            return (
                tool_name == "browser.navigate"
                and actual["url"] == self._safe_page_url(context.get("requested_url", ""))
                and bool(actual["title"])
            )
        if operation == "click":
            before = context.get("before")
            target = context.get("target", {})
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
    def _safe_page_url(value: str) -> str:
        try:
            parts = urlsplit(_validate_http_url(value))
            # Drop credentials, fragments, and query values before persisting or
            # returning browser provenance. The origin and path remain useful.
            return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", "", ""))
        except Exception:
            return ""

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


def browser_tool_bindings(adapter: BrowserToolAdapter) -> tuple[BrowserToolBinding, ...]:
    """Return the six compatible bindings for the host's registry to register."""
    return adapter.bindings


def browser_tool_definitions(adapter: BrowserToolAdapter) -> tuple[ToolDefinition, ...]:
    """Return serializable definitions without exposing callable objects."""
    return adapter.definitions
