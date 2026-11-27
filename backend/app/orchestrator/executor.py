"""Single-action observe/act/verify loop with per-task serialization and approval gates."""
from __future__ import annotations

from contextlib import nullcontext
from threading import Condition, RLock
import logging
from time import monotonic
from typing import Callable
from uuid import UUID

from pydantic import UUID4

from backend.app.orchestrator.recovery import prepare_resume
from backend.app.orchestrator.execution_diagnostics import result_error_category, safe_tool_response_diagnostic
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.orchestrator.tool_registry import (
    ToolExecutionCancelled,
    ToolExecutionOutcomeUnknown,
    ToolInputError,
    ToolRegistry,
    ToolResponsePersistenceError,
)
from backend.app.schemas.contracts import (
    ApprovalRequest,
    ApprovalStatus,
    EventStatus,
    StepStatus,
    TaskResponse,
    TaskStatus,
    ToolCall,
    ToolResult,
)


Verifier = Callable[[str, str, ToolResult], bool]
NO_VERIFIER_ERROR = "No outcome verifier is configured; the tool was not executed"
logger = logging.getLogger(__name__)


def verify_result(tool_name: str, expected: str, result: ToolResult, verifier: Verifier) -> bool:
    """Verify an expected outcome independently from ToolResult.success."""
    return result.success and bool(verifier(tool_name, expected, result))


