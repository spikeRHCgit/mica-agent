"""Standard Message 公共契约测试。

验证阶段计划 4.1：供应商无关的文本消息，支持 system/user/assistant
三种角色，消息对象对外不可变。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mica_agent.models import Message, MessageRole


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
    ],
)
def test_message_role_wire_values(role: MessageRole, wire_value: str) -> None:
    assert role.value == wire_value


def test_three_roles_are_distinct() -> None:
    assert len({MessageRole.SYSTEM, MessageRole.USER, MessageRole.ASSISTANT}) == 3


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


def test_message_is_immutable() -> None:
    message = Message(role=MessageRole.SYSTEM, content="rules")

    with pytest.raises(FrozenInstanceError):
        message.content = "tampered"  # type: ignore[misc]
