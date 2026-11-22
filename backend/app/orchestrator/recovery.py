"""Resume tasks by retaining completed work and retrying recoverable current steps."""
from backend.app.orchestrator.models import StepStatus, TaskResponse, TaskStatus


def prepare_resume(task: TaskResponse) -> TaskResponse:
    if task.status in {TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.NEEDS_CLARIFICATION}:
        return task
    for step in task.steps:
        if step.status == StepStatus.RUNNING:
            step.status = StepStatus.PENDING
    task.status = TaskStatus.PENDING
    return task
