"""Prepare shared-contract tasks for safe retry/resume."""
from backend.app.schemas.contracts import StepStatus, TaskResponse, TaskStatus


def prepare_resume(task: TaskResponse) -> TaskResponse:
    if task.status in {TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.NEEDS_ATTENTION, TaskStatus.AWAITING_APPROVAL}:
        return task
    for step in task.plan:
        if step.status in {StepStatus.RUNNING, StepStatus.FAILED}:
            step.status = StepStatus.PENDING
            step.error = None
            step.verification = None
    task.status = TaskStatus.EXECUTING
    task.error = None
    task.current_step = None
    return task
