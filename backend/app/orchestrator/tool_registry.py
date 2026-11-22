"""Registry and guarded execution for tools supplied by browser/MCP integrations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError


AGREED_TOOL_NAMES = frozenset({
    "browser.navigate", "browser.click", "browser.fill", "browser.record", "browser.replay", "browser.download",
    "document.upload", "document.search", "context.retrieve", "context.relate", "report.generate",
})


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: type[BaseModel]
    execute: Callable[[BaseModel], Any]
    permissions: dict[str, Any] | None = None


class ToolUnavailableError(LookupError):
    pass


class ToolInputError(ValueError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name not in AGREED_TOOL_NAMES:
            raise ValueError(f"Tool name '{tool.name}' is not in the agreed tool namespace")
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def list_tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def get_tool(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolUnavailableError(f"Tool '{name}' is unavailable") from exc

    def validate_input(self, name: str, value: dict[str, Any]) -> BaseModel:
        tool = self.get_tool(name)
        try:
            return tool.input_schema.model_validate(value)
        except ValidationError as exc:
            raise ToolInputError(f"Invalid input for '{name}': {exc}") from exc

    def execute(self, name: str, value: dict[str, Any]) -> Any:
        tool = self.get_tool(name)
        validated = self.validate_input(name, value)
        return tool.execute(validated)
