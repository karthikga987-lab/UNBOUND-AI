"""Single-action observe/act/verify loop with approval and replay gates."""
from __future__ import annotations

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
        self.browser_replay_bridge = browser_replay_bridge

    def run(self, task_id: UUID4 | UUID | str, *, resume: bool = False) -> TaskResponse:
        with self.manager.execution_lock(task_id):
            result = self._run_locked(task_id, resume=resume)
        if result.status in {
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.NEEDS_ATTENTION, TaskStatus.CANCELLED,
        }:
            self.manager.notify_task_terminal(result.task_id)
        return self.manager._public_task(result)

    def _run_locked(self, task_id: UUID4 | UUID | str, *, resume: bool) -> TaskResponse:
        task = self.manager.get_for_execution(task_id)
        if task.status in {TaskStatus.CANCELLED, TaskStatus.COMPLETED}:
            return task
        if task.status == TaskStatus.NEEDS_ATTENTION:
            if task.error != NO_VERIFIER_ERROR or self.verifier is None:
                return task
            task.status = TaskStatus.EXECUTING
            task.error = None
            task = self.manager.save_for_execution(task)
        if task.status == TaskStatus.AWAITING_APPROVAL and not resume:
            return task
        if task.status == TaskStatus.FAILED and not resume:
            return task
        if resume:
            # Do not reset an uncertain browser action through generic recovery.
            if any(
                step.tool_name == "browser.replay" and step.status == StepStatus.RUNNING
                for step in task.plan
            ) and self.browser_replay_bridge is None:
                return self._mark_replay_attention(task_id, task.current_step, "Trusted replay continuation is unavailable")
            task = self.manager.save_for_execution(prepare_resume(task))

        for step in task.plan:
            latest = self.manager.get_for_execution(task_id)
            if latest.status == TaskStatus.CANCELLED:
                return latest
            if step.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}:
                continue
            task.current_step = step.step_id
            if self.verifier is None:
                task.status = TaskStatus.NEEDS_ATTENTION
                task.error = NO_VERIFIER_ERROR
                self.manager.event(task, "verification.unavailable", EventStatus.NEEDS_ATTENTION, task.error, step.step_id)
                return self.manager.save_for_execution(task)

            task.status = TaskStatus.EXECUTING
            task = self.manager.save_for_execution(task)
            if task.status == TaskStatus.CANCELLED:
                return task

            try:
                self.registry.get_tool(step.tool_name)
                self.registry.validate_input(step.tool_name, step.input)
                planned_call = ToolCall(
                    task_id=task.task_id,
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    arguments=step.input,
                )
                if step.tool_name == "browser.replay":
                    result_or_task = self._execute_browser_replay(task_id, task, step, planned_call)
                    if isinstance(result_or_task, TaskResponse):
                        return result_or_task
                    result = result_or_task
                    if not result.success:
                        task = self.manager.get_for_execution(task_id)
                        step = next(item for item in task.plan if item.step_id == planned_call.step_id)
                        return self._fail_step(task_id, task, step, "Browser workflow replay failed safely")
                    if not isinstance(result.data, dict) or result.data.get("status") != "completed":
                        return self._mark_replay_attention(task_id, step.step_id, "Browser replay did not reach a verified terminal state")
                else:
                    result_or_task = self._execute_planned_call(task_id, task, step, planned_call)
                    if isinstance(result_or_task, TaskResponse):
                        return result_or_task
                    result = result_or_task
            except ToolExecutionCancelled:
                latest = self.manager.get_for_execution(task_id)
                if latest.status == TaskStatus.CANCELLED:
                    return latest
                return self._fail_step(task_id, latest, step, "Could not claim the planned tool action")
            except Exception:
                latest = self.manager.get_for_execution(task_id)
                if latest.status == TaskStatus.CANCELLED:
                    return latest
                message = "Browser workflow replay could not be continued safely" if step.tool_name == "browser.replay" else "Tool execution could not be completed safely"
                return self._fail_step(task_id, latest, step, message)

            task = self.manager.get_for_execution(task_id)
            step = next(item for item in task.plan if item.step_id == planned_call.step_id)
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
                task = self.manager.save_for_execution(task)
            if not result.success:
                return self._fail_step(task_id, task, step, result.error or f"Tool '{step.tool_name}' failed")

            task.status = TaskStatus.VERIFYING
            self.manager.event(task, "verification.started", EventStatus.VERIFYING, "Checking expected outcome", step.step_id, evidence_ids)
            try:
                verified = verify_result(step.tool_name, step.expected_outcome, result, self.verifier)
            except Exception:
                step.verification = {"verified": False, "expected_outcome": step.expected_outcome, "error": "Verifier failed safely"}
                return self._fail_step(task_id, task, step, "Outcome verification failed safely", event_type="verification.failed")
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
            task = self.manager.save_for_execution(task)
            if task.status == TaskStatus.CANCELLED:
                return task

        task.current_step = None
        task.status = TaskStatus.COMPLETED
        task.error = None
        task.result = synthesize_result(task, self.manager.missing_information(task_id))
        self.manager.event(task, "task.completed", EventStatus.COMPLETED, "All planned steps completed and verified")
        return self.manager.save_for_execution(task)

    def _execute_planned_call(self, task_id, task, step, call):
        needs_approval = self.registry.action_requires_approval(call, step.description) or step.requires_approval
        approval = self.manager.approved_request(call) if needs_approval else None
        if needs_approval and approval is None:
            step.status = StepStatus.AWAITING_APPROVAL
            task.status = TaskStatus.AWAITING_APPROVAL
            self._append_pending_approval(task, step)
            self.manager.event(task, "approval.required", EventStatus.AWAITING_APPROVAL, "Approval required before this action", step.step_id)
            return self.manager.save_for_execution(task)
        return self.registry.execute(
            call,
            description=step.description,
            approval=approval,
            requires_approval=step.requires_approval,
            before_execute=lambda: self.manager.claim_tool_call(task_id, call, approval),
        )

    def _execute_browser_replay(self, task_id, task, step, planned_call):
        bridge = self.browser_replay_bridge
        if bridge is None:
            return self._mark_replay_attention(task_id, step.step_id, "Browser replay orchestration is unavailable")

        pending = bridge.pending_for_task(task.task_id)
        if pending is not None:
            pending_action, policy_context = pending
            parent = bridge.recover_parent_tool_call(policy_context)
            if parent.step_id != step.step_id or parent.task_id != task.task_id:
                return self._mark_replay_attention(task_id, step.step_id, "Replay continuation does not match the planned step")
            return self._continue_replay(task_id, task, step, pending_action, policy_context)

        # Once a parent call has been claimed, missing bridge state means that
        # restart recovery cannot be trusted. Never start the workflow again.
        recorded = [
            call for call in self.manager.tool_calls(task.task_id)
            if call.tool_name == "browser.replay" and call.step_id == step.step_id
        ]
        if recorded or step.status == StepStatus.RUNNING:
            return self._mark_replay_attention(task_id, step.step_id, "Trusted replay continuation is unavailable after recovery")
        if planned_call.arguments.get("operation") != "start":
            return self._mark_replay_attention(task_id, step.step_id, "Planner-provided replay resume is not trusted")

        needs_approval = self.registry.action_requires_approval(planned_call, step.description) or step.requires_approval
        approval = self.manager.approved_request(planned_call) if needs_approval else None
        if needs_approval and approval is None:
            step.status = StepStatus.AWAITING_APPROVAL
            task.status = TaskStatus.AWAITING_APPROVAL
            self._append_pending_approval(task, step)
            self.manager.event(task, "approval.required", EventStatus.AWAITING_APPROVAL, "Approval required before this action", step.step_id)
            return self.manager.save_for_execution(task)

        with self.manager.replay_dispatch_lock(task_id):
            if self.manager.is_cancelled(task_id):
                raise ToolExecutionCancelled("Cancelled")
            result = self.registry.execute(
                planned_call,
                description=step.description,
                approval=approval,
                requires_approval=step.requires_approval,
                before_execute=lambda: self.manager.claim_tool_call(task_id, planned_call, approval),
            )
        return self._handle_replay_result(task_id, step, result)

    def _continue_replay(self, task_id, task, step, pending_action, policy_context):
        bridge = self.browser_replay_bridge
        while True:
            authorization = bridge.issue_authorization(pending_action, policy_context)
            if authorization is None:
                latest = self.manager.get_for_execution(task_id)
                if latest.status in {TaskStatus.AWAITING_APPROVAL, TaskStatus.CANCELLED, TaskStatus.NEEDS_ATTENTION}:
                    return latest
                return self._mark_replay_attention(task_id, step.step_id, "Replay action authorization is unavailable")

            parent = bridge.recover_parent_tool_call(policy_context)
            resume_call = parent.model_copy(update={"arguments": {"operation": "resume"}})
            registry_approval = self.manager.replay_resume_approval(resume_call, authorization)
            if registry_approval is None:
                return self._mark_replay_attention(task_id, step.step_id, "Trusted replay authorization could not be claimed")

            with self.manager.replay_dispatch_lock(task_id):
                if self.manager.is_cancelled(task_id):
                    raise ToolExecutionCancelled("Cancelled")
                result = self.registry.execute(
                    resume_call,
                    description="Continue one authorized browser workflow action",
                    approval=registry_approval,
                    before_execute=lambda: self.manager.claim_replay_resume(resume_call, authorization, registry_approval),
                )
            status = result.data.get("status") if isinstance(result.data, dict) else None
            if status == "pending_action":
                next_pending = bridge.pending_for_task(task_id)
                if next_pending is None:
                    return self._mark_replay_attention(task_id, step.step_id, "Next replay action is unavailable")
                pending_action, policy_context = next_pending
                continue
            return self._handle_replay_result(task_id, step, result)

    def _handle_replay_result(self, task_id, step, result):
        if not isinstance(result, ToolResult):
            return self._mark_replay_attention(task_id, step.step_id, "Browser replay returned an invalid result")
        data = result.data if isinstance(result.data, dict) else {}
        status = data.get("status")
        if status == "pending_action":
            pending = self.browser_replay_bridge.pending_for_task(task_id) if self.browser_replay_bridge else None
            if pending is None:
                return self._mark_replay_attention(task_id, step.step_id, "Replay pending action could not be recovered")
            return self._continue_replay(task_id, self.manager.get_for_execution(task_id), step, pending[0], pending[1])
        if status == "completed":
            run_id = data.get("replay_run_id")
            if run_id and self.browser_replay_bridge:
                try:
                    self.browser_replay_bridge.forget_replay(task_id, UUID(str(run_id)))
                except (ValueError, TypeError):
                    return self._mark_replay_attention(task_id, step.step_id, "Replay completion identity is invalid")
            return result
        if status == "needs_attention":
            return self._mark_replay_attention(task_id, step.step_id, "Browser replay requires operator attention")
        if status == "failed":
            return ToolResult(success=False, error="Browser workflow replay failed safely")
        return self._mark_replay_attention(task_id, step.step_id, "Browser replay returned an unknown status")

    def _append_pending_approval(self, task, step) -> None:
        pending = next(
            (item for item in task.pending_approvals if item.step_id == step.step_id and item.status == ApprovalStatus.PENDING),
            None,
        )
        if pending is None:
            task.pending_approvals.append(ApprovalRequest(
                task_id=task.task_id,
                step_id=step.step_id,
                tool_name=step.tool_name,
                description=step.description,
                arguments=step.input,
            ))

    def _mark_replay_attention(self, task_id, step_id, message):
        task = self.manager.get_for_execution(task_id)
        task.status = TaskStatus.NEEDS_ATTENTION
        task.error = message
        if step_id is not None:
            task.current_step = step_id
        self.manager.event(task, "replay.needs_attention", EventStatus.NEEDS_ATTENTION, message, step_id)
        return self.manager.save_for_execution(task)

    def _fail_step(self, task_id, task: TaskResponse, step, message: str, *, event_type: str = "step.failed") -> TaskResponse:
        step.status = StepStatus.FAILED
        step.error = message
        task.status = TaskStatus.FAILED
        task.error = message
        task.result = synthesize_result(task, self.manager.missing_information(task_id))
        self.manager.event(task, event_type, EventStatus.FAILED, message, step.step_id)
        return self.manager.save_for_execution(task)


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
