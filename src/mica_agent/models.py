"""供应商无关的标准消息模型（阶段计划 4.1）。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MessageRole(Enum):
    """标准消息角色；枚举值即对外传输格式的 wire 值。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Message:
    """一条供应商无关的文本消息。

    不可变：Engine 和 Provider 都无法在创建后修改消息历史。
    当前阶段只承载文本 content，不包含工具、图像或多模态字段。
    空文本 content 允许（不要求非空），但必须是 ``str``。
    """

    role: MessageRole
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, MessageRole):
            raise TypeError(
                f"role must be a MessageRole, got {type(self.role).__name__}"
            )
        if not isinstance(self.content, str):
            raise TypeError(f"content must be a str, got {type(self.content).__name__}")
