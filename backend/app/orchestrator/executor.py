"""Single-action observe/act/verify loop with per-task serialization and approval gates."""
from __future__ import annotations

from threading import Condition, RLock
from typing import Callable
from uuid import UUID

from pydantic import UUID4

from backend.app.orchestrator.recovery import prepare_resume
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.orchestrator.tool_registry import ToolExecutionCancelled, ToolRegistry
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


def verify_result(tool_name: str, expected: str, result: ToolResult, verifier: Verifier) -> bool:
    """Verify an expected outcome independently from ToolResult.success."""
    return result.success and bool(verifier(tool_name, expected, result))


class TaskExecutor:
    def __init__(self, manager: TaskManager, registry: ToolRegistry, verifier: Verifier | None = None) -> None:
        self.manager = manager
        self.registry = registry
        self.verifier = verifier
        self._tool_verifiers: dict[str, Verifier] = {}
        self._configuration_condition = Condition(RLock())
        self._active_runs = 0

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

    def run(self, task_id: UUID4 | UUID | str, *, resume: bool = False) -> TaskResponse:
        # A second caller waits for the current run and then sees its persisted result.
        # Cancellation deliberately does not acquire this lock.
        with self._configuration_condition:
            self._active_runs += 1
        try:
            with self.manager.execution_lock(task_id):
                return self._run_locked(task_id, resume=resume)
        finally:
            with self._configuration_condition:
                self._active_runs -= 1
                if self._active_runs == 0:
                    self._configuration_condition.notify_all()

    def _run_locked(self, task_id: UUID4 | UUID | str, *, resume: bool) -> TaskResponse:
        task = self.manager.get(task_id)
        if task.status in {TaskStatus.CANCELLED, TaskStatus.COMPLETED}:
            return task
        if task.status == TaskStatus.NEEDS_ATTENTION:
            pending_step = next((step for step in task.plan if step.status not in {StepStatus.COMPLETED, StepStatus.SKIPPED}), None)
            if task.error != NO_VERIFIER_ERROR or pending_step is None or self.verifier_for(pending_step.tool_name) is None:
                return task
            task.status = TaskStatus.EXECUTING
            task.error = None
            task = self.manager.save(task)
        if task.status == TaskStatus.FAILED and not resume:
            return task
        if resume:
            task = self.manager.save(prepare_resume(task))

        for step in task.plan:
            latest = self.manager.get(task_id)
            if latest.status == TaskStatus.CANCELLED:
                return latest
            if step.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}:
                continue

            task.current_step = step.step_id
            step_verifier = self.verifier_for(step.tool_name)
            if step_verifier is None:
                task.status = TaskStatus.NEEDS_ATTENTION
                task.error = NO_VERIFIER_ERROR
                self.manager.event(task, "verification.unavailable", EventStatus.NEEDS_ATTENTION, task.error, step.step_id)
                return self.manager.save(task)

            task.status = TaskStatus.EXECUTING
            task = self.manager.save(task)
            if task.status == TaskStatus.CANCELLED:
                return task

            try:
                self.registry.get_tool(step.tool_name)
                self.registry.validate_input(step.tool_name, step.input)
                call = ToolCall(task_id=task.task_id, step_id=step.step_id, tool_name=step.tool_name, arguments=step.input)
                needs_approval = self.registry.action_requires_approval(call, step.description) or step.requires_approval

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
                        return self.manager.save(task)

                # The registry validates again, then atomically claims the step under
                # the same manager lock used by cancel(), immediately before callout.
                result = self.registry.execute(
                    call,
                    description=step.description,
                    approval=approval,
                    requires_approval=step.requires_approval,
                    before_execute=lambda: self.manager.claim_tool_call(task_id, call, approval),
                )
            except ToolExecutionCancelled:
                latest = self.manager.get(task_id)
                if latest.status == TaskStatus.CANCELLED:
                    return latest
                return self._fail_step(task_id, task, step, "Could not claim the planned tool action")
            except Exception as exc:
                latest = self.manager.get(task_id)
                if latest.status == TaskStatus.CANCELLED:
                    return latest
                return self._fail_step(task_id, latest, step, str(exc))

            task = self.manager.get(task_id)
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
                task = self.manager.save(task)

            if not result.success:
                return self._fail_step(task_id, task, step, result.error or f"Tool '{step.tool_name}' failed")

            task.status = TaskStatus.VERIFYING
            self.manager.event(task, "verification.started", EventStatus.VERIFYING, "Checking expected outcome", step.step_id, evidence_ids)
            try:
                verified = verify_result(step.tool_name, step.expected_outcome, result, step_verifier)
            except Exception as exc:
                step.verification = {"verified": False, "expected_outcome": step.expected_outcome, "error": f"Verifier error: {exc}"}
                return self._fail_step(task_id, task, step, f"Outcome verification failed: {exc}", event_type="verification.failed")
            step.verification = {
                "verified": verified,
                "expected_outcome": step.expected_outcome,
                "method": "injected_verifier",
                "tool_result": result.data,
                "evidence_ids": [str(item) for item in evidence_ids],
            }
            if not verified:
                return self._fail_step(task_id, task, step, "Tool result did not verify the expected outcome", event_type="verification.failed")

            step.status = StepStatus.COMPLETED
            step.error = None
            task.completed_steps = list(dict.fromkeys([*task.completed_steps, step.step_id]))
            task.current_step = None
            task.status = TaskStatus.EXECUTING
            self.manager.event(task, "step.verified", EventStatus.COMPLETED, "Expected outcome verified", step.step_id, evidence_ids)
            task = self.manager.save(task)
            if task.status == TaskStatus.CANCELLED:
                return task

        task.current_step = None
        task.status = TaskStatus.COMPLETED
        task.error = None
        task.result = synthesize_result(task, self.manager.missing_information(task_id))
        self.manager.event(task, "task.completed", EventStatus.COMPLETED, "All planned steps completed and verified")
        return self.manager.save(task)

    def _fail_step(self, task_id, task: TaskResponse, step, message: str, *, event_type: str = "step.failed") -> TaskResponse:
        step.status = StepStatus.FAILED
        step.error = message
        task.status = TaskStatus.FAILED
        task.error = message
        task.result = synthesize_result(task, self.manager.missing_information(task_id))
        self.manager.event(task, event_type, EventStatus.FAILED, message, step.step_id)
        return self.manager.save(task)


def synthesize_result(task: TaskResponse, missing_information: list[str] | None = None) -> dict:
    """Summarize only verified work and retain evidence references and limitations."""
    completed = [step for step in task.plan if step.status == StepStatus.COMPLETED and step.verification and step.verification.get("verified") is True]
    return {
        "requested": task.user_request,
        "actions_completed": [step.description for step in completed],
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
