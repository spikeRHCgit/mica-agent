"""供应商无关的公共工具数据契约。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
JsonObject: TypeAlias = Mapping[str, JsonValue]
MutableJsonValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | list["MutableJsonValue"]
    | dict[str, "MutableJsonValue"]
)

_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")


def _validate_non_blank_text(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a str")
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty and non-blank")


def _validate_tool_name(value: object, field_name: str = "name") -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a str")
    if not _TOOL_NAME_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} must start with an ASCII letter or underscore, "
            "contain only ASCII letters, digits, underscores, or hyphens after "
            "the first character, and be at most 64 characters long"
        )


def _freeze_json(
    value: object, field_name: str, active: set[int] | None = None
) -> JsonValue:
    if active is None:
        active = set()
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must contain only finite JSON numbers")
        return value
    if isinstance(value, (list, tuple, Mapping)):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{field_name} must not contain recursive values")
        active.add(identity)
        try:
            if isinstance(value, (list, tuple)):
                return tuple(_freeze_json(item, field_name, active) for item in value)
            if not all(isinstance(key, str) for key in value):
                raise TypeError(f"{field_name} must contain only string object keys")
            return MappingProxyType(
                {
                    key: _freeze_json(item, field_name, active)
                    for key, item in value.items()
                }
            )
        finally:
            active.remove(identity)
    raise TypeError(f"{field_name} must contain only JSON-compatible values")


def _freeze_json_object(value: object, field_name: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a JSON object")
    frozen = _freeze_json(value, field_name)
    assert isinstance(frozen, Mapping)
    return frozen


def to_mutable_json(value: JsonValue) -> MutableJsonValue:
    """把 P5 不可变 JSON 快照递归复制为标准可变 JSON 值。"""

    def copy_snapshot(current: object, active: set[int]) -> MutableJsonValue:
        if current is None or isinstance(current, (bool, str)):
            return current
        if isinstance(current, int):
            return current
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError("JSON snapshot must contain only finite numbers")
            return current
        if isinstance(current, tuple):
            identity = id(current)
            if identity in active:
                raise ValueError("JSON snapshot must not contain recursive values")
            active.add(identity)
            try:
                return [copy_snapshot(item, active) for item in current]
            finally:
                active.remove(identity)
        if isinstance(current, Mapping):
            if not all(isinstance(key, str) for key in current):
                raise TypeError("JSON snapshot object keys must be strings")
            identity = id(current)
            if identity in active:
                raise ValueError("JSON snapshot must not contain recursive values")
            active.add(identity)
            try:
                return {
                    key: copy_snapshot(item, active) for key, item in current.items()
                }
            finally:
                active.remove(identity)
        raise TypeError("value must be a P5 JSON snapshot")

    return copy_snapshot(value, set())


class ToolResultStatus(Enum):
    """工具执行结果的稳定 wire 状态。"""

    SUCCESS = "success"
    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    PERMISSION_DENIED = "permission_denied"
    EXECUTION_FAILED = "execution_failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """向模型公开的工具定义快照。"""

    name: str
    description: str
    input_schema: JsonObject

    def __post_init__(self) -> None:
        _validate_tool_name(self.name)
        _validate_non_blank_text(self.description, "description")
        object.__setattr__(
            self,
            "input_schema",
            _freeze_json_object(self.input_schema, "input_schema"),
        )


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Provider 已完整组装的一次工具调用。"""

    call_id: str
    name: str
    arguments: JsonObject

    def __post_init__(self) -> None:
        _validate_non_blank_text(self.call_id, "call_id")
        _validate_tool_name(self.name)
        object.__setattr__(
            self, "arguments", _freeze_json_object(self.arguments, "arguments")
        )


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """Engine 交给工具系统的一次已组装请求。"""

    call_id: str
    name: str
    arguments: JsonObject

    def __post_init__(self) -> None:
        _validate_non_blank_text(self.call_id, "call_id")
        _validate_tool_name(self.name)
        object.__setattr__(
            self, "arguments", _freeze_json_object(self.arguments, "arguments")
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具系统返回的结构化、安全结果。"""

    call_id: str
    tool_name: str
    status: ToolResultStatus
    message: str
    data: JsonValue | None = None

    def __post_init__(self) -> None:
        _validate_non_blank_text(self.call_id, "call_id")
        _validate_tool_name(self.tool_name, "tool_name")
        if not isinstance(self.status, ToolResultStatus):
            raise TypeError("status must be a ToolResultStatus")
        _validate_non_blank_text(self.message, "message")
        if self.data is not None:
            object.__setattr__(self, "data", _freeze_json(self.data, "data"))
