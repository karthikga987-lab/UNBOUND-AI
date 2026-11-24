"""Single-action observe/act/verify loop with per-task serialization and approval gates."""
from __future__ import annotations

from contextlib import nullcontext
from threading import Condition, RLock
import logging
from typing import Callable
from uuid import UUID

from pydantic import UUID4

from backend.app.orchestrator.recovery import prepare_resume
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.orchestrator.tool_registry import ToolExecutionCancelled, ToolExecutionOutcomeUnknown, ToolRegistry
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
    ) -> None:
        self.manager = manager
        self.registry = registry
        self.verifier = verifier
        self._tool_verifiers: dict[str, Verifier] = {}
        self.browser_replay_bridge = browser_replay_bridge
        self._configuration_condition = Condition(RLock())
        self._active_runs = 0
        if browser_replay_bridge is not None:
            self.manager.register_terminal_callback(self._on_task_terminal)

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

    def _on_task_terminal(self, task_id, _status) -> None:
        bridge = self.browser_replay_bridge
        if bridge is not None:
            bridge.forget_task(task_id)

    def _get_task(self, task_id):
        getter = getattr(self.manager, "get_for_execution", None)
        return getter(task_id) if callable(getter) else self.manager.get(task_id)

    def _save_task(self, task):
        saver = getattr(self.manager, "save_for_execution", None)
        return saver(task) if callable(saver) else self.manager.save(task)

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
            if step.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}:
                continue

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

            task.current_step = step.step_id
            step_verifier = self.verifier_for(step.tool_name)
            if step_verifier is None:
                task.status = TaskStatus.NEEDS_ATTENTION
                task.error = NO_VERIFIER_ERROR
                self.manager.event(task, "verification.unavailable", EventStatus.NEEDS_ATTENTION, task.error, step.step_id)
                return self._save_task(task)

            task.status = TaskStatus.EXECUTING
            task = self._save_task(task)
            if task.status == TaskStatus.CANCELLED:
                return task

            try:
                self.registry.get_tool(step.tool_name)
                self.registry.validate_input(step.tool_name, step.input)
                replay_pending = None
                if step.tool_name == "browser.replay" and self.browser_replay_bridge is not None:
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
                needs_approval = False if replay_continuation else (
                    self.registry.action_requires_approval(call, step.description) or step.requires_approval
                )

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

                if call.tool_name == "browser.replay":
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
                    result = self._execute_registered_tool(task_id, step, call, approval)
            except ToolExecutionCancelled:
                latest = self._get_task(task_id)
                if latest.status == TaskStatus.CANCELLED:
                    return latest
                return self._fail_step(task_id, task, step, "Could not claim the planned tool action")
            except ToolExecutionOutcomeUnknown as exc:
                latest = self._get_task(task_id)
                if step.tool_name == "browser.replay":
                    return self._mark_needs_attention(task_id, latest, step, "Browser action outcome is unknown; replay will not be retried", "replay.outcome_unknown")
                return self._mark_needs_attention(task_id, latest, step, str(exc), "tool.outcome_unknown")
            except Exception as exc:
                latest = self._get_task(task_id)
                if latest.status == TaskStatus.CANCELLED:
                    return latest
                return self._fail_step(task_id, latest, step, str(exc))

            task = self._get_task(task_id)
            step = next(item for item in task.plan if item.step_id == call.step_id)
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
                    )
                return self._fail_step(
                    task_id,
                    task,
                    step,
                    result.error or f"Tool '{step.tool_name}' reported failure",
                )

            task.status = TaskStatus.VERIFYING
            self.manager.event(task, "verification.started", EventStatus.VERIFYING, "Checking expected outcome", step.step_id, evidence_ids)
            try:
                verified = verify_result(step.tool_name, step.expected_outcome, result, step_verifier)
            except Exception as exc:
                logger.warning("Outcome verifier failed: tool_name=%s exception_class=%s", step.tool_name, type(exc).__name__)
                step.verification = {"verified": False, "expected_outcome": step.expected_outcome, "error": f"Verifier failed ({type(exc).__name__})"}
                return self._mark_needs_attention(
                    task_id,
                    task,
                    step,
                    f"Outcome verification failed ({type(exc).__name__}); the external outcome is unknown",
                    "verification.failed",
                )
            step.verification = {
                "verified": verified,
                "expected_outcome": step.expected_outcome,
                "method": "injected_verifier",
                "tool_result": result.data,
                "evidence_ids": [str(item) for item in evidence_ids],
            }
            if not verified:
                return self._mark_needs_attention(
                    task_id,
                    task,
                    step,
                    "Tool returned, but the expected outcome could not be verified; the external outcome is unknown",
                    "verification.failed",
                )

            step.status = StepStatus.COMPLETED
            step.error = None
            task.completed_steps = list(dict.fromkeys([*task.completed_steps, step.step_id]))
            task.current_step = None
            task.status = TaskStatus.EXECUTING
            self.manager.event(task, "step.verified", EventStatus.COMPLETED, "Expected outcome verified", step.step_id, evidence_ids)
            task = self._save_task(task)
            if task.status == TaskStatus.CANCELLED:
                return task

        task.current_step = None
        task.status = TaskStatus.COMPLETED
        task.error = None
        task.result = synthesize_result(task, self.manager.missing_information(task_id))
        self.manager.event(task, "task.completed", EventStatus.COMPLETED, "All planned steps completed and verified")
        return self._save_task(task)

    def _fail_step(self, task_id, task: TaskResponse, step, message: str, *, event_type: str = "step.failed") -> TaskResponse:
        step.status = StepStatus.FAILED
        step.error = message
        task.status = TaskStatus.FAILED
        task.error = message
        task.result = synthesize_result(task, self.manager.missing_information(task_id))
        self.manager.event(task, event_type, EventStatus.FAILED, message, step.step_id)
        return self._save_task(task)

    def _mark_needs_attention(
        self,
        task_id,
        task: TaskResponse,
        step,
        message: str,
        event_type: str,
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
        self.manager.event(task, event_type, EventStatus.NEEDS_ATTENTION, message, step.step_id)
        return self._save_task(task)

    def _execute_registered_tool(self, task_id, step, call, approval):
        """Run a normal registered action, atomically claiming it against cancel."""
        with self.manager.replay_dispatch_lock(task_id) if step.tool_name == "browser.replay" else nullcontext():
            return self.registry.execute(
                call,
                description=step.description,
                approval=approval,
                requires_approval=step.requires_approval,
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
