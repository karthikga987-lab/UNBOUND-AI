"""Shared, replaceable runtime used by all HTTP routes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from backend.app.llm.provider import LLMConfigurationError, LLMProvider, get_provider_from_env
from backend.app.orchestrator.executor import TaskExecutor, Verifier
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.orchestrator.tool_registry import ToolRegistry


@dataclass
class TaskRuntime:
    """One process-local set of orchestration services.

    Tasks and approvals are currently held in memory and do not survive restarts.
    """

    manager: TaskManager = field(default_factory=TaskManager)
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    provider: LLMProvider | None = None
    provider_factory: Callable[[], LLMProvider] = get_provider_from_env
    verifier: Verifier | None = None
    executor: TaskExecutor = field(init=False)

    def __post_init__(self) -> None:
        self.executor = TaskExecutor(self.manager, self.registry, self.verifier)

    def get_provider(self) -> LLMProvider:
        if self.provider is not None:
            return self.provider
        # Resolve lazily so an unconfigured installation still starts, then reuse
        # the selected provider for later requests in this application process.
        provider = self.provider_factory()
        self.provider = provider
        return provider

    def provider_is_configured(self) -> bool:
        try:
            self.get_provider()
        except LLMConfigurationError:
            return False
        return True

    def registered_tools_count(self) -> int:
        return len(self.registry.list_tools())