class TaskExecutor:
    def __init__(
        self,
        manager: TaskManager,
        registry: ToolRegistry,
        verifier: Verifier | None = None,
        *,
        browser_replay_bridge=None,
        browser_dispatcher=None,
        remote_browser_selected: bool = False,
        document_id_validator: Callable[[str], bool] | None = None,
    ) -> None:
        self.manager = manager
        self.registry = registry
        self.verifier = verifier
        self._tool_verifiers: dict[str, Verifier] = {}
        self.browser_replay_bridge = browser_replay_bridge
        self.browser_dispatcher = None
        self.remote_browser_selected = False
        self.document_id_validator = document_id_validator or (lambda _document_id: False)
        self._browser_dispatcher_callback_registered = False
        self._configuration_condition = Condition(RLock())
        self._active_runs = 0
        if browser_replay_bridge is not None:
            self.manager.register_terminal_callback(self._on_task_terminal)
        if browser_dispatcher is not None or remote_browser_selected:
            self.configure_browser_dispatcher(browser_dispatcher, remote_selected=remote_browser_selected)

    def configure_document_id_validator(self, validator: Callable[[str], bool]) -> None:
        if not callable(validator):
            raise TypeError("document ID validator must be callable")
        with self._configuration_condition:
            while self._active_runs:
                self._configuration_condition.wait()
            self.document_id_validator = validator

    def configure_verifier(self, tool_name: str, verifier: Verifier) -> None:
        """Install or replace the verifier used for one registered tool."""
        if not callable(verifier):
            raise TypeError("Verifier must be callable")
        self.registry.get_tool(tool_name)
        with self._configuration_condition:
            while self._active_runs:
                self._configuration_condition.wait()
            self._tool_verifiers[tool_name] = verifier

    def verifier_for(self, tool_name: str) -> Verifier | None:
        """Prefer a tool-specific verifier, falling back to the shared verifier."""
        with self._configuration_condition:
            return self._tool_verifiers.get(tool_name, self.verifier)

    def configure_browser_replay_bridge(self, bridge) -> None:
        """Attach the trusted replay bridge without replacing this executor."""
        if bridge is None or not callable(getattr(bridge, "resolve", None)):
            raise TypeError("A trusted browser replay bridge is required")
        with self._configuration_condition:
            while self._active_runs:
                self._configuration_condition.wait()
            self.browser_replay_bridge = bridge
            self.manager.register_terminal_callback(self._on_task_terminal)

    def configure_browser_dispatcher(self, dispatcher, *, remote_selected: bool = False) -> None:
        """Select the remote browser path explicitly, with no local fallback.

        Passing ``dispatcher=None, remote_selected=True`` deliberately leaves
        browser execution disabled while ensuring every browser tool is
        intercepted instead of reaching the local registry callable.
        """
        if not isinstance(remote_selected, bool):
            raise TypeError("remote browser selection must be a boolean")
        if dispatcher is not None and (
            not callable(getattr(dispatcher, "execute", None))
            or not callable(getattr(dispatcher, "verify_result", None))
            or not callable(getattr(dispatcher, "cleanup_task", None))
        ):
            raise TypeError("A complete remote browser dispatcher is required")
        with self._configuration_condition:
            while self._active_runs:
                self._configuration_condition.wait()
            self.browser_dispatcher = dispatcher
            self.remote_browser_selected = remote_selected
            if remote_selected and not self._browser_dispatcher_callback_registered:
                self.manager.register_terminal_callback(self._on_task_terminal)
                self._browser_dispatcher_callback_registered = True

    def _on_task_terminal(self, task_id, _status) -> None:
        bridge = self.browser_replay_bridge
        if bridge is not None:
            bridge.forget_task(task_id)
        dispatcher = self.browser_dispatcher
        if dispatcher is not None:
            try:
                dispatcher.cleanup_task(task_id)
            except Exception as exc:
                logger.warning("Remote browser cleanup failed: exception_class=%s", type(exc).__name__)

    def _get_task(self, task_id):
        getter = getattr(self.manager, "get_for_execution", None)
        return getter(task_id) if callable(getter) else self.manager.get(task_id)

    def _save_task(self, task):
        saver = getattr(self.manager, "save_for_execution", None)
        return saver(task) if callable(saver) else self.manager.save(task)

    def _emit_diagnostic_event(
        self,
        task_id,
        event_type: str,
        status: EventStatus,
        message: str,
        *,
        step_id: str | None = None,
        tool_name: str | None = None,
        duration_ms: int | None = None,
        result_count: int | None = None,
        error_category: str | None = None,
        diagnostic: dict | None = None,
        browser_session_id: str | None = None,
        provider_session_id: str | None = None,
        screenshot_reference: str | None = None,
    ) -> None:
        recorder = getattr(self.manager, "record_execution_event", None)
        if not callable(recorder):
            return
        recorder(
            task_id, event_type, status, message,
            step_id=step_id,
            tool_name=tool_name,
            duration_ms=duration_ms,
            result_count=result_count,
            error_category=error_category,
            diagnostic=diagnostic,
            browser_session_id=browser_session_id,
            provider_session_id=provider_session_id,
            screenshot_reference=screenshot_reference,
        )

    def _persist_tool_response_checkpoint(self, task, step, call, result: ToolResult) -> tuple[object, dict]:
        """Persist a safe result summary before verification or dependent steps."""
        diagnostic = safe_tool_response_diagnostic(step.tool_name, result, stage="normalized_response")
        diagnostic["error_category"] = result_error_category(step.tool_name, diagnostic, phase="result")
        checkpoint_started_at = monotonic()
        try:
            self.manager.record_tool_diagnostic(task.task_id, step.step_id, step.tool_name, diagnostic)
            step.verification = {
                "verified": None,
                "method": "awaiting_outcome_verification",
                "diagnostic": diagnostic,
                "tool_result_persisted": True,
            }
            task = self._save_task(task)
        except Exception as exc:
            logger.error(
                "Tool response checkpoint failed: task_id=%s step_id=%s tool_name=%s exception_class=%s",
                task.task_id, step.step_id, step.tool_name, type(exc).__name__,
            )
            raise ToolExecutionOutcomeUnknown(
                "Tool returned a response, but its result could not be persisted; the external outcome is unknown"
            ) from None

        try:
            self._emit_diagnostic_event(
                task.task_id,
                "tool.response_persisted",
                EventStatus.RUNNING,
                "A sanitized tool outcome checkpoint was persisted before verification",
                step_id=step.step_id,
                tool_name=step.tool_name,
                duration_ms=max(0, int((monotonic() - checkpoint_started_at) * 1000)),
                result_count=diagnostic.get("result_count"),
                error_category=diagnostic.get("error_category"),
                diagnostic=diagnostic,
                screenshot_reference=diagnostic.get("screenshot_reference"),
            )
        except Exception:
            raise ToolExecutionOutcomeUnknown(
                "Tool response checkpoint was saved, but its execution event could not be persisted; review is required"
            ) from None
        return task, diagnostic

    def _bind_document_dependency_outputs(self, task, step) -> tuple[object, str | None]:
        """Bind verified, persisted document IDs from declared predecessor steps."""
        document_producers = {
            "document.upload", "document.search", "document.read_local_file", "document.analyze_uploaded",
        }
        consumer = step.tool_name in {"context.retrieve", "context.relate"}
        if not consumer:
            return task, None
        dependencies = [item for item in task.plan if item.step_id in step.depends_on and item.tool_name in document_producers]
        candidate_ids: set[str] = set()
        for dependency in dependencies:
            if dependency.status != StepStatus.COMPLETED or not isinstance(dependency.verification, dict) or dependency.verification.get("verified") is not True:
                return task, f"Step '{step.step_id}' requires verified output from '{dependency.step_id}' before it can run."
            diagnostic = self.manager.tool_diagnostic(task.task_id, dependency.step_id)
            if diagnostic is None:
                return task, f"Step '{step.step_id}' has no persisted result checkpoint for '{dependency.step_id}'."
            candidate_ids.update(item for item in diagnostic.get("document_ids", []) if isinstance(item, str))

        supplied = step.input.get("document_ids")
        if dependencies:
            if not candidate_ids:
                return task, f"Step '{step.step_id}' cannot run because its verified predecessor produced no document IDs."
            if supplied is not None and (
                not isinstance(supplied, list)
                or any(not isinstance(item, str) for item in supplied)
            ):
                return task, f"Step '{step.step_id}' document IDs do not match its verified predecessor results."
            selected = candidate_ids if supplied is None else set(supplied)
            if not selected or not selected.issubset(candidate_ids):
                return task, f"Step '{step.step_id}' document IDs do not match its verified predecessor results."
        elif supplied is None:
            return task, None
        else:
            if not isinstance(supplied, list) or any(not isinstance(item, str) for item in supplied):
                return task, f"Step '{step.step_id}' document IDs are invalid."
            selected = set(supplied)
            if not selected:
                return task, f"Step '{step.step_id}' document IDs are invalid."

        valid_ids: set[str] = set()
        for document_id in selected:
            try:
                is_persisted = self.document_id_validator(document_id)
            except Exception:
                is_persisted = False
            if not is_persisted:
                return task, f"Step '{step.step_id}' references a document ID that is not present in the document store."
            valid_ids.add(document_id)
        if not valid_ids:
            return task, f"Step '{step.step_id}' requires at least one persisted document ID."
        if step.input.get("document_ids") != sorted(valid_ids):
            step.input["document_ids"] = sorted(valid_ids)
            persistence_started_at = monotonic()
            task = self._save_task(task)
            self._emit_diagnostic_event(
                task.task_id,
                "dependency.document_ids_bound",
                EventStatus.RUNNING,
                "Verified predecessor document IDs were persisted before dependent search",
                step_id=step.step_id,
                tool_name=step.tool_name,
                duration_ms=max(0, int((monotonic() - persistence_started_at) * 1000)),
                diagnostic={"document_ids": sorted(valid_ids), "document_id_count": len(valid_ids)},
            )
        return task, None

    def run(self, task_id: UUID4 | UUID | str, *, resume: bool = False) -> TaskResponse:
        # A second caller waits for the current run and then sees its persisted result.
        # Cancellation deliberately does not acquire this lock.
        with self._configuration_condition:
            self._active_runs += 1
        try:
            with self.manager.execution_lock(task_id):
                self._run_locked(task_id, resume=resume)
                return self.manager.get(task_id)
        finally:
            with self._configuration_condition:
                self._active_runs -= 1
                if self._active_runs == 0:
                    self._configuration_condition.notify_all()

    def _run_locked(self, task_id: UUID4 | UUID | str, *, resume: bool) -> TaskResponse:
        task = self._get_task(task_id)
        if task.status in {TaskStatus.CANCELLED, TaskStatus.COMPLETED}:
            return task
        if task.status == TaskStatus.NEEDS_ATTENTION:
            pending_step = next((step for step in task.plan if step.status not in {StepStatus.COMPLETED, StepStatus.SKIPPED}), None)
            if task.error != NO_VERIFIER_ERROR or pending_step is None or self.verifier_for(pending_step.tool_name) is None:
                return task
            task.status = TaskStatus.EXECUTING
            task.error = None
            task = self._save_task(task)
        if task.status == TaskStatus.FAILED and not resume:
            return task
        if resume:
            task = self._save_task(prepare_resume(task))
            if task.status != TaskStatus.EXECUTING:
                return task

        for step in task.plan:
            latest = self._get_task(task_id)
            if latest.status == TaskStatus.CANCELLED:
                return latest
            step = next((item for item in latest.plan if item.step_id == step.step_id), step)
            task = latest
            if step.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}:
                continue

            # Older or malformed persisted plans may have serialized the
            # complete orchestration step inside its `input` object. Normalize
            # only the known step-metadata keys before dependency and approval
            # checks. All remaining arguments are still validated strictly
            # against the registered tool schema below.
            registered_names = {tool.tool_name for tool in self.registry.list_tools()}
            if step.tool_name in registered_names:
                try:
                    metadata_moved = self.registry.normalize_step_metadata(step)
                except ToolInputError as exc:
                    return self._fail_step(
                        task_id, task, step, str(exc),
                        error_category="step_metadata_validation_failure",
                    )
                if metadata_moved:
                    task = self._save_task(task)
                    step = next(item for item in task.plan if item.step_id == step.step_id)
                    latest = task

            step_statuses = {item.step_id: item.status for item in task.plan}
            unmet_dependencies = [
                dependency for dependency in step.depends_on
                if step_statuses.get(dependency) != StepStatus.COMPLETED
            ]
            if unmet_dependencies:
                step.status = StepStatus.SKIPPED
                step.error = f"Prerequisite step(s) not completed: {', '.join(unmet_dependencies)}"
                task.status = TaskStatus.NEEDS_ATTENTION
                task.error = step.error
                task.result = synthesize_result(task, self.manager.missing_information(task_id))
                self.manager.event(task, "step.dependency_unmet", EventStatus.NEEDS_ATTENTION, step.error, step.step_id)
                return self._save_task(task)

            task, dependency_error = self._bind_document_dependency_outputs(task, step)
            if dependency_error:
                step = next(item for item in task.plan if item.step_id == step.step_id)
                step.verification = {
                    "verified": False,
                    "method": "persisted_dependency_output_check",
                    "error_category": "dependency_result_unavailable",
                }
                return self._mark_needs_attention(
                    task_id,
                    task,
                    step,
                    dependency_error,
                    "step.dependency_result_unavailable",
                    error_category="dependency_result_unavailable",
                )

            task.current_step = step.step_id
            remote_browser = self._remote_browser_step(step.tool_name)
            step_verifier = self.verifier_for(step.tool_name)
            if step_verifier is None and not remote_browser:
                task.status = TaskStatus.NEEDS_ATTENTION
                task.error = NO_VERIFIER_ERROR
                self.manager.event(task, "verification.unavailable", EventStatus.NEEDS_ATTENTION, task.error, step.step_id)
                return self._save_task(task)

            task.status = TaskStatus.EXECUTING
            task = self._save_task(task)
            if task.status == TaskStatus.CANCELLED:
                return task

            tool_started_at = monotonic()
            try:
                self.registry.get_tool(step.tool_name)
                self.registry.validate_input(step.tool_name, step.input)
                replay_pending = None
                if not remote_browser and step.tool_name == "browser.replay" and self.browser_replay_bridge is not None:
                    replay_pending = self.browser_replay_bridge.pending_for_task(task.task_id)
                replay_continuation = replay_pending is not None
                if replay_continuation:
                    call = self.browser_replay_bridge.recover_parent_tool_call(replay_pending[1])
                    if call.step_id != step.step_id or call.arguments != step.input:
                        return self._mark_replay_attention(task_id, step.step_id, "Trusted browser replay does not match the planned step")
                else:
                    if step.tool_name == "browser.replay":
                        prior_start = any(
                            prior.task_id == task.task_id
                            and prior.step_id == step.step_id
                            and prior.tool_name == "browser.replay"
                            and prior.arguments.get("operation") == "start"
                            for prior in self.manager.tool_calls(task.task_id)
                        )
                        if prior_start:
                            return self._mark_replay_attention(
                                task_id,
                                step.step_id,
                                "Trusted replay continuation state is unavailable; start a new task after reviewing the browser outcome",
                            )
                    call = ToolCall(task_id=task.task_id, step_id=step.step_id, tool_name=step.tool_name, arguments=step.input)
                needs_approval = remote_browser or (False if replay_continuation else (
                    self.registry.action_requires_approval(call, step.description) or step.requires_approval
                ))

                approval = None
                if needs_approval:
                    approval = self.manager.approved_request(call)
                    if approval is None:
                        step.status = StepStatus.AWAITING_APPROVAL
                        task.status = TaskStatus.AWAITING_APPROVAL
                        pending = next((a for a in task.pending_approvals if a.step_id == step.step_id and a.status == ApprovalStatus.PENDING), None)
                        if pending is None:
                            task.pending_approvals.append(ApprovalRequest(
                                task_id=task.task_id,
                                step_id=step.step_id,
                                tool_name=step.tool_name,
                                description=step.description,
                                arguments=step.input,
                            ))
                        self.manager.event(task, "approval.required", EventStatus.AWAITING_APPROVAL, "Approval required before this action", step.step_id)
                        return self._save_task(task)

                if remote_browser:
                    result = self._execute_remote_browser_tool(task_id, step, call, approval)
                elif call.tool_name == "browser.replay":
                    replay_result = self._execute_browser_replay(
                        task_id, task, step, call, approval,
                        continuation=replay_continuation,
                    )
                    if isinstance(replay_result, TaskResponse):
                        return replay_result
                    result = replay_result
                    if not result.success:
                        latest = self._get_task(task_id)
                        step = next(item for item in latest.plan if item.step_id == call.step_id)
                        return self._fail_step(task_id, latest, step, result.error or "Browser replay failed safely")
                    if not isinstance(result.data, dict) or result.data.get("status") != "completed":
                        return self._mark_replay_attention(task_id, call.step_id, "Browser replay did not reach a verified terminal state")
                else:
                    if call.tool_name == "document.search":
                        self._emit_diagnostic_event(
                            task_id,
                            "search.submission_started",
                            EventStatus.RUNNING,
                            "Submitting the validated query to indexed document search",
                            step_id=step.step_id,
                            tool_name=step.tool_name,
                        )
                    result = self._execute_registered_tool(task_id, step, call, approval)
            except ToolExecutionCancelled:
                latest = self._get_task(task_id)
                if latest.status == TaskStatus.CANCELLED:
                    return latest
                return self._fail_step(task_id, task, step, "Could not claim the planned tool action")
            except ToolResponsePersistenceError:
                latest = self._get_task(task_id)
                latest_step = next(item for item in latest.plan if item.step_id == step.step_id)
                latest_step.verification = {
                    "verified": None,
                    "method": "tool_response_checkpoint",
                    "error_category": "persistence_failure",
                }
                self._emit_diagnostic_event(
                    task_id, "task.persistence_failed", EventStatus.NEEDS_ATTENTION,
                    "The tool returned, but its diagnostic response checkpoint could not be persisted",
                    step_id=latest_step.step_id, tool_name=latest_step.tool_name,
                    duration_ms=max(0, int((monotonic() - tool_started_at) * 1000)),
                    error_category="persistence_failure",
                )
                return self._mark_needs_attention(
                    task_id, latest, latest_step,
                    "The tool returned, but its response checkpoint could not be persisted; review is required",
                    "tool.result_persistence_failed", error_category="persistence_failure",
                )
            except ToolExecutionOutcomeUnknown as exc:
                latest = self._get_task(task_id)
                latest_step = next(item for item in latest.plan if item.step_id == step.step_id)
                category = "search_submission_failure" if step.tool_name == "document.search" else "unknown_external_outcome"
                latest_step.verification = {
                    "verified": None,
                    "method": "tool_invocation",
                    "error_category": category,
                }
                if step.tool_name == "document.search":
                    self._emit_diagnostic_event(
                        task_id, "search.submission_failed", EventStatus.NEEDS_ATTENTION,
                        "Search did not return a usable response; no dependent step was run",
                        step_id=step.step_id, tool_name=step.tool_name,
                        duration_ms=max(0, int((monotonic() - tool_started_at) * 1000)),
                        error_category=category,
                    )
                else:
                    self._emit_diagnostic_event(
                        task_id, "tool.outcome_unknown", EventStatus.NEEDS_ATTENTION,
                        "The tool call ended without a confirmed outcome",
                        step_id=step.step_id, tool_name=step.tool_name,
                        duration_ms=max(0, int((monotonic() - tool_started_at) * 1000)),
                        error_category=category,
                    )
                if step.tool_name == "browser.replay":
                    return self._mark_needs_attention(task_id, latest, latest_step, "Browser action outcome is unknown; replay will not be retried", "replay.outcome_unknown", error_category=category)
                message = "Search submission failed before a result was confirmed" if step.tool_name == "document.search" else str(exc)
                return self._mark_needs_attention(task_id, latest, latest_step, message, "tool.outcome_unknown", error_category=category)
            except ToolInputError as exc:
                latest = self._get_task(task_id)
                latest_step = next(item for item in latest.plan if item.step_id == step.step_id)
                return self._fail_step(
                    task_id,
                    latest,
                    latest_step,
                    str(exc),
                    error_category="tool_input_validation_failure",
                )
            except Exception as exc:
                latest = self._get_task(task_id)
                if latest.status == TaskStatus.CANCELLED:
                    return latest
                category = "search_submission_failure" if step.tool_name == "document.search" else "tool_execution_failure"
                safe_message = "Search submission failed" if step.tool_name == "document.search" else f"Tool execution failed ({type(exc).__name__})"
                return self._fail_step(task_id, latest, step, safe_message, error_category=category)

            task = self._get_task(task_id)
            step = next(item for item in task.plan if item.step_id == call.step_id)
            duration_ms = max(0, int((monotonic() - tool_started_at) * 1000))
            try:
                task, response_diagnostic = self._persist_tool_response_checkpoint(task, step, call, result)
            except ToolExecutionOutcomeUnknown:
                latest = self._get_task(task_id)
                latest_step = next(item for item in latest.plan if item.step_id == call.step_id)
                latest_step.verification = {
                    "verified": None,
                    "method": "normalized_response_checkpoint",
                    "error_category": "persistence_failure",
                }
                self._emit_diagnostic_event(
                    task_id, "task.persistence_failed", EventStatus.NEEDS_ATTENTION,
                    "The tool returned, but its diagnostic checkpoint could not be saved",
                    step_id=latest_step.step_id, tool_name=latest_step.tool_name,
                    duration_ms=duration_ms, error_category="persistence_failure",
                )
                return self._mark_needs_attention(
                    task_id, latest, latest_step,
                    "The tool returned, but its outcome checkpoint could not be persisted; review is required",
                    "tool.result_persistence_failed", error_category="persistence_failure",
                )
            step = next(item for item in task.plan if item.step_id == call.step_id)

            search_shape_valid = None
            if step.tool_name == "document.search":
                result_data = result.data if isinstance(result.data, dict) else {}
                raw_results = result_data.get("results")
                search_shape_valid = (
                    result.success is True
                    and isinstance(raw_results, list)
                    and result_data.get("total") == len(raw_results)
                )
                if search_shape_valid:
                    self._emit_diagnostic_event(
                        task_id, "search.results_extracted", EventStatus.COMPLETED,
                        "Search response was normalized into persisted result records",
                        step_id=step.step_id, tool_name=step.tool_name,
                        duration_ms=duration_ms,
                        result_count=response_diagnostic.get("result_count"),
                        error_category="search_no_results" if response_diagnostic.get("result_count") == 0 else None,
                        diagnostic={
                            "document_ids": response_diagnostic.get("document_ids", []),
                            "result_summaries": response_diagnostic.get("results", []),
                        },
                    )
                else:
                    self._emit_diagnostic_event(
                        task_id, "search.extraction_failed", EventStatus.FAILED,
                        "Search returned data that could not be normalized as result records",
                        step_id=step.step_id, tool_name=step.tool_name,
                        duration_ms=duration_ms,
                        error_category="search_extraction_failure",
                        diagnostic=response_diagnostic,
                    )
            # Preserve provenance even when tool execution or verification fails. Retaining
            # evidence does not certify that the requested outcome occurred.
            evidence_ids = []
            for evidence in result.evidence:
                if evidence.task_id != task.task_id:
                    return self._fail_step(task_id, task, step, "Tool evidence task_id does not match the executing task")
                if not any(existing.evidence_id == evidence.evidence_id for existing in task.evidence):
                    task.evidence.append(evidence)
                evidence_ids.append(evidence.evidence_id)
            if evidence_ids:
                self.manager.event(
                    task, "tool.evidence_received", EventStatus.RUNNING,
                    "Tool evidence retained; outcome verification is still pending", step.step_id, evidence_ids,
                )
                task = self._save_task(task)
                # Saving returns a fresh TaskResponse snapshot. Rebind the step
                # before verification so subsequent status changes are applied
                # to the plan that will be persisted, not the stale pre-save
                # TaskStep instance.
                step = next(item for item in task.plan if item.step_id == call.step_id)

            if not result.success:
                # Keep only allowlisted diagnostic fields for the local-file
                # adapter so users can distinguish extraction and analysis
                # failures without putting document contents or paths in task state.
                if step.tool_name == "document.read_local_file" and isinstance(result.data, dict):
                    safe_keys = {
                        "failure_code", "extraction_status", "analysis_status", "document_id",
                        "evidence_validation_category", "failure_stage", "analysis_failure_detail",
                        "metadata_mismatch_fields",
                    }
                    details = {key: result.data[key] for key in safe_keys if key in result.data}
                    step.verification = {
                        "verified": False,
                        "method": "local_file_operation_failed",
                        "details": details,
                    }
                if result.error in {
                    "Tool returned an invalid result",
                    "Tool returned data that is not JSON serializable",
                }:
                    return self._mark_needs_attention(
                        task_id,
                        task,
                        step,
                        "Tool returned unusable data after invocation; its external outcome is unknown",
                        "tool.outcome_unknown",
                        error_category="result_normalization_failure",
                    )
                error_category = "search_extraction_failure" if step.tool_name == "document.search" else "tool_reported_failure"
                if step.tool_name == "document.search":
                    self._emit_diagnostic_event(
                        task_id, "search.extraction_failed", EventStatus.FAILED,
                        "Search returned an unsuccessful result and dependent steps were stopped",
                        step_id=step.step_id, tool_name=step.tool_name,
                        duration_ms=duration_ms, error_category=error_category,
                        diagnostic=response_diagnostic,
                    )
                return self._fail_step(
                    task_id,
                    task,
                    step,
                    result.error or f"Tool '{step.tool_name}' reported failure",
                    error_category=error_category,
                )

            task.status = TaskStatus.VERIFYING
            verification_started_at = monotonic()
            self.manager.event(
                task, "verification.started", EventStatus.VERIFYING,
                "Checking expected outcome", step.step_id, evidence_ids,
                tool_name=step.tool_name,
                result_count=response_diagnostic.get("result_count"),
                screenshot_reference=response_diagnostic.get("screenshot_reference"),
                diagnostic={"method": "independent_worker_verifier" if remote_browser else "injected_verifier"},
            )
            try:
                if remote_browser:
                    dispatcher = self.browser_dispatcher
                    verified = bool(
                        dispatcher is not None
                        and dispatcher.verify_result(step.tool_name, step.expected_outcome, result)
                    )
                else:
                    verified = verify_result(step.tool_name, step.expected_outcome, result, step_verifier)
            except Exception as exc:
                logger.warning("Outcome verifier failed: tool_name=%s exception_class=%s", step.tool_name, type(exc).__name__)
                step.verification = {
                    "verified": False,
                    "expected_outcome": step.expected_outcome,
                    "error": f"Verifier failed ({type(exc).__name__})",
                    "diagnostic": response_diagnostic,
                }
                return self._mark_needs_attention(
                    task_id,
                    task,
                    step,
                    f"Outcome verification failed ({type(exc).__name__}); the external outcome is unknown",
                    "verification.failed",
                    error_category="verification_failure",
                )
            step.verification = {
                "verified": verified,
                "expected_outcome": step.expected_outcome,
                "method": "independent_worker_verifier" if remote_browser else "injected_verifier",
                "evidence_ids": [str(item) for item in evidence_ids],
                "diagnostic": response_diagnostic,
                "tool_result_persisted": True,
            }
            if not verified:
                if step.tool_name == "document.search" and response_diagnostic.get("success") is True and response_diagnostic.get("result_count") == 0:
                    message = "Search completed but found no matching indexed documents; dependent steps were not run."
                    return self._mark_needs_attention(
                        task_id, task, step, message, "search.completed_empty",
                        error_category="search_no_results",
                    )
                if step.tool_name == "document.search" and search_shape_valid is False:
                    message = "Search returned data that failed result extraction checks; dependent steps were not run."
                    return self._mark_needs_attention(
                        task_id, task, step, message, "search.extraction_blocked",
                        error_category="search_extraction_failure",
                    )
                return self._mark_needs_attention(
                    task_id,
                    task,
                    step,
                    "Tool returned, but the expected outcome could not be verified; the external outcome is unknown",
                    "verification.failed",
                    error_category="verification_failure",
                )

            # Keep full tool output available to downstream verified steps only.
            # Failed verification retains the private, content-minimized
            # checkpoint and evidence references for diagnosis without echoing
            # potentially sensitive provider content through task APIs.
            step.verification["tool_result"] = result.data
            step.status = StepStatus.COMPLETED
            step.error = None
            task.completed_steps = list(dict.fromkeys([*task.completed_steps, step.step_id]))
            task.current_step = None
            task.status = TaskStatus.EXECUTING
            self.manager.event(
                task, "step.verified", EventStatus.COMPLETED,
                "Expected outcome verified", step.step_id, evidence_ids,
                tool_name=step.tool_name,
                duration_ms=max(0, int((monotonic() - verification_started_at) * 1000)),
                result_count=response_diagnostic.get("result_count"),
                screenshot_reference=response_diagnostic.get("screenshot_reference"),
                diagnostic={"verification": "passed"},
            )
            persistence_started_at = monotonic()
            task = self._save_task(task)
            self._emit_diagnostic_event(
                task.task_id, "task.persistence_completed", EventStatus.COMPLETED,
                "Verified step state was committed to task storage",
                step_id=step.step_id, tool_name=step.tool_name,
                duration_ms=max(0, int((monotonic() - persistence_started_at) * 1000)),
                result_count=response_diagnostic.get("result_count"),
                screenshot_reference=response_diagnostic.get("screenshot_reference"),
                diagnostic={"commit": "task_state_saved", "verified": True},
            )
            if task.status == TaskStatus.CANCELLED:
                return task

        task.current_step = None
        task.status = TaskStatus.COMPLETED
        task.error = None
        task.result = synthesize_result(task, self.manager.missing_information(task_id))
        self.manager.event(task, "task.completed", EventStatus.COMPLETED, "All planned steps completed and verified")
        return self._save_task(task)

    def _fail_step(
        self,
        task_id,
        task: TaskResponse,
        step,
        message: str,
        *,
        event_type: str = "step.failed",
        error_category: str | None = None,
    ) -> TaskResponse:
        step.status = StepStatus.FAILED
        step.error = message
        task.status = TaskStatus.FAILED
        task.error = message
        task.result = synthesize_result(task, self.manager.missing_information(task_id))
        self.manager.event(
            task, event_type, EventStatus.FAILED, message, step.step_id,
            tool_name=step.tool_name,
            error_category=error_category,
            diagnostic=step.verification.get("diagnostic") if isinstance(step.verification, dict) else None,
        )
        return self._save_task(task)

    def _mark_needs_attention(
        self,
        task_id,
        task: TaskResponse,
        step,
        message: str,
        event_type: str,
        *,
        error_category: str | None = None,
    ) -> TaskResponse:
        """Record an unresolved post-call outcome without enabling automatic replay."""
        verification = step.verification
        latest = self._get_task(task_id)
        step = next(item for item in latest.plan if item.step_id == step.step_id)
        # The caller may have just attached verifier diagnostics locally; keep
        # those while merging against the latest cancellation/task state.
        if verification is not None:
            step.verification = verification
        step.status = StepStatus.FAILED
        step.error = message
        task = latest
        task.current_step = step.step_id
        task.error = message
        if task.status != TaskStatus.CANCELLED:
            task.status = TaskStatus.NEEDS_ATTENTION
        task.result = synthesize_result(task, self.manager.missing_information(task_id))
        self.manager.event(
            task, event_type, EventStatus.NEEDS_ATTENTION, message, step.step_id,
            tool_name=step.tool_name,
            error_category=error_category,
            diagnostic=step.verification.get("diagnostic") if isinstance(step.verification, dict) else None,
        )
        return self._save_task(task)

    def _execute_registered_tool(self, task_id, step, call, approval):
        """Run a normal registered action, atomically claiming it against cancel."""
        def persist_raw_response(raw_response):
            diagnostic = safe_tool_response_diagnostic(step.tool_name, raw_response, stage="raw_response")
            diagnostic["error_category"] = result_error_category(step.tool_name, diagnostic, phase="result")
            persistence_started_at = monotonic()
            self.manager.record_tool_diagnostic(task_id, step.step_id, step.tool_name, diagnostic)
            self._emit_diagnostic_event(
                task_id,
                "tool.raw_response_persisted",
                EventStatus.RUNNING,
                "A sanitized raw tool response checkpoint was persisted",
                step_id=step.step_id,
                tool_name=step.tool_name,
                duration_ms=max(0, int((monotonic() - persistence_started_at) * 1000)),
                result_count=diagnostic.get("result_count"),
                error_category=diagnostic.get("error_category"),
                diagnostic=diagnostic,
                screenshot_reference=diagnostic.get("screenshot_reference"),
            )

        with self.manager.replay_dispatch_lock(task_id) if step.tool_name == "browser.replay" else nullcontext():
            return self.registry.execute(
                call,
                description=step.description,
                approval=approval,
                requires_approval=step.requires_approval,
                before_execute=lambda: self.manager.claim_tool_call(task_id, call, approval),
                on_raw_response=persist_raw_response,
            )

    def _remote_browser_step(self, tool_name: str) -> bool:
        return self.remote_browser_selected and tool_name.startswith("browser.")

    def _execute_remote_browser_tool(self, task_id, step, call, approval):
        """Dispatch a browser step remotely, never through local Chromium."""
        dispatcher = self.browser_dispatcher
        if dispatcher is None:
            return ToolResult(
                success=False,
                data={"failure_code": "worker_unavailable"},
                error="Remote browser execution is selected, but no worker is configured.",
            )
        if approval is None:
            return ToolResult(
                success=False,
                data={"failure_code": "approval_required"},
                error="An exact approved browser action is required for remote execution.",
            )
        with self.manager.replay_dispatch_lock(task_id) if step.tool_name == "browser.replay" else nullcontext():
            return dispatcher.execute(
                call,
                approval,
                before_execute=lambda: self.manager.claim_tool_call(task_id, call, approval),
            )

    def _execute_browser_replay(self, task_id, task, step, parent_call, approval, *, continuation: bool):
        """Start a replay checkpoint or dispatch exactly one trusted continuation."""
        bridge = self.browser_replay_bridge
        if bridge is None:
            return self._mark_replay_attention(task_id, step.step_id, "Trusted browser replay integration is unavailable")

        if not continuation:
            # Starting replay only prepares and persists its first pending action.
            # It never dispatches that action in the same Run plan request.
            result = self._execute_registered_tool(task_id, step, parent_call, approval)
            if not result.success:
                return result
            status = result.data.get("status") if isinstance(result.data, dict) else None
            if status == "pending_action":
                return self._pause_replay_for_explicit_resume(task_id, step.step_id)
            if status in {"needs_attention", "failed"}:
                return self._mark_replay_attention(task_id, step.step_id, "Browser replay preparation did not produce a resumable checkpoint")
            return result

        pending = bridge.pending_for_task(task_id)
        if pending is None:
            return self._mark_replay_attention(task_id, step.step_id, "Trusted replay checkpoint is unavailable; no browser action was retried")
        pending_action, policy_context = pending
        authorization = bridge.issue_authorization(pending_action, policy_context)
        if authorization is None:
            latest = self._get_task(task_id)
            if latest.status in {TaskStatus.AWAITING_APPROVAL, TaskStatus.CANCELLED}:
                return latest
            return self._mark_replay_attention(task_id, step.step_id, "Replay authorization could not be established; no browser action was dispatched")

        resume_call = parent_call.model_copy(update={"arguments": {"operation": "resume"}})
        registry_approval = self.manager.replay_resume_approval(resume_call, authorization)
        if registry_approval is None:
            return self._mark_replay_attention(task_id, step.step_id, "One-use replay authorization is unavailable; no browser action was dispatched")

        try:
            # cancel() takes this same lock. The lock covers the final cancellation
            # check, one-use claim, browser side effect, and ReplayStateStore journal.
            with self.manager.replay_dispatch_lock(task_id):
                if self.manager.is_cancelled(task_id):
                    return self._get_task(task_id)
                result = self.registry.execute(
                    resume_call,
                    description=step.description,
                    approval=registry_approval,
                    requires_approval=True,
                    before_execute=lambda: self.manager.claim_replay_resume(
                        resume_call, authorization, registry_approval
                    ),
                )
        except ToolExecutionOutcomeUnknown:
            raise
        except ToolExecutionCancelled:
            return self._get_task(task_id)

        status = result.data.get("status") if isinstance(result.data, dict) else None
        if status == "pending_action" and result.success:
            return self._pause_replay_for_explicit_resume(task_id, step.step_id)
        if status == "completed" and result.success:
            bridge.forget_replay(task_id, pending_action.replay_run_id)
            return result
        # A replay result that is failed or uncertain is terminal for this attempt.
        # The action journal decides recovery; the executor never dispatches it again.
        return self._mark_replay_attention(
            task_id,
            step.step_id,
            "Browser replay did not verify a safe completed checkpoint; review is required and the action will not be retried",
        )

    def _pause_replay_for_explicit_resume(self, task_id, step_id: str) -> TaskResponse:
        task = self._get_task(task_id)
        if task.status == TaskStatus.CANCELLED:
            return task
        step = next((item for item in task.plan if item.step_id == step_id), None)
        if step is None:
            return self._mark_replay_attention(task_id, step_id, "Browser replay step is unavailable")
        step.status = StepStatus.PENDING
        step.error = None
        task.current_step = None
        task.status = TaskStatus.CREATED
        task.error = None
        self.manager.event(
            task,
            "browser.replay.action_checkpointed",
            EventStatus.CREATED,
            "Browser replay checkpoint saved; explicitly run the plan to continue",
            step_id,
        )
        return self._save_task(task)

    def _mark_replay_attention(self, task_id, step_id: str, message: str) -> TaskResponse:
        task = self._get_task(task_id)
        step = next((item for item in task.plan if item.step_id == step_id), None)
        if step is None:
            return task
        return self._mark_needs_attention(task_id, task, step, message, "browser.replay.needs_attention")


def synthesize_result(task: TaskResponse, missing_information: list[str] | None = None) -> dict:
    """Summarize only verified work and retain evidence references and limitations."""
    completed = [step for step in task.plan if step.status == StepStatus.COMPLETED and step.verification and step.verification.get("verified") is True]
    return {
        "requested": task.user_request,
        # A description may describe a planned gate (for example, asking
        # permission). Once verified, report the expected outcome as the
        # completed action instead of repeating that stale pre-execution text.
        "actions_completed": [step.expected_outcome for step in completed],
        "verified": [
            {
                "step_id": step.step_id,
                "outcome": step.expected_outcome,
                "verification": step.verification,
                "evidence": step.verification.get("evidence_ids", []),
            }
            for step in completed
        ],
        "failed_or_incomplete": [step.error or step.description for step in task.plan if step.status not in {StepStatus.COMPLETED, StepStatus.SKIPPED}],
        "skipped_steps": [step.step_id for step in task.plan if step.status == StepStatus.SKIPPED],
        "assumptions_or_missing_information": missing_information or [],
    }
