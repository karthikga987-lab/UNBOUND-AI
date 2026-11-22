"""Shared, replaceable runtime used by all HTTP routes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel

from backend.app.llm.provider import LLMConfigurationError, LLMProvider, get_provider_from_env
from backend.app.orchestrator.executor import TaskExecutor, Verifier
from backend.app.orchestrator.task_manager import TaskManager
from backend.app.orchestrator.tool_registry import ToolCallable, ToolRegistry
from backend.app.schemas.contracts import ToolDefinition


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

    def register_tool(
        self,
        definition: ToolDefinition,
        input_model: type[BaseModel],
        execute: ToolCallable,
        verifier: Verifier | None = None,
    ) -> None:
        """Register a real adapter and optionally its tool-specific verifier.

        A tool without a specific verifier can use the runtime's explicitly
        configured shared verifier. Otherwise the executor will not invoke it.
        """
        if verifier is not None and not callable(verifier):
            raise TypeError("Verifier must be callable")
        self.registry.register(definition, input_model, execute)
        if verifier is not None:
            self.executor.configure_verifier(definition.tool_name, verifier)

    def configure_verifier(self, tool_name: str, verifier: Verifier) -> None:
        """Configure verification after initialization for a registered tool."""
        self.registry.get_tool(tool_name)  # Fail clearly for misspelled/unavailable tools.
        self.executor.configure_verifier(tool_name, verifier)

