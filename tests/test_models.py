"""Standard Message 公共契约测试。

验证供应商无关的文本与工具消息，支持 system/user/assistant/tool
四种角色，同时保持旧纯文本构造与不可变行为。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mica_agent.models import Message, MessageRole
from mica_agent.tool_contracts import ToolCall


def test_message_carries_role_and_text_content() -> None:
    message = Message(role=MessageRole.USER, content="hello mica")

    assert message.role is MessageRole.USER
    assert message.content == "hello mica"


@pytest.mark.parametrize(
    ("role", "wire_value"),
    [
        (MessageRole.SYSTEM, "system"),
        (MessageRole.USER, "user"),
        (MessageRole.ASSISTANT, "assistant"),
        (MessageRole.TOOL, "tool"),
    ],
)
def test_message_role_wire_values(role: MessageRole, wire_value: str) -> None:
    assert role.value == wire_value


def test_four_roles_are_distinct() -> None:
    assert len(set(MessageRole)) == 4


def test_invalid_role_is_rejected_at_creation() -> None:
    with pytest.raises(TypeError):
        Message(role="user", content="hi")  # type: ignore[arg-type]


@pytest.mark.parametrize("content", [b"bytes", 42, None, ["list"]])
def test_non_text_content_is_rejected(content: object) -> None:
    with pytest.raises(TypeError):
        Message(role=MessageRole.USER, content=content)  # type: ignore[arg-type]


def test_empty_text_content_is_allowed() -> None:
    message = Message(role=MessageRole.USER, content="")

    assert message.role is MessageRole.USER
    assert message.content == ""


def test_old_message_construction_and_equality_are_unchanged() -> None:
    positional = Message(MessageRole.USER, "hello")
    keyword = Message(role=MessageRole.USER, content="hello")

    assert positional == keyword
    assert positional.tool_calls == ()
    assert positional.tool_call_id is None


def test_message_is_immutable() -> None:
    message = Message(role=MessageRole.SYSTEM, content="rules")

    with pytest.raises(FrozenInstanceError):
        message.content = "tampered"  # type: ignore[misc]


def test_message_supports_assistant_tool_calls() -> None:
    call = ToolCall(call_id="call-1", name="read", arguments={"path": "a.txt"})
    message = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=(call,),
    )

    assert message.content == ""
    assert message.tool_calls == (call,)
    assert message.tool_call_id is None


def test_message_supports_tool_result_reference() -> None:
    message = Message(
        role=MessageRole.TOOL,
        content="read succeeded",
        tool_call_id="call-1",
    )

    assert message.role.value == "tool"
    assert message.tool_call_id == "call-1"
    assert message.tool_calls == ()


def test_message_rejects_invalid_tool_field_combinations() -> None:
    call = ToolCall(call_id="call-1", name="read", arguments={})

    with pytest.raises(ValueError):
        Message(role=MessageRole.SYSTEM, content="x", tool_call_id="call-1")
    with pytest.raises(ValueError):
        Message(role=MessageRole.USER, content="x", tool_calls=(call,))
    with pytest.raises(ValueError):
        Message(role=MessageRole.ASSISTANT, content="x", tool_call_id="call-1")
    with pytest.raises(ValueError):
        Message(role=MessageRole.TOOL, content="x")
    with pytest.raises(ValueError):
        Message(role=MessageRole.TOOL, content="x", tool_calls=(call,))


def test_message_tool_calls_must_be_a_tuple() -> None:
    call = ToolCall(call_id="call-1", name="read", arguments={})

    with pytest.raises(TypeError):
        Message(  # type: ignore[arg-type]
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[call],
        )


def test_message_validates_tool_field_types_and_ids() -> None:
    with pytest.raises(TypeError):
        Message(  # type: ignore[arg-type]
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=("not a call",),
        )
    with pytest.raises(TypeError, match="tool_call_id"):
        Message(  # type: ignore[arg-type]
            role=MessageRole.TOOL,
            content="result",
            tool_call_id=42,
        )
    with pytest.raises(ValueError, match="tool_call_id"):
        Message(role=MessageRole.TOOL, content="result", tool_call_id="   ")


def test_message_tool_fields_are_immutable() -> None:
    call = ToolCall(call_id="call-1", name="read", arguments={})
    message = Message(role=MessageRole.ASSISTANT, content="", tool_calls=(call,))

    with pytest.raises(FrozenInstanceError):
        message.tool_calls = ()  # type: ignore[misc]
