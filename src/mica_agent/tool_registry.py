"""In-memory registry of tool definitions and their async execution entry points."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from .cancellation import CancellationToken
from .schema_validation import InvalidToolSchemaError, validate_tool_schema
from .tool_contracts import ToolRequest, ToolResult, ToolSpec

__all__ = [
    "DuplicateToolError",
    "InvalidToolSchemaError",
    "RegisteredTool",
    "ToolHandler",
    "ToolNotFoundError",
    "ToolRegistry",
    "ToolRegistryError",
]


class ToolRegistryError(Exception):
    """Stable base error for ``ToolRegistry``."""


class DuplicateToolError(ToolRegistryError):
    """The tool name is already registered."""


class ToolNotFoundError(ToolRegistryError):
    """The tool name is not registered."""


class ToolHandler(Protocol):
    """Public call protocol for an async tool handler."""

    def __call__(
        self, request: ToolRequest, cancellation: CancellationToken
    ) -> Awaitable[ToolResult]: ...


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """Immutable registration entry used by ``ToolExecutor``."""

    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    """Owns tool definitions and their async handler entry points."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        """Validate and register a tool without invoking its handler."""
        if not isinstance(spec, ToolSpec):
            raise TypeError("spec must be a ToolSpec")
        if not callable(handler):
            raise TypeError("handler must be callable")
        if spec.name in self._tools:
            raise DuplicateToolError("tool name is already registered")
        validate_tool_schema(spec)
        self._tools[spec.name] = RegisteredTool(spec, handler)

    def specs(self) -> tuple[ToolSpec, ...]:
        """Return the current name-sorted snapshot of public specs only."""
        return tuple(self._tools[name].spec for name in sorted(self._tools))

    def resolve(self, name: str) -> RegisteredTool:
        """Return the registered entry without invoking its handler."""
        if not isinstance(name, str):
            raise TypeError("name must be a str")
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError("tool is not registered") from None
