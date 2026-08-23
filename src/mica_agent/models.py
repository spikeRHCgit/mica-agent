"""供应商无关的标准消息模型（阶段计划 4.1）。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .tool_contracts import ToolCall


class MessageRole(Enum):
    """标准消息角色；枚举值即对外传输格式的 wire 值。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class Message:
    """一条供应商无关的标准消息。

    旧纯文本构造保持为 ``Message(role, content)``。助手消息可携带
    完整工具调用，工具消息通过 ``tool_call_id`` 关联调用；所有集合
    使用不可变元组，且不保存 Provider 私有对象。
    """

    role: MessageRole
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.role, MessageRole):
            raise TypeError(
                f"role must be a MessageRole, got {type(self.role).__name__}"
            )
        if not isinstance(self.content, str):
            raise TypeError(f"content must be a str, got {type(self.content).__name__}")
        if not isinstance(self.tool_calls, tuple):
            raise TypeError("tool_calls must be a tuple")
        if not all(isinstance(call, ToolCall) for call in self.tool_calls):
            raise TypeError("every item in tool_calls must be a ToolCall")
        if self.tool_call_id is not None and not isinstance(self.tool_call_id, str):
            raise TypeError("tool_call_id must be a str or None")

        if self.role in (MessageRole.SYSTEM, MessageRole.USER):
            if self.tool_calls or self.tool_call_id is not None:
                raise ValueError("system and user messages cannot carry tool fields")
        elif self.role is MessageRole.ASSISTANT:
            if self.tool_call_id is not None:
                raise ValueError("assistant messages cannot carry tool_call_id")
        elif self.role is MessageRole.TOOL:
            if self.tool_calls:
                raise ValueError("tool messages cannot carry tool_calls")
            if self.tool_call_id is None or not self.tool_call_id.strip():
                raise ValueError("tool messages require a non-blank tool_call_id")
