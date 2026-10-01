"""公共 JSON Schema 校验入口测试。"""

from __future__ import annotations

import pytest

from mica_agent.schema_validation import (
    InvalidToolSchemaError,
    ToolArgumentsValidationError,
    validate_tool_arguments,
    validate_tool_schema,
)
from mica_agent.tool_contracts import ToolRequest, ToolSpec


def test_validate_tool_schema_accepts_draft_2020_12_object_schema() -> None:
    spec = ToolSpec(
        "read",
        "Read a value",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )

    assert validate_tool_schema(spec) is None


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "credential-like-invalid-type"},
        {"type": "object", "required": "credential-like-required"},
        {"type": "object", "properties": []},
        {"type": "object", "additionalProperties": "credential-like-value"},
    ],
)
def test_validate_tool_schema_rejects_invalid_schema_without_echoing_it(
    schema: dict[str, object],
) -> None:
    spec = ToolSpec("read", "Read a value", schema)

    with pytest.raises(InvalidToolSchemaError) as caught:
        validate_tool_schema(spec)

    assert str(caught.value) == "tool input schema is invalid"
    assert "credential-like" not in str(caught.value)


def test_validate_tool_schema_requires_a_tool_spec() -> None:
    with pytest.raises(TypeError, match="ToolSpec"):
        validate_tool_schema({})  # type: ignore[arg-type]


def test_validate_tool_arguments_accepts_valid_nested_values_and_empty_object() -> None:
    empty_spec = ToolSpec("empty", "Accept an empty object", {"type": "object"})
    complex_spec = ToolSpec(
        "read",
        "Read values",
        {
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {
                        "levels": {"type": "array", "items": {"type": "integer"}},
                        "mode": {"enum": ["short", "full"]},
                    },
                    "required": ["levels", "mode"],
                    "additionalProperties": False,
                }
            },
            "required": ["options"],
            "additionalProperties": False,
        },
    )

    assert validate_tool_arguments(empty_spec, {}) is None
    assert validate_tool_arguments(empty_spec, {"extra": "allowed by default"}) is None
    assert (
        validate_tool_arguments(
            complex_spec,
            ToolRequest(
                "call-1",
                "read",
                {"options": {"levels": [1, 2], "mode": "short"}},
            ).arguments,
        )
        is None
    )


@pytest.mark.parametrize(
    ("schema", "arguments"),
    [
        (
            {"type": "object", "required": ["path"]},
            {},
        ),
        (
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
            {"path": 42},
        ),
        (
            {
                "type": "object",
                "properties": {
                    "options": {
                        "type": "object",
                        "properties": {"level": {"type": "integer"}},
                    }
                },
            },
            {"options": {"level": "credential-like-level"}},
        ),
        (
            {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "integer"}}
                },
            },
            {"items": [1, "credential-like-item"]},
        ),
        (
            {
                "type": "object",
                "properties": {"mode": {"enum": ["short", "full"]}},
            },
            {"mode": "credential-like-mode"},
        ),
        (
            {"type": "object", "additionalProperties": False},
            {"credential-like-extra": "hidden"},
        ),
        (
            {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
            },
            {"count": True},
        ),
    ],
)
def test_validate_tool_arguments_rejects_schema_mismatches_without_values(
    schema: dict[str, object], arguments: dict[str, object]
) -> None:
    spec = ToolSpec("read", "Read values", schema)

    request = ToolRequest("call-1", "read", arguments)

    with pytest.raises(ToolArgumentsValidationError) as caught:
        validate_tool_arguments(spec, request.arguments)

    assert str(caught.value) == "tool arguments do not match input schema"
    assert "credential-like" not in str(caught.value)


def test_validate_tool_arguments_does_not_modify_request_arguments() -> None:
    spec = ToolSpec(
        "read",
        "Read values",
        {
            "type": "object",
            "properties": {"items": {"type": "array"}},
        },
    )
    request = ToolRequest("call-1", "read", {"items": [1, {"nested": True}]})

    validate_tool_arguments(spec, request.arguments)

    assert request.arguments == {"items": (1, {"nested": True})}
