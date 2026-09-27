"""Provider-agnostic standard message model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .tool_contracts import ToolCall


class MessageRole(Enum):
    """Standard message role; enum values are the wire values on the transport."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class Message:
    """One provider-agnostic standard message.

    Plain-text construction stays ``Message(role, content)``. Assistant messages may
    carry complete tool calls, tool messages reference the call through
    ``tool_call_id``; every collection is an immutable tuple and no provider-private
    object is stored.
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
