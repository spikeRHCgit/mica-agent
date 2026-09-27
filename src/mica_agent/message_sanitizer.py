"""Central pure-function credential sanitization boundary for tool context messages."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from itertools import chain

from .models import Message, MessageRole
from .tool_contracts import ToolCall, to_mutable_json

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "api-key",
        "apikey",
        "token",
        "access_token",
        "access-token",
        "secret",
        "password",
        "authorization",
        "client_secret",
        "private_key",
    }
)
_SK_CREDENTIAL = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{4,}")
_BEARER_CREDENTIAL = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SENSITIVE_KEY = re.compile(
    r"\b(api[_-]?key|apikey|token|access[_-]?token|secret|password|authorization|"
    r"client[_-]?secret|private[_-]?key)\b",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\)[^\s\"']+"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_])/(?!/)[^\s\"']+")
_PATH_REDACTION = "[REDACTED_PATH]"
_HTTP_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _value_start(value: str, match: re.Match[str]) -> int | None:
    key_start, key_end = match.span()
    index = key_end
    if (
        key_start >= 2
        and value[key_start - 2] == "\\"
        and value[key_start - 1] in {'"', "'"}
    ):
        wrapper = value[key_start - 2 : key_start]
        if value[key_end : key_end + 2] != wrapper:
            return None
        index += 2
    elif key_start >= 1 and value[key_start - 1] in {'"', "'"}:
        wrapper = value[key_start - 1]
        if value[key_end : key_end + 1] != wrapper:
            return None
        index += 1
    while index < len(value) and value[index].isspace():
        index += 1
    if index >= len(value) or value[index] not in {":", "="}:
        return None
    index += 1
    while index < len(value) and value[index].isspace():
        index += 1
    return index


def _quoted_value_end(value: str, start: int, delimiter: str) -> int:
    quote = delimiter[-1]
    index = start + len(delimiter)
    while index < len(value):
        if value[index] != quote:
            index += 1
            continue
        backslashes = 0
        before = index - 1
        while before >= 0 and value[before] == "\\":
            backslashes += 1
            before -= 1
        if (len(delimiter) == 1 and backslashes % 2 == 0) or (
            len(delimiter) == 2 and backslashes == 1
        ):
            return index - 1 if len(delimiter) == 2 else index
        index += 1
    return len(value)


def _credential_value_span(value: str, start: int) -> tuple[int, int] | None:
    if start >= len(value):
        return None
    if value.startswith(REDACTED, start):
        return start, start + len(REDACTED)
    if value[start : start + 2] in {'\\"', "\\'"}:
        delimiter = value[start : start + 2]
        return start + 2, _quoted_value_end(value, start, delimiter)
    if value[start] in {'"', "'"}:
        return start + 1, _quoted_value_end(value, start, value[start])
    end = start
    while end < len(value) and value[end] not in "\r\n,;&|}]":
        end += 1
    value_end = end
    while value_end > start and value[value_end - 1] in " \t\f\v":
        value_end -= 1
    return (start, value_end) if value_end > start else None


def _redact_assignments(value: str) -> str:
    parts: list[str] = []
    cursor = 0
    search_from = 0
    while match := _SENSITIVE_KEY.search(value, search_from):
        start = _value_start(value, match)
        span = _credential_value_span(value, start) if start is not None else None
        if span is None:
            search_from = match.end()
            continue
        value_start, value_end = span
        if value[value_start:value_end] == REDACTED:
            search_from = value_end
            continue
        parts.extend((value[cursor:value_start], REDACTED))
        cursor = value_end
        search_from = value_end
    if not parts:
        return value
    parts.append(value[cursor:])
    return "".join(parts)


def _redact_local_paths(value: str) -> str:
    url_spans = [match.span() for match in _HTTP_URL.finditer(value)]
    parts: list[str] = []
    cursor = 0
    path_matches = chain(
        _WINDOWS_ABSOLUTE_PATH.finditer(value),
        _POSIX_ABSOLUTE_PATH.finditer(value),
    )
    for match in sorted(path_matches, key=lambda item: item.start()):
        if match.start() < cursor or any(
            start <= match.start() < end for start, end in url_spans
        ):
            continue
        parts.extend((value[cursor : match.start()], _PATH_REDACTION))
        cursor = match.end()
    if not parts:
        return value
    parts.append(value[cursor:])
    return "".join(parts)


def _redact_text(value: str) -> str:
    value = _BEARER_CREDENTIAL.sub(
        REDACTED,
        _SK_CREDENTIAL.sub(REDACTED, _redact_assignments(value)),
    )
    return _redact_local_paths(value)


def sanitize_presentation_text(value: str) -> str:
    """Sanitize a text copy that enters tool presentation events or the session."""
    if not isinstance(value, str):
        raise TypeError("value must be a str")
    return _redact_text(value)


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
    """Copy and sanitize an assistant tool-call or TOOL message."""
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


__all__ = ["REDACTED", "sanitize_presentation_text", "sanitize_tool_message"]
