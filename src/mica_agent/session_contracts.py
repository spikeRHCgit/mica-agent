"""Provider-agnostic session data and per-record persistence contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TypeAlias

from .checkpoint_contracts import (
    RunCheckpoint,
    RunCheckpointKind,
    RunCheckpointProgress,
    RunCheckpointToolProgress,
    RunCheckpointToolStatus,
)
from .message_sanitizer import sanitize_presentation_text, sanitize_tool_message
from .models import Message, MessageRole
from .tool_contracts import ToolCall, to_mutable_json
from .tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
    _safe_relative_path,
)

# Current JSONL format version used for session writes.
CURRENT_SESSION_FORMAT_VERSION = 5

_SUPPORTED_SESSION_FORMAT_VERSIONS = frozenset(
    {1, 2, 3, 4, CURRENT_SESSION_FORMAT_VERSION}
)


class SessionError(Exception):
    """Base error the session subsystem allows callers to catch."""


class SessionFormatError(SessionError):
    """An untrusted persisted record does not match the current session format."""


class SessionRecordType(Enum):
    """Explicit wire record type of persisted records."""

    SESSION_META = "session_meta"
    TURN = "turn"
    RUN_CHECKPOINT = "run_checkpoint"


def _validate_non_blank_text(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a str")
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty and non-blank")


def _validate_utc_datetime(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")


@dataclass(frozen=True, slots=True, repr=False)
class SessionSelection:
    """Stable provider and model identity, independent of credentials and settings."""

    provider_id: str
    model_id: str

    def __post_init__(self) -> None:
        for name in ("provider_id", "model_id"):
            value = getattr(self, name)
            _validate_non_blank_text(value, name)
            if not value.isprintable():
                raise ValueError("selection must contain printable identifiers")

    def __repr__(self) -> str:
        return "SessionSelection(<redacted>)"


@dataclass(frozen=True, slots=True)
class SessionMeta:
    """Immutable identity and workspace metadata of one session."""

    session_id: str
    format_version: int
    workspace_id: str
    workspace_path: str
    title: str
    created_at: datetime
    updated_at: datetime
    selection: SessionSelection | None = None

    def __post_init__(self) -> None:
        _validate_non_blank_text(self.session_id, "session_id")
        if isinstance(self.format_version, bool) or not isinstance(
            self.format_version, int
        ):
            raise TypeError("format_version must be an int")
        if self.format_version not in _SUPPORTED_SESSION_FORMAT_VERSIONS:
            raise ValueError("format_version is not supported")
        if self.selection is not None:
            if not isinstance(self.selection, SessionSelection):
                raise TypeError("selection must be a SessionSelection or None")
            if self.format_version < 5:
                raise ValueError("selection requires format_version 5")
        _validate_non_blank_text(self.workspace_id, "workspace_id")
        _validate_non_blank_text(self.workspace_path, "workspace_path")
        if not isinstance(self.title, str):
            raise TypeError("title must be a str")
        if self.title and not self.title.strip():
            raise ValueError("title must be empty or contain non-whitespace text")
        _validate_utc_datetime(self.created_at, "created_at")
        _validate_utc_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")


@dataclass(frozen=True, slots=True)
class TurnEntry:
    """Complete, immutable turn produced by one successful user instruction."""

    turn_id: str
    run_id: str
    user_message: Message
    assistant_message: Message
    completed_at: datetime
    intermediate_messages: tuple[Message, ...] = ()
    tool_presentations: tuple[ToolPresentationSnapshot, ...] = ()

    def __post_init__(self) -> None:
        _validate_non_blank_text(self.turn_id, "turn_id")
        _validate_non_blank_text(self.run_id, "run_id")
        if not isinstance(self.user_message, Message):
            raise TypeError("user_message must be a Message")
        if self.user_message.role is not MessageRole.USER:
            raise ValueError("user_message role must be user")
        if not isinstance(self.assistant_message, Message):
            raise TypeError("assistant_message must be a Message")
        if self.assistant_message.role is not MessageRole.ASSISTANT:
            raise ValueError("assistant_message role must be assistant")
        if self.assistant_message.tool_calls:
            raise ValueError("final assistant message cannot carry tool_calls")
        if not isinstance(self.intermediate_messages, tuple):
            raise TypeError("intermediate_messages must be a tuple")
        if not all(
            isinstance(message, Message) for message in self.intermediate_messages
        ):
            raise TypeError("intermediate_messages must contain only Message values")
        self._validate_intermediate_messages()
        if not isinstance(self.tool_presentations, tuple):
            raise TypeError("tool_presentations must be a tuple")
        if not all(
            isinstance(presentation, ToolPresentationSnapshot)
            for presentation in self.tool_presentations
        ):
            raise TypeError(
                "tool_presentations must contain only ToolPresentationSnapshot values"
            )
        self._validate_tool_presentations()
        object.__setattr__(
            self,
            "intermediate_messages",
            tuple(
                sanitize_tool_message(message) for message in self.intermediate_messages
            ),
        )
        _validate_utc_datetime(self.completed_at, "completed_at")

    def _validate_intermediate_messages(self) -> None:
        index = 0
        messages = self.intermediate_messages
        while index < len(messages):
            assistant = messages[index]
            if assistant.role is not MessageRole.ASSISTANT or not assistant.tool_calls:
                raise ValueError(
                    "intermediate messages must start with assistant tool_calls"
                )
            index += 1
            for call in assistant.tool_calls:
                if index >= len(messages):
                    raise ValueError(
                        "assistant tool_calls require matching tool results"
                    )
                result = messages[index]
                if (
                    result.role is not MessageRole.TOOL
                    or result.tool_call_id != call.call_id
                ):
                    raise ValueError(
                        "tool result order must match assistant tool_calls"
                    )
                index += 1

    def _validate_tool_presentations(self) -> None:
        if not self.tool_presentations:
            return
        calls = tuple(
            call
            for message in self.intermediate_messages
            if message.role is MessageRole.ASSISTANT
            for call in message.tool_calls
        )
        if len(self.tool_presentations) != len(calls):
            raise ValueError("tool presentations must match tool calls")
        seen: set[str] = set()
        for call, presentation in zip(calls, self.tool_presentations, strict=True):
            if presentation.call_id in seen:
                raise ValueError("duplicate tool presentation call_id")
            if (
                presentation.call_id != call.call_id
                or presentation.tool_name != call.name
            ):
                raise ValueError("tool presentation identity must match tool call")
            expected_kind = {
                "read": ToolPresentationKind.READ,
                "write": ToolPresentationKind.WRITE,
                "edit": ToolPresentationKind.EDIT,
                "bash": ToolPresentationKind.BASH,
                "ask": ToolPresentationKind.ASK,
            }.get(call.name, ToolPresentationKind.GENERIC)
            if presentation.kind is not expected_kind:
                raise ValueError("tool presentation kind must match tool call")
            if presentation.status not in {
                ToolPresentationStatus.SUCCESS,
                ToolPresentationStatus.FAILED,
            }:
                raise ValueError("historical tool presentation must be terminal")
            seen.add(presentation.call_id)

    @property
    def context_messages(self) -> tuple[Message, ...]:
        """Return every message of this turn in model context order."""
        return (
            self.user_message,
            *self.intermediate_messages,
            self.assistant_message,
        )


# Business record union for the session timeline.
SessionEntry: TypeAlias = TurnEntry | RunCheckpoint

# Strongly typed record union readable and writable by the store.
SessionRecord: TypeAlias = SessionMeta | SessionEntry


def _format_utc_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _require_exact_fields(
    record: Mapping[object, object], expected: frozenset[str], record_name: str
) -> None:
    actual = set(record)
    missing = expected - actual
    if missing:
        fields = ", ".join(sorted(missing))
        raise SessionFormatError(f"{record_name} record is missing fields: {fields}")
    if actual - expected:
        raise SessionFormatError(f"{record_name} record contains unexpected fields")


def _require_record_text(record: Mapping[object, object], field_name: str) -> str:
    value = record[field_name]
    if not isinstance(value, str):
        raise SessionFormatError(f"{field_name} must be a string")
    return value


def _require_non_blank_record_text(
    record: Mapping[object, object], field_name: str
) -> str:
    value = _require_record_text(record, field_name)
    if not value.strip():
        raise SessionFormatError(f"{field_name} must be non-empty and non-blank")
    return value


def _require_supported_version(record: Mapping[object, object]) -> int:
    if "format_version" not in record:
        raise SessionFormatError("session record is missing field: format_version")
    version = record["format_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise SessionFormatError("format_version must be an integer")
    if version not in _SUPPORTED_SESSION_FORMAT_VERSIONS:
        raise SessionFormatError("unsupported format_version")
    return version


def _parse_utc_datetime(record: Mapping[object, object], field_name: str) -> datetime:
    value = _require_record_text(record, field_name)
    try:
        parsed = datetime.fromisoformat(value)
        _validate_utc_datetime(parsed, field_name)
    except (TypeError, ValueError):
        raise SessionFormatError(
            f"{field_name} must be a valid UTC timestamp"
        ) from None
    return parsed.astimezone(UTC)


def _parse_message(
    record: Mapping[object, object], field_name: str, expected_role: MessageRole
) -> Message:
    value = record[field_name]
    if not isinstance(value, Mapping):
        raise SessionFormatError(f"{field_name} must be a JSON object")
    _require_exact_fields(value, frozenset({"role", "content"}), field_name)
    role = _require_record_text(value, "role")
    if role != expected_role.value:
        raise SessionFormatError(f"{field_name}.role is invalid")
    content = _require_record_text(value, "content")
    return Message(role=expected_role, content=content)


def _serialize_v2_message(message: Message) -> dict[str, object]:
    result: dict[str, object] = {
        "role": message.role.value,
        "content": message.content,
    }
    if message.role is MessageRole.ASSISTANT and message.tool_calls:
        result["tool_calls"] = [
            {
                "call_id": call.call_id,
                "name": call.name,
                "arguments": to_mutable_json(call.arguments),
            }
            for call in message.tool_calls
        ]
    elif message.role is MessageRole.TOOL:
        result["tool_call_id"] = message.tool_call_id
    return result


def _parse_v2_message(value: object) -> Message:
    if not isinstance(value, Mapping):
        raise SessionFormatError("message must be a JSON object")
    role = value.get("role")
    if role in {MessageRole.USER.value, MessageRole.SYSTEM.value}:
        _require_exact_fields(value, frozenset({"role", "content"}), "message")
        return Message(MessageRole(role), _require_record_text(value, "content"))
    if role == MessageRole.TOOL.value:
        _require_exact_fields(
            value, frozenset({"role", "content", "tool_call_id"}), "message"
        )
        return Message(
            MessageRole.TOOL,
            _require_record_text(value, "content"),
            tool_call_id=_require_record_text(value, "tool_call_id"),
        )
    if role != MessageRole.ASSISTANT.value:
        raise SessionFormatError("message role is invalid")
    allowed = frozenset({"role", "content", "tool_calls"})
    actual = set(value)
    if actual == {"role", "content"}:
        return Message(MessageRole.ASSISTANT, _require_record_text(value, "content"))
    _require_exact_fields(value, allowed, "message")
    calls_value = value["tool_calls"]
    if not isinstance(calls_value, list) or not calls_value:
        raise SessionFormatError("tool_calls must be a non-empty array")
    calls: list[ToolCall] = []
    for call_value in calls_value:
        if not isinstance(call_value, Mapping):
            raise SessionFormatError("tool_call must be a JSON object")
        _require_exact_fields(
            call_value,
            frozenset({"call_id", "name", "arguments"}),
            "tool_call",
        )
        arguments = call_value["arguments"]
        if not isinstance(arguments, Mapping):
            raise SessionFormatError("tool_call arguments must be a JSON object")
        try:
            calls.append(
                ToolCall(
                    _require_record_text(call_value, "call_id"),
                    _require_record_text(call_value, "name"),
                    arguments,
                )
            )
        except (TypeError, ValueError):
            raise SessionFormatError("tool_call is invalid") from None
    return Message(
        MessageRole.ASSISTANT,
        _require_record_text(value, "content"),
        tuple(calls),
    )


def _serialize_tool_presentation(
    presentation: ToolPresentationSnapshot,
) -> dict[str, object]:
    return {
        "call_id": presentation.call_id,
        "tool_name": presentation.tool_name,
        "kind": presentation.kind.value,
        "status": presentation.status.value,
        "payload": to_mutable_json(presentation.payload),
        "message": presentation.message,
    }


def _require_safe_presentation_text(
    value: object, field_name: str, *, allow_blank: bool = True
) -> str:
    text = _require_record_text({field_name: value}, field_name)
    if not allow_blank and not text.strip():
        raise SessionFormatError(f"{field_name} must be non-empty and non-blank")
    if sanitize_presentation_text(text) != text:
        raise SessionFormatError(f"{field_name} contains unsafe text")
    return text


def _parse_presentation_line(
    value: object, *, status: ToolPresentationStatus
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise SessionFormatError("presentation line must be a JSON object")
    _require_exact_fields(
        value,
        frozenset({"kind", "text", "old_line", "new_line"}),
        "presentation line",
    )
    kind = _require_record_text(value, "kind")
    if kind not in {"context", "removed", "added"}:
        raise SessionFormatError("presentation line kind is invalid")
    text = _require_safe_presentation_text(value["text"], "text")
    old_line = value["old_line"]
    new_line = value["new_line"]
    for line_number, field_name in ((old_line, "old_line"), (new_line, "new_line")):
        if line_number is not None and (
            isinstance(line_number, bool)
            or not isinstance(line_number, int)
            or line_number <= 0
        ):
            raise SessionFormatError(f"{field_name} must be a positive integer or null")
    if kind == "context" and (old_line is None or new_line is None):
        raise SessionFormatError("context line numbers are required")
    if kind == "removed" and new_line is not None:
        raise SessionFormatError("removed line numbers are invalid")
    if (
        kind == "removed"
        and old_line is None
        and status is ToolPresentationStatus.SUCCESS
    ):
        raise SessionFormatError("removed line number is required")
    if kind == "added" and old_line is not None:
        raise SessionFormatError("added line numbers are invalid")
    if (
        kind == "added"
        and new_line is None
        and status is ToolPresentationStatus.SUCCESS
    ):
        raise SessionFormatError("added line number is required")
    return {
        "kind": kind,
        "text": text,
        "old_line": old_line,
        "new_line": new_line,
    }


def _parse_tool_presentation(value: object) -> ToolPresentationSnapshot:
    if not isinstance(value, Mapping):
        raise SessionFormatError("tool presentation must be a JSON object")
    _require_exact_fields(
        value,
        frozenset({"call_id", "tool_name", "kind", "status", "payload", "message"}),
        "tool presentation",
    )
    call_id = _require_non_blank_record_text(value, "call_id")
    tool_name = _require_non_blank_record_text(value, "tool_name")
    kind_value = _require_record_text(value, "kind")
    status_value = _require_record_text(value, "status")
    try:
        kind = ToolPresentationKind(kind_value)
        status = ToolPresentationStatus(status_value)
    except ValueError:
        raise SessionFormatError(
            "tool presentation kind or status is invalid"
        ) from None
    payload_value = value["payload"]
    if not isinstance(payload_value, Mapping):
        raise SessionFormatError("tool presentation payload must be an object")
    if not all(isinstance(key, str) for key in payload_value):
        raise SessionFormatError("tool presentation payload keys must be strings")
    message_value = value["message"]
    if message_value is not None:
        message_value = _require_safe_presentation_text(
            message_value, "message", allow_blank=False
        )
    expected: frozenset[str]
    if kind is ToolPresentationKind.READ:
        expected = frozenset({"path"})
    elif kind is ToolPresentationKind.WRITE:
        expected = frozenset({"path", "content"})
    elif kind is ToolPresentationKind.EDIT:
        expected = frozenset({"path", "matched", "lines"})
    elif kind is ToolPresentationKind.BASH:
        expected = frozenset({"command", "output", "exit_code"})
    elif kind is ToolPresentationKind.ASK:
        expected = frozenset({"question", "answer"})
    else:
        expected = frozenset()
    _require_exact_fields(payload_value, expected, "tool presentation payload")
    payload: dict[str, object] = {}
    if kind in {
        ToolPresentationKind.READ,
        ToolPresentationKind.WRITE,
        ToolPresentationKind.EDIT,
    }:
        path = payload_value["path"]
        if path != "" and _safe_relative_path(path) is None:
            raise SessionFormatError("tool presentation path is invalid")
        payload["path"] = path
    if kind is ToolPresentationKind.WRITE:
        payload["content"] = _require_safe_presentation_text(
            payload_value["content"], "content"
        )
    elif kind is ToolPresentationKind.EDIT:
        matched = payload_value["matched"]
        if not isinstance(matched, bool):
            raise SessionFormatError("presentation matched must be a boolean")
        lines_value = payload_value["lines"]
        if not isinstance(lines_value, list):
            raise SessionFormatError("presentation lines must be an array")
        payload["matched"] = matched
        payload["lines"] = [
            _parse_presentation_line(line, status=status) for line in lines_value
        ]
    elif kind is ToolPresentationKind.BASH:
        payload["command"] = _require_safe_presentation_text(
            payload_value["command"], "command"
        )
        payload["output"] = _require_safe_presentation_text(
            payload_value["output"], "output"
        )
        exit_code = payload_value["exit_code"]
        if exit_code is not None and (
            isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
            or exit_code < 0
        ):
            raise SessionFormatError(
                "presentation exit_code must be a non-negative integer or null"
            )
        payload["exit_code"] = exit_code
    elif kind is ToolPresentationKind.ASK:
        payload["question"] = _require_safe_presentation_text(
            payload_value["question"], "question"
        )
        payload["answer"] = _require_safe_presentation_text(
            payload_value["answer"], "answer"
        )
    try:
        return ToolPresentationSnapshot(
            call_id=call_id,
            tool_name=tool_name,
            kind=kind,
            status=status,
            payload=payload,
            message=message_value,
        )
    except (TypeError, ValueError):
        raise SessionFormatError("tool presentation is invalid") from None


def _serialize_checkpoint_tool(
    item: RunCheckpointToolProgress,
) -> dict[str, object]:
    return {
        "call": _serialize_v2_message(Message(MessageRole.ASSISTANT, "", (item.call,)))[
            "tool_calls"
        ][0],
        "status": item.status.value,
        "result": None if item.result is None else _serialize_v2_message(item.result),
        "presentation": (
            None
            if item.presentation is None
            else _serialize_tool_presentation(item.presentation)
        ),
    }


def _serialize_checkpoint_progress(
    step: RunCheckpointProgress,
) -> dict[str, object]:
    return {
        "assistant_message": (
            None
            if step.assistant_message is None
            else _serialize_v2_message(step.assistant_message)
        ),
        "tools": [_serialize_checkpoint_tool(item) for item in step.tools],
    }


def _parse_checkpoint_tool(value: object) -> RunCheckpointToolProgress:
    if not isinstance(value, Mapping):
        raise SessionFormatError("checkpoint tool progress must be a JSON object")
    _require_exact_fields(
        value,
        frozenset({"call", "status", "result", "presentation"}),
        "checkpoint tool progress",
    )
    call_value = value["call"]
    if not isinstance(call_value, Mapping):
        raise SessionFormatError("checkpoint tool call must be a JSON object")
    _require_exact_fields(
        call_value, frozenset({"call_id", "name", "arguments"}), "checkpoint tool call"
    )
    arguments = call_value["arguments"]
    if not isinstance(arguments, Mapping):
        raise SessionFormatError("checkpoint tool arguments must be an object")
    try:
        call = ToolCall(
            _require_non_blank_record_text(call_value, "call_id"),
            _require_non_blank_record_text(call_value, "name"),
            arguments,
        )
    except (TypeError, ValueError):
        raise SessionFormatError("checkpoint tool call is invalid") from None
    status_value = _require_record_text(value, "status")
    try:
        status = RunCheckpointToolStatus(status_value)
    except ValueError:
        raise SessionFormatError("checkpoint tool status is invalid") from None
    result_value = value["result"]
    result = None
    if result_value is not None:
        result = _parse_v2_message(result_value)
    presentation_value = value["presentation"]
    presentation = (
        None
        if presentation_value is None
        else _parse_tool_presentation(presentation_value)
    )
    try:
        return RunCheckpointToolProgress(call, status, result, presentation)
    except (TypeError, ValueError):
        raise SessionFormatError("checkpoint tool progress is invalid") from None


def _parse_checkpoint_progress(value: object) -> RunCheckpointProgress:
    if not isinstance(value, Mapping):
        raise SessionFormatError("checkpoint progress must be a JSON object")
    _require_exact_fields(
        value, frozenset({"assistant_message", "tools"}), "checkpoint progress"
    )
    assistant_value = value["assistant_message"]
    assistant = None
    if assistant_value is not None:
        if not isinstance(assistant_value, Mapping):
            raise SessionFormatError("checkpoint assistant message must be an object")
        _require_exact_fields(
            assistant_value,
            frozenset({"role", "content"}),
            "checkpoint assistant message",
        )
        if _require_record_text(assistant_value, "role") != MessageRole.ASSISTANT.value:
            raise SessionFormatError("checkpoint assistant message role is invalid")
        assistant = Message(
            MessageRole.ASSISTANT, _require_record_text(assistant_value, "content")
        )
    tools_value = value["tools"]
    if not isinstance(tools_value, list):
        raise SessionFormatError("checkpoint tools must be an array")
    try:
        return RunCheckpointProgress(
            assistant_message=assistant,
            tools=tuple(_parse_checkpoint_tool(item) for item in tools_value),
        )
    except (TypeError, ValueError):
        raise SessionFormatError("checkpoint progress is invalid") from None


def _parse_checkpoint(record: Mapping[object, object]) -> RunCheckpoint:
    expected = frozenset(
        {
            "record_type",
            "format_version",
            "checkpoint_id",
            "run_id",
            "kind",
            "user_message",
            "progress",
            "partial_assistant_message",
            "recorded_at",
        }
    )
    _require_exact_fields(record, expected, "run_checkpoint")
    if _require_supported_version(record) not in {4, 5}:
        raise SessionFormatError("run_checkpoint requires format_version 4 or 5")
    try:
        kind = RunCheckpointKind(_require_record_text(record, "kind"))
    except ValueError:
        raise SessionFormatError("checkpoint kind is invalid") from None
    progress_value = record["progress"]
    if not isinstance(progress_value, list):
        raise SessionFormatError("checkpoint progress must be an array")
    partial_value = record["partial_assistant_message"]
    partial = None
    if partial_value is not None:
        if not isinstance(partial_value, Mapping):
            raise SessionFormatError("partial assistant message must be an object")
        _require_exact_fields(
            partial_value, frozenset({"role", "content"}), "partial assistant message"
        )
        if _require_record_text(partial_value, "role") != MessageRole.ASSISTANT.value:
            raise SessionFormatError("partial assistant message role is invalid")
        partial = Message(
            MessageRole.ASSISTANT, _require_record_text(partial_value, "content")
        )
    try:
        return RunCheckpoint(
            checkpoint_id=_require_non_blank_record_text(record, "checkpoint_id"),
            run_id=_require_non_blank_record_text(record, "run_id"),
            kind=kind,
            user_message=_parse_message(record, "user_message", MessageRole.USER),
            progress=tuple(_parse_checkpoint_progress(item) for item in progress_value),
            partial_assistant_message=partial,
            recorded_at=_parse_utc_datetime(record, "recorded_at"),
        )
    except SessionFormatError:
        raise
    except (TypeError, ValueError):
        raise SessionFormatError("run checkpoint is invalid") from None


def serialize_session_record(record: SessionRecord) -> dict[str, object]:
    """Convert one strongly typed session object into a plain-JSON record."""
    if isinstance(record, SessionMeta):
        return {
            "record_type": SessionRecordType.SESSION_META.value,
            "format_version": CURRENT_SESSION_FORMAT_VERSION,
            "session_id": record.session_id,
            "workspace_id": record.workspace_id,
            "workspace_path": record.workspace_path,
            "title": record.title,
            "created_at": _format_utc_datetime(record.created_at),
            "updated_at": _format_utc_datetime(record.updated_at),
            **(
                {}
                if record.selection is None
                else {
                    "provider_id": record.selection.provider_id,
                    "model_id": record.selection.model_id,
                }
            ),
        }
    if isinstance(record, TurnEntry):
        return {
            "record_type": SessionRecordType.TURN.value,
            "format_version": CURRENT_SESSION_FORMAT_VERSION,
            "turn_id": record.turn_id,
            "run_id": record.run_id,
            "messages": [
                _serialize_v2_message(message) for message in record.context_messages
            ],
            "tool_presentations": [
                _serialize_tool_presentation(presentation)
                for presentation in record.tool_presentations
            ],
            "completed_at": _format_utc_datetime(record.completed_at),
        }
    if isinstance(record, RunCheckpoint):
        return {
            "record_type": SessionRecordType.RUN_CHECKPOINT.value,
            "format_version": CURRENT_SESSION_FORMAT_VERSION,
            "checkpoint_id": record.checkpoint_id,
            "run_id": record.run_id,
            "kind": record.kind.value,
            "user_message": _serialize_v2_message(record.user_message),
            "progress": [
                _serialize_checkpoint_progress(step) for step in record.progress
            ],
            "partial_assistant_message": (
                None
                if record.partial_assistant_message is None
                else _serialize_v2_message(record.partial_assistant_message)
            ),
            "recorded_at": _format_utc_datetime(record.recorded_at),
        }
    raise TypeError("record must be a SessionMeta, TurnEntry, or RunCheckpoint")


def deserialize_session_record(record: object) -> SessionRecord:
    """Parse one untrusted JSON-compatible record into a strongly typed session object."""
    if not isinstance(record, Mapping):
        raise SessionFormatError("session record must be a JSON object")
    if "record_type" not in record:
        raise SessionFormatError("session record is missing field: record_type")

    record_type = record["record_type"]
    if record_type == SessionRecordType.SESSION_META.value:
        expected = frozenset(
            {
                "record_type",
                "format_version",
                "session_id",
                "workspace_id",
                "workspace_path",
                "title",
                "created_at",
                "updated_at",
            }
        )
        version = _require_supported_version(record)
        selection = None
        if version == 5 and ("provider_id" in record or "model_id" in record):
            expected = expected | {"provider_id", "model_id"}
            _require_exact_fields(record, expected, "session_meta")
            try:
                selection = SessionSelection(
                    _require_non_blank_record_text(record, "provider_id"),
                    _require_non_blank_record_text(record, "model_id"),
                )
            except (TypeError, ValueError):
                raise SessionFormatError("session selection is invalid") from None
        _require_exact_fields(record, expected, "session_meta")
        try:
            return SessionMeta(
                session_id=_require_record_text(record, "session_id"),
                format_version=_require_supported_version(record),
                workspace_id=_require_record_text(record, "workspace_id"),
                workspace_path=_require_record_text(record, "workspace_path"),
                title=_require_record_text(record, "title"),
                created_at=_parse_utc_datetime(record, "created_at"),
                updated_at=_parse_utc_datetime(record, "updated_at"),
                selection=selection,
            )
        except (TypeError, ValueError) as exc:
            raise SessionFormatError(str(exc)) from None
    if record_type == SessionRecordType.TURN.value:
        version = _require_supported_version(record)
        if version == 1:
            expected = frozenset(
                {
                    "record_type",
                    "format_version",
                    "turn_id",
                    "run_id",
                    "user_message",
                    "assistant_message",
                    "completed_at",
                }
            )
            _require_exact_fields(record, expected, "turn")
            try:
                return TurnEntry(
                    turn_id=_require_record_text(record, "turn_id"),
                    run_id=_require_record_text(record, "run_id"),
                    user_message=_parse_message(
                        record, "user_message", MessageRole.USER
                    ),
                    assistant_message=_parse_message(
                        record, "assistant_message", MessageRole.ASSISTANT
                    ),
                    completed_at=_parse_utc_datetime(record, "completed_at"),
                )
            except (TypeError, ValueError) as exc:
                raise SessionFormatError(str(exc)) from None
        if version == 2:
            expected = frozenset(
                {
                    "record_type",
                    "format_version",
                    "turn_id",
                    "run_id",
                    "messages",
                    "completed_at",
                }
            )
            _require_exact_fields(record, expected, "turn")
            messages_value = record["messages"]
            if not isinstance(messages_value, list):
                raise SessionFormatError("messages must be an array")
            turn_id = _require_non_blank_record_text(record, "turn_id")
            run_id = _require_non_blank_record_text(record, "run_id")
            try:
                messages = tuple(_parse_v2_message(value) for value in messages_value)
                if len(messages) < 2:
                    raise ValueError(
                        "turn messages must contain user and final assistant"
                    )
                return TurnEntry(
                    turn_id=turn_id,
                    run_id=run_id,
                    user_message=messages[0],
                    assistant_message=messages[-1],
                    completed_at=_parse_utc_datetime(record, "completed_at"),
                    intermediate_messages=messages[1:-1],
                )
            except SessionFormatError:
                raise
            except (TypeError, ValueError):
                raise SessionFormatError("turn messages are invalid") from None

        expected = frozenset(
            {
                "record_type",
                "format_version",
                "turn_id",
                "run_id",
                "messages",
                "tool_presentations",
                "completed_at",
            }
        )
        _require_exact_fields(record, expected, "turn")
        messages_value = record["messages"]
        presentations_value = record["tool_presentations"]
        if not isinstance(messages_value, list):
            raise SessionFormatError("messages must be an array")
        if not isinstance(presentations_value, list):
            raise SessionFormatError("tool_presentations must be an array")
        turn_id = _require_non_blank_record_text(record, "turn_id")
        run_id = _require_non_blank_record_text(record, "run_id")
        try:
            messages = tuple(_parse_v2_message(value) for value in messages_value)
            presentations = tuple(
                _parse_tool_presentation(value) for value in presentations_value
            )
            if len(messages) < 2:
                raise ValueError("turn messages must contain user and final assistant")
            has_tool_calls = any(
                message.role is MessageRole.ASSISTANT and message.tool_calls
                for message in messages
            )
            # A v1/v2 turn rewritten during an atomic upgrade has no historical
            # presentation data.  The wire cannot distinguish that case from a
            # newly authored empty list, so preserve the explicit compatibility
            # exception while rejecting every non-empty mismatch below.
            if not has_tool_calls and presentations:
                raise ValueError("plain turns cannot contain tool presentations")
            return TurnEntry(
                turn_id=turn_id,
                run_id=run_id,
                user_message=messages[0],
                assistant_message=messages[-1],
                completed_at=_parse_utc_datetime(record, "completed_at"),
                intermediate_messages=messages[1:-1],
                tool_presentations=presentations,
            )
        except SessionFormatError:
            raise
        except (TypeError, ValueError):
            raise SessionFormatError(
                "turn messages or tool presentations are invalid"
            ) from None
    if record_type == SessionRecordType.RUN_CHECKPOINT.value:
        return _parse_checkpoint(record)
    raise SessionFormatError("unknown session record type")
