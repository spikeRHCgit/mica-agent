"""工具上下文消息的集中、纯函数凭据清洗边界。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from .models import Message, MessageRole
from .tool_contracts import ToolCall, to_mutable_json

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "access_token",
        "secret",
        "password",
        "authorization",
    }
)
_SK_CREDENTIAL = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{4,}")
_BEARER_CREDENTIAL = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def _redact_text(value: str) -> str:
    return _BEARER_CREDENTIAL.sub(
        REDACTED,
        _SK_CREDENTIAL.sub(REDACTED, value),
    )


def _redact_json(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, Mapping):
        return {
            key: REDACTED if key.lower() in _SENSITIVE_KEYS else _redact_json(item)
            for key, item in value.items()
        }
    raise TypeError("tool message contains an invalid JSON value")


def _reject_json_constant(value: str) -> object:
    raise ValueError("invalid JSON constant")


def _redact_tool_content(content: str) -> str:
    try:
        value = json.loads(content, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        return _redact_text(content)
    return json.dumps(
        _redact_json(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def sanitize_tool_message(message: Message) -> Message:
    """复制并清洗 assistant tool-call 或 TOOL 消息。"""
    if not isinstance(message, Message):
        raise TypeError("message must be a Message")
    if message.role is MessageRole.ASSISTANT and message.tool_calls:
        return Message(
            MessageRole.ASSISTANT,
            message.content,
            tuple(
                ToolCall(
                    call.call_id,
                    call.name,
                    _redact_json(to_mutable_json(call.arguments)),
                )
                for call in message.tool_calls
            ),
        )
    if message.role is MessageRole.TOOL:
        return Message(
            MessageRole.TOOL,
            _redact_tool_content(message.content),
            tool_call_id=message.tool_call_id,
        )
    raise ValueError("message must be tool-related")


__all__ = ["REDACTED", "sanitize_tool_message"]
