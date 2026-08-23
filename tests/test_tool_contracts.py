"""P5-01 公共工具数据契约测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from mica_agent.tool_contracts import (
    ToolCall,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)


def test_tool_contracts_carry_validated_public_data() -> None:
    spec = ToolSpec(
        name="read",
        description="Read a UTF-8 text file",
        input_schema={"type": "object", "required": ["path"]},
    )
    call = ToolCall(call_id="call-1", name="read", arguments={"path": "a.txt"})
    request = ToolRequest(call_id="call-1", name="read", arguments={"path": "a.txt"})
    result = ToolResult(
        call_id="call-1",
        tool_name="read",
        status=ToolResultStatus.SUCCESS,
        message="read completed",
        data={"content": "hello"},
    )

    assert spec.name == call.name == request.name == result.tool_name == "read"
    assert spec.description == "Read a UTF-8 text file"
    assert call.call_id == request.call_id == result.call_id == "call-1"
    assert call.arguments == request.arguments == {"path": "a.txt"}
    assert result.status is ToolResultStatus.SUCCESS
    assert result.message == "read completed"
    assert result.data == {"content": "hello"}


@pytest.mark.parametrize(
    ("status", "wire_value"),
    [
        (ToolResultStatus.SUCCESS, "success"),
        (ToolResultStatus.INVALID_ARGUMENTS, "invalid_arguments"),
        (ToolResultStatus.UNKNOWN_TOOL, "unknown_tool"),
        (ToolResultStatus.PERMISSION_DENIED, "permission_denied"),
        (ToolResultStatus.EXECUTION_FAILED, "execution_failed"),
        (ToolResultStatus.CANCELLED, "cancelled"),
    ],
)
def test_tool_result_status_wire_values(
    status: ToolResultStatus, wire_value: str
) -> None:
    assert status.value == wire_value


def test_json_objects_are_deep_immutable_snapshots() -> None:
    schema = {"type": "object", "required": ["path"]}
    arguments = {"path": "a.txt", "options": {"lines": [1, 2]}}
    data = {"items": [{"name": "a"}]}

    spec = ToolSpec("read", "Read a file", schema)
    call = ToolCall("call-1", "read", arguments)
    result = ToolResult("call-1", "read", ToolResultStatus.SUCCESS, "done", data)
    schema["type"] = "array"
    schema["required"].append("secret")  # type: ignore[union-attr]
    arguments["path"] = "changed"
    arguments["options"]["lines"].append(3)  # type: ignore[index,union-attr]
    data["items"].append({"name": "b"})  # type: ignore[union-attr]

    assert spec.input_schema == {"type": "object", "required": ("path",)}
    assert call.arguments == {
        "path": "a.txt",
        "options": {"lines": (1, 2)},
    }
    assert result.data == {"items": ({"name": "a"},)}
    assert isinstance(call.arguments, MappingProxyType)
    with pytest.raises(TypeError):
        call.arguments["path"] = "tampered"  # type: ignore[index]
    nested = call.arguments["options"]
    assert isinstance(nested, MappingProxyType)
    with pytest.raises(TypeError):
        nested["lines"] = ()  # type: ignore[index]


def test_public_tool_objects_are_frozen() -> None:
    objects_and_fields = [
        (ToolSpec("read", "Read a file", {}), "name"),
        (ToolCall("call-1", "read", {}), "call_id"),
        (ToolRequest("call-1", "read", {}), "call_id"),
        (
            ToolResult("call-1", "read", ToolResultStatus.SUCCESS, "done", None),
            "call_id",
        ),
    ]

    for value, field_name in objects_and_fields:
        with pytest.raises(FrozenInstanceError):
            setattr(value, field_name, "changed")


@pytest.mark.parametrize("call_id", ["", "   ", "\t\n"])
def test_tool_objects_reject_blank_call_ids(call_id: str) -> None:
    for build in (
        lambda: ToolCall(call_id, "read", {}),
        lambda: ToolRequest(call_id, "read", {}),
        lambda: ToolResult(call_id, "read", ToolResultStatus.SUCCESS, "done"),
    ):
        with pytest.raises(ValueError, match="call_id"):
            build()


@pytest.mark.parametrize("call_id", [None, 42, b"call"])
def test_tool_objects_reject_non_string_call_ids(call_id: object) -> None:
    for build in (
        lambda: ToolCall(call_id, "read", {}),  # type: ignore[arg-type]
        lambda: ToolRequest(call_id, "read", {}),  # type: ignore[arg-type]
        lambda: ToolResult(  # type: ignore[arg-type]
            call_id, "read", ToolResultStatus.SUCCESS, "done"
        ),
    ):
        with pytest.raises(TypeError, match="call_id"):
            build()


@pytest.mark.parametrize(
    "name",
    ["", "1read", "-read", "has space", "dot.name", "slash/name", "a" * 65],
)
def test_tool_objects_reject_unsafe_names(name: str) -> None:
    for build in (
        lambda: ToolSpec(name, "description", {}),
        lambda: ToolCall("call-1", name, {}),
        lambda: ToolRequest("call-1", name, {}),
        lambda: ToolResult("call-1", name, ToolResultStatus.SUCCESS, "done"),
    ):
        with pytest.raises(ValueError):
            build()


@pytest.mark.parametrize("name", ["1read", "-read"])
def test_tool_name_error_explains_leading_character_rule_without_echoing_name(
    name: str,
) -> None:
    builders = (
        lambda: ToolSpec(name, "description", {}),
        lambda: ToolCall("call-1", name, {}),
        lambda: ToolRequest("call-1", name, {}),
        lambda: ToolResult("call-1", name, ToolResultStatus.SUCCESS, "done"),
    )

    for build in builders:
        with pytest.raises(ValueError) as caught:
            build()
        message = str(caught.value)
        assert "must start with an ASCII letter or underscore" in message
        assert "after the first character" in message
        assert "at most 64 characters" in message
        assert name not in message


def test_tool_objects_reject_non_string_names_and_descriptions() -> None:
    with pytest.raises(TypeError, match="name"):
        ToolSpec(42, "description", {})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="name"):
        ToolCall("call-1", 42, {})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="tool_name"):
        ToolResult(  # type: ignore[arg-type]
            "call-1", 42, ToolResultStatus.SUCCESS, "done"
        )
    with pytest.raises(ValueError, match="description"):
        ToolSpec("read", "   ", {})
    with pytest.raises(TypeError, match="description"):
        ToolSpec("read", b"secret", {})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [None, [], (), "object", 42])
def test_schema_and_arguments_must_be_json_objects(value: object) -> None:
    for build in (
        lambda: ToolSpec("read", "description", value),
        lambda: ToolCall("call-1", "read", value),
        lambda: ToolRequest("call-1", "read", value),
    ):
        with pytest.raises(TypeError, match="JSON object"):
            build()


@pytest.mark.parametrize(
    "invalid",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        b"secret bytes",
        {"set value"},
        object(),
        {1: "non-string key"},
    ],
)
def test_recursive_json_data_rejects_non_json_values(invalid: object) -> None:
    secret = "credential-like-secret-marker"

    for build in (
        lambda: ToolSpec("read", "description", {"nested": [invalid]}),
        lambda: ToolCall("call-1", "read", {"nested": [invalid]}),
        lambda: ToolRequest("call-1", "read", {"nested": [invalid]}),
        lambda: ToolResult(
            "call-1",
            "read",
            ToolResultStatus.SUCCESS,
            "done",
            {"secret": secret, "nested": [invalid]},
        ),
    ):
        with pytest.raises((TypeError, ValueError)) as caught:
            build()
        assert secret not in str(caught.value)
        assert "secret bytes" not in str(caught.value)


def test_recursive_containers_are_rejected_without_echoing_data() -> None:
    recursive: list[object] = []
    recursive.append(recursive)

    with pytest.raises(ValueError, match="recursive") as caught:
        ToolCall("call-1", "read", {"secret": recursive})
    assert "secret" not in str(caught.value)


def test_tool_result_validates_status_message_and_optional_data() -> None:
    assert (
        ToolResult("call-1", "read", ToolResultStatus.CANCELLED, "cancelled").data
        is None
    )

    with pytest.raises(TypeError, match="status"):
        ToolResult("call-1", "read", "success", "done")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="message"):
        ToolResult("call-1", "read", ToolResultStatus.SUCCESS, "   ")
    with pytest.raises(TypeError, match="message"):
        ToolResult(  # type: ignore[arg-type]
            "call-1", "read", ToolResultStatus.SUCCESS, b"secret"
        )
