"""Prepare shared-contract tasks for safe retry/resume."""
from backend.app.schemas.contracts import StepStatus, TaskResponse, TaskStatus


def prepare_resume(task: TaskResponse) -> TaskResponse:
    if task.status in {TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.NEEDS_ATTENTION, TaskStatus.AWAITING_APPROVAL}:
        return task
    ambiguous_steps = [step for step in task.plan if step.status in {StepStatus.RUNNING, StepStatus.FAILED}]
    if ambiguous_steps:
        # A failed/abandoned external call may have taken effect even when its
        # verifier failed. Never replay it automatically; require human review.
        for step in ambiguous_steps:
            if step.status == StepStatus.RUNNING:
                step.status = StepStatus.FAILED
                step.error = "Execution was interrupted; the external outcome is unknown"
        task.status = TaskStatus.NEEDS_ATTENTION
        task.error = "A previous step may have had an external effect; review before creating a new task"
        task.current_step = None
        return task
    for step in task.plan:
        if step.status == StepStatus.PENDING:
            step.error = None
    task.status = TaskStatus.EXECUTING
    task.error = None
    task.current_step = None
    return task
