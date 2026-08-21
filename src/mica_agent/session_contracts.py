"""供应商无关的会话数据与单条持久化记录契约。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TypeAlias

from .models import Message, MessageRole

CURRENT_SESSION_FORMAT_VERSION = 1
"""当前会话 JSONL 格式版本。"""


class SessionError(Exception):
    """会话子系统可公开捕获的基础错误。"""


class SessionFormatError(SessionError):
    """不可信持久化记录不符合当前会话格式。"""


class SessionRecordType(Enum):
    """持久化记录的显式 wire 类型。"""

    SESSION_META = "session_meta"
    TURN = "turn"


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


@dataclass(frozen=True, slots=True)
class SessionMeta:
    """一个会话的不可变身份与工作区元数据。"""

    session_id: str
    format_version: int
    workspace_id: str
    workspace_path: str
    title: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _validate_non_blank_text(self.session_id, "session_id")
        if isinstance(self.format_version, bool) or not isinstance(
            self.format_version, int
        ):
            raise TypeError("format_version must be an int")
        if self.format_version != CURRENT_SESSION_FORMAT_VERSION:
            raise ValueError("format_version is not supported")
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
    """一次成功用户指令对应的完整、不可变轮次。"""

    turn_id: str
    run_id: str
    user_message: Message
    assistant_message: Message
    completed_at: datetime

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
        _validate_utc_datetime(self.completed_at, "completed_at")


SessionRecord: TypeAlias = SessionMeta | TurnEntry
"""Store 可读写的当前强类型记录联合。"""


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


def _require_current_version(record: Mapping[object, object]) -> int:
    version = record["format_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise SessionFormatError("format_version must be an integer")
    if version != CURRENT_SESSION_FORMAT_VERSION:
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


def serialize_session_record(record: SessionRecord) -> dict[str, object]:
    """把一个强类型会话对象转换为标准 JSON 可表示的单条记录。"""
    if isinstance(record, SessionMeta):
        return {
            "record_type": SessionRecordType.SESSION_META.value,
            "format_version": record.format_version,
            "session_id": record.session_id,
            "workspace_id": record.workspace_id,
            "workspace_path": record.workspace_path,
            "title": record.title,
            "created_at": _format_utc_datetime(record.created_at),
            "updated_at": _format_utc_datetime(record.updated_at),
        }
    if isinstance(record, TurnEntry):
        return {
            "record_type": SessionRecordType.TURN.value,
            "format_version": CURRENT_SESSION_FORMAT_VERSION,
            "turn_id": record.turn_id,
            "run_id": record.run_id,
            "user_message": {
                "role": record.user_message.role.value,
                "content": record.user_message.content,
            },
            "assistant_message": {
                "role": record.assistant_message.role.value,
                "content": record.assistant_message.content,
            },
            "completed_at": _format_utc_datetime(record.completed_at),
        }
    raise TypeError("record must be a SessionMeta or TurnEntry")


def deserialize_session_record(record: object) -> SessionRecord:
    """把一条不可信 JSON 兼容记录解析为强类型会话对象。"""
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
        _require_exact_fields(record, expected, "session_meta")
        try:
            return SessionMeta(
                session_id=_require_record_text(record, "session_id"),
                format_version=_require_current_version(record),
                workspace_id=_require_record_text(record, "workspace_id"),
                workspace_path=_require_record_text(record, "workspace_path"),
                title=_require_record_text(record, "title"),
                created_at=_parse_utc_datetime(record, "created_at"),
                updated_at=_parse_utc_datetime(record, "updated_at"),
            )
        except (TypeError, ValueError) as exc:
            raise SessionFormatError(str(exc)) from None
    if record_type == SessionRecordType.TURN.value:
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
            _require_current_version(record)
            return TurnEntry(
                turn_id=_require_record_text(record, "turn_id"),
                run_id=_require_record_text(record, "run_id"),
                user_message=_parse_message(record, "user_message", MessageRole.USER),
                assistant_message=_parse_message(
                    record, "assistant_message", MessageRole.ASSISTANT
                ),
                completed_at=_parse_utc_datetime(record, "completed_at"),
            )
        except (TypeError, ValueError) as exc:
            raise SessionFormatError(str(exc)) from None
    raise SessionFormatError("unknown session record type")
