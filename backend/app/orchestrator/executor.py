"""Single-action observe/act/verify execution loop."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from backend.app.orchestrator.models import EvidenceItem, StepStatus, TaskResponse, TaskStatus
from backend.app.orchestrator.recovery import prepare_resume
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.orchestrator.tool_registry import ToolRegistry
from backend.app.safety.permissions import requires_approval


Verifier = Callable[[str, str, Any], bool]


def verify_result(tool_name: str, expected: str, result: Any, verifier: Verifier | None = None) -> bool:
    if verifier:
        return bool(verifier(tool_name, expected, result))
    if not isinstance(result, dict):
        return False
    return result.get("success") is True and result.get("verified") is True


class TaskExecutor:
    def __init__(self, manager: TaskManager, registry: ToolRegistry, verifier: Verifier | None = None) -> None:
        self.manager = manager
        self.registry = registry
        self.verifier = verifier

    def run(self, task_id: str, *, approved_steps: set[str] | None = None, resume: bool = False) -> TaskResponse:
        task = self.manager.get(task_id)
        if task.status == TaskStatus.CANCELLED:
            return task
        if resume:
            task = prepare_resume(task)
        if task.status == TaskStatus.NEEDS_CLARIFICATION:
            return task
        task.status = TaskStatus.RUNNING
        approved = approved_steps or set()
        for index, step in enumerate(task.steps):
            # Re-read between actions so a concurrent cancellation is observed before
            # another external side effect begins.
            latest = self.manager.get(task_id)
            if latest.status == TaskStatus.CANCELLED:
                return latest
            task.current_step = index
            if step.status == StepStatus.COMPLETED:
                continue
            if task.status == TaskStatus.CANCELLED:
                return self.manager.save(task)
            try:
                tool = self.registry.get_tool(step.tool_name)
                self.registry.validate_input(step.tool_name, step.input)
                needs_approval = step.requires_approval or requires_approval(step.description, tool.permissions)
                if needs_approval and step.step_id not in approved:
                    task.status = TaskStatus.AWAITING_APPROVAL
                    self.manager.event(task, "awaiting_approval", "Approval required before this action", step.step_id)
                    return self.manager.save(task)
                step.status = StepStatus.RUNNING
                self.manager.event(task, "running", f"Executing {step.tool_name}", step.step_id)
                result = self.registry.execute(step.tool_name, step.input)
                step.result = result if isinstance(result, dict) else {"value": str(result)}
                if not verify_result(step.tool_name, step.expected_outcome, result, self.verifier):
                    raise RuntimeError("Tool result did not provide evidence verifying the expected outcome")
                refs = result.get("evidence", []) if isinstance(result, dict) else []
                if isinstance(refs, str):
                    refs = [refs]
                for ref in refs:
                    evidence = EvidenceItem(reference=str(ref), summary=f"Evidence for {step.tool_name}", verified=True)
                    step.evidence.append(evidence)
                    task.evidence.append(evidence)
                step.status = StepStatus.COMPLETED
                task.completed_steps.append(step.step_id)
                task.current_step = index + 1
                if self.manager.get(task_id).status == TaskStatus.CANCELLED:
                    task.status = TaskStatus.CANCELLED
                self.manager.event(task, "completed", "Expected outcome verified", step.step_id)
                self.manager.save(task)
            except Exception as exc:
                step.status = StepStatus.FAILED
                step.error = str(exc)
                task.errors.append(f"{step.step_id}: {exc}")
                task.status = TaskStatus.FAILED
                self.manager.event(task, "failed", str(exc), step.step_id)
                task.final_result = synthesize_result(task)
                return self.manager.save(task)
        if task.status == TaskStatus.CANCELLED:
            return self.manager.save(task)
        task.status = TaskStatus.COMPLETED
        task.final_result = synthesize_result(task)
        self.manager.event(task, "completed", "All planned steps completed and verified")
        return self.manager.save(task)


def synthesize_result(task: TaskResponse) -> dict[str, Any]:
    """Deterministic evidence-backed summary; makes no unsupported factual claims."""
    completed = [s for s in task.steps if s.status == StepStatus.COMPLETED]
    return {
        "requested": task.goal,
        "actions_completed": [s.description for s in completed],
        "verified": [{"step_id": s.step_id, "outcome": s.expected_outcome, "evidence": [e.reference for e in s.evidence]} for s in completed],
        "failed_or_incomplete": [s.error or s.description for s in task.steps if s.status != StepStatus.COMPLETED],
        "assumptions_or_missing_information": task.missing_information,
        "errors": task.errors,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
