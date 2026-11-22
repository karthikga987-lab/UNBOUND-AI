"""Backward-compatible imports for the authoritative shared contracts.

New code should import from ``backend.app.schemas.contracts`` directly. These
re-exports keep older integration imports from creating duplicate model classes.
"""
from backend.app.schemas.contracts import (
    EventStatus,
    EvidenceItem,
    ExecutionEvent,
    StepStatus,
    TaskCreateRequest,
    TaskResponse,
    TaskStatus,
    TaskStep,
)

__all__ = [
    "EventStatus",
    "EvidenceItem",
    "ExecutionEvent",
    "StepStatus",
    "TaskCreateRequest",
    "TaskResponse",
    "TaskStatus",
    "TaskStep",
]
