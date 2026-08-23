"""工具定义与异步执行入口的内存注册表。"""

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
    """ToolRegistry 的稳定基础错误。"""


class DuplicateToolError(ToolRegistryError):
    """工具名称已经注册。"""


class ToolNotFoundError(ToolRegistryError):
    """工具名称未注册。"""


class ToolHandler(Protocol):
    """异步工具处理函数的公共调用协议。"""

    def __call__(
        self, request: ToolRequest, cancellation: CancellationToken
    ) -> Awaitable[ToolResult]: ...


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """供未来 ToolExecutor 使用的不可变注册项。"""

    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    """集中拥有工具定义及其异步处理入口。"""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        """校验并注册一个工具，但不执行处理函数。"""
        if not isinstance(spec, ToolSpec):
            raise TypeError("spec must be a ToolSpec")
        if not callable(handler):
            raise TypeError("handler must be callable")
        if spec.name in self._tools:
            raise DuplicateToolError("tool name is already registered")
        validate_tool_schema(spec)
        self._tools[spec.name] = RegisteredTool(spec, handler)

    def specs(self) -> tuple[ToolSpec, ...]:
        """返回仅含公开定义、按名称排序的当前快照。"""
        return tuple(self._tools[name].spec for name in sorted(self._tools))

    def resolve(self, name: str) -> RegisteredTool:
        """返回未来 ToolExecutor 使用的注册项，但不执行处理函数。"""
        if not isinstance(name, str):
            raise TypeError("name must be a str")
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError("tool is not registered") from None
