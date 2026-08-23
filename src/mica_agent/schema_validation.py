"""ToolSpec 的固定 Draft 2020-12 Schema 校验边界。"""

from __future__ import annotations

from collections.abc import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry

from .tool_contracts import JsonObject, ToolSpec, to_mutable_json

_INVALID_SCHEMA_MESSAGE = "tool input schema is invalid"
_INVALID_ARGUMENTS_MESSAGE = "tool arguments do not match input schema"


class InvalidToolSchemaError(ValueError):
    """开发者提供的工具输入 Schema 无效。"""


class ToolArgumentsValidationError(ValueError):
    """模型参数不符合工具输入 Schema。"""


def validate_tool_schema(spec: ToolSpec) -> None:
    """确认 ToolSpec.input_schema 是有效的 Draft 2020-12 Schema。"""
    if not isinstance(spec, ToolSpec):
        raise TypeError("spec must be a ToolSpec")

    schema = to_mutable_json(spec.input_schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise InvalidToolSchemaError(_INVALID_SCHEMA_MESSAGE) from None


def validate_tool_arguments(spec: ToolSpec, arguments: JsonObject) -> None:
    """按 ToolSpec 的 Draft 2020-12 Schema 校验一次模型参数快照。"""
    validate_tool_schema(spec)
    if not isinstance(arguments, Mapping):
        raise TypeError("arguments must be a JSON object")

    schema = to_mutable_json(spec.input_schema)
    instance = to_mutable_json(arguments)
    try:
        Draft202012Validator(schema, registry=Registry()).validate(instance)
    except ValidationError:
        raise ToolArgumentsValidationError(_INVALID_ARGUMENTS_MESSAGE) from None
    except Exception:  # noqa: BLE001 - hide all validator backend details
        raise InvalidToolSchemaError(_INVALID_SCHEMA_MESSAGE) from None
