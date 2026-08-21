"""P3-01 会话公共数据契约测试。"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from mica_agent.models import Message, MessageRole
from mica_agent.session_contracts import (
    CURRENT_SESSION_FORMAT_VERSION,
    SessionError,
    SessionFormatError,
    SessionMeta,
    SessionRecordType,
    TurnEntry,
    deserialize_session_record,
    serialize_session_record,
)

UTC_NOW = datetime(2026, 8, 21, 3, 4, 5, 123456, tzinfo=UTC)


def make_session_meta(**changes: object) -> SessionMeta:
    values = {
        "session_id": "session-1",
        "format_version": CURRENT_SESSION_FORMAT_VERSION,
        "workspace_id": "workspace-1",
        "workspace_path": r"C:\work\mica",
        "title": "Mica session",
        "created_at": UTC_NOW,
        "updated_at": UTC_NOW,
    }
    values.update(changes)
    return SessionMeta(**values)  # type: ignore[arg-type]


def test_session_meta_carries_validated_session_identity() -> None:
    meta = SessionMeta(
        session_id="session-1",
        format_version=CURRENT_SESSION_FORMAT_VERSION,
        workspace_id="workspace-1",
        workspace_path=r"C:\work\mica",
        title="Mica session",
        created_at=UTC_NOW,
        updated_at=UTC_NOW,
    )

    assert meta.session_id == "session-1"
    assert meta.format_version == 1
    assert meta.workspace_id == "workspace-1"
    assert meta.workspace_path == r"C:\work\mica"
    assert meta.title == "Mica session"
    assert meta.created_at == UTC_NOW
    assert meta.updated_at == UTC_NOW

    with pytest.raises(FrozenInstanceError):
        meta.title = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("field", ["session_id", "workspace_id", "workspace_path"])
@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_session_meta_rejects_blank_identity_fields(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=field):
        make_session_meta(**{field: value})


@pytest.mark.parametrize("version", [True, False, 1.0, "1", None])
def test_session_meta_rejects_non_integer_format_versions(version: object) -> None:
    with pytest.raises(TypeError, match="format_version"):
        make_session_meta(format_version=version)


@pytest.mark.parametrize("version", [0, 2, -1])
def test_session_meta_rejects_unsupported_format_versions(version: int) -> None:
    with pytest.raises(ValueError, match="format_version"):
        make_session_meta(format_version=version)


def test_session_meta_allows_empty_title_for_an_unnamed_new_session() -> None:
    assert make_session_meta(title="").title == ""


def test_session_meta_rejects_blank_non_empty_title() -> None:
    with pytest.raises(ValueError, match="title"):
        make_session_meta(title="   ")


@pytest.mark.parametrize(
    "field", ["session_id", "workspace_id", "workspace_path", "title"]
)
def test_session_meta_rejects_non_text_fields(field: str) -> None:
    with pytest.raises(TypeError, match=field):
        make_session_meta(**{field: 42})


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_session_meta_rejects_naive_datetimes(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        make_session_meta(
            **{
                field: datetime(  # noqa: DTZ001 - intentional invalid input
                    2026, 8, 21, 3, 4, 5
                )
            }
        )


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_session_meta_rejects_non_utc_aware_datetimes(field: str) -> None:
    non_utc = datetime(2026, 8, 21, 11, 4, 5, tzinfo=timezone(timedelta(hours=8)))

    with pytest.raises(ValueError, match=field):
        make_session_meta(**{field: non_utc})


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_session_meta_rejects_non_datetime_values(field: str) -> None:
    with pytest.raises(TypeError, match=field):
        make_session_meta(**{field: "2026-08-21T03:04:05+00:00"})


def test_session_meta_rejects_updated_time_before_creation() -> None:
    earlier = UTC_NOW - timedelta(microseconds=1)

    with pytest.raises(ValueError, match="updated_at"):
        make_session_meta(updated_at=earlier)


def make_turn_entry(**changes: object) -> TurnEntry:
    values = {
        "turn_id": "turn-1",
        "run_id": "run-1",
        "user_message": Message(MessageRole.USER, "hello"),
        "assistant_message": Message(MessageRole.ASSISTANT, "hi"),
        "completed_at": UTC_NOW,
    }
    values.update(changes)
    return TurnEntry(**values)  # type: ignore[arg-type]


def test_turn_entry_carries_one_complete_successful_turn() -> None:
    entry = make_turn_entry()

    assert entry.turn_id == "turn-1"
    assert entry.run_id == "run-1"
    assert entry.user_message == Message(MessageRole.USER, "hello")
    assert entry.assistant_message == Message(MessageRole.ASSISTANT, "hi")
    assert entry.completed_at == UTC_NOW

    with pytest.raises(FrozenInstanceError):
        entry.run_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("field", ["turn_id", "run_id"])
@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_turn_entry_rejects_blank_ids(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=field):
        make_turn_entry(**{field: value})


@pytest.mark.parametrize("field", ["turn_id", "run_id"])
def test_turn_entry_rejects_non_text_ids(field: str) -> None:
    with pytest.raises(TypeError, match=field):
        make_turn_entry(**{field: 42})


def test_turn_entry_rejects_a_non_user_message_in_user_position() -> None:
    with pytest.raises(ValueError, match="user_message"):
        make_turn_entry(user_message=Message(MessageRole.ASSISTANT, "wrong role"))


def test_turn_entry_rejects_a_non_assistant_message_in_assistant_position() -> None:
    with pytest.raises(ValueError, match="assistant_message"):
        make_turn_entry(assistant_message=Message(MessageRole.USER, "wrong role"))


@pytest.mark.parametrize("field", ["user_message", "assistant_message"])
def test_turn_entry_rejects_non_message_values(field: str) -> None:
    with pytest.raises(TypeError, match=field):
        make_turn_entry(**{field: "not a Message"})


def test_turn_entry_allows_an_empty_complete_assistant_message() -> None:
    entry = make_turn_entry(assistant_message=Message(MessageRole.ASSISTANT, ""))

    assert entry.assistant_message.content == ""


def test_turn_entry_rejects_naive_completion_time() -> None:
    with pytest.raises(ValueError, match="completed_at"):
        make_turn_entry(
            completed_at=datetime(  # noqa: DTZ001 - intentional invalid input
                2026, 8, 21, 3, 4, 5
            )
        )


def test_turn_entry_rejects_non_utc_completion_time() -> None:
    non_utc = datetime(2026, 8, 21, 11, 4, 5, tzinfo=timezone(timedelta(hours=8)))

    with pytest.raises(ValueError, match="completed_at"):
        make_turn_entry(completed_at=non_utc)


def test_session_meta_serializes_to_an_explicit_canonical_record() -> None:
    record = serialize_session_record(make_session_meta())

    assert record == {
        "record_type": "session_meta",
        "format_version": 1,
        "session_id": "session-1",
        "workspace_id": "workspace-1",
        "workspace_path": r"C:\work\mica",
        "title": "Mica session",
        "created_at": "2026-08-21T03:04:05.123456+00:00",
        "updated_at": "2026-08-21T03:04:05.123456+00:00",
    }
    assert SessionRecordType.SESSION_META.value == "session_meta"
    assert json.loads(json.dumps(record)) == record


def test_turn_entry_serializes_to_an_explicit_canonical_record() -> None:
    record = serialize_session_record(make_turn_entry())

    assert record == {
        "record_type": "turn",
        "format_version": 1,
        "turn_id": "turn-1",
        "run_id": "run-1",
        "user_message": {"role": "user", "content": "hello"},
        "assistant_message": {"role": "assistant", "content": "hi"},
        "completed_at": "2026-08-21T03:04:05.123456+00:00",
    }
    assert SessionRecordType.TURN.value == "turn"
    assert json.loads(json.dumps(record)) == record


def test_session_meta_record_round_trips_to_a_strong_type() -> None:
    original = make_session_meta()

    restored = deserialize_session_record(serialize_session_record(original))

    assert restored == original
    assert isinstance(SessionFormatError("bad record"), SessionError)


def test_turn_record_round_trips_to_a_strong_type() -> None:
    original = make_turn_entry()

    restored = deserialize_session_record(serialize_session_record(original))

    assert restored == original


@pytest.mark.parametrize(
    "record",
    [
        serialize_session_record(make_session_meta()),
        serialize_session_record(make_turn_entry()),
    ],
)
@pytest.mark.parametrize("version", [0, 2, -1])
def test_deserializer_rejects_unknown_versions(
    record: dict[str, object], version: int
) -> None:
    record["format_version"] = version

    with pytest.raises(SessionFormatError, match="format_version"):
        deserialize_session_record(record)


@pytest.mark.parametrize("version", [True, False, 1.0, "1", None])
def test_deserializer_does_not_treat_bool_or_other_types_as_a_version(
    version: object,
) -> None:
    record = serialize_session_record(make_turn_entry())
    record["format_version"] = version

    with pytest.raises(SessionFormatError, match="format_version"):
        deserialize_session_record(record)


def test_deserializer_rejects_an_unknown_record_type_without_echoing_it() -> None:
    untrusted_type = "secret-record-type-value"

    with pytest.raises(SessionFormatError, match="unknown") as caught:
        deserialize_session_record({"record_type": untrusted_type})

    assert untrusted_type not in str(caught.value)


def test_deserializer_requires_an_explicit_record_type() -> None:
    with pytest.raises(SessionFormatError, match="record_type"):
        deserialize_session_record({"format_version": 1})


@pytest.mark.parametrize("record", [None, [], "record", 42])
def test_deserializer_requires_a_json_object(record: object) -> None:
    with pytest.raises(SessionFormatError, match="JSON object"):
        deserialize_session_record(record)


@pytest.mark.parametrize(
    ("record", "missing_field"),
    [
        (serialize_session_record(make_session_meta()), "title"),
        (serialize_session_record(make_turn_entry()), "run_id"),
    ],
)
def test_deserializer_rejects_missing_fields(
    record: dict[str, object], missing_field: str
) -> None:
    del record[missing_field]

    with pytest.raises(SessionFormatError, match=missing_field):
        deserialize_session_record(record)


@pytest.mark.parametrize(
    "record",
    [
        serialize_session_record(make_session_meta()),
        serialize_session_record(make_turn_entry()),
    ],
)
def test_deserializer_rejects_extra_fields(record: dict[str, object]) -> None:
    record["unexpected"] = "untrusted-value"

    with pytest.raises(SessionFormatError, match="unexpected fields") as caught:
        deserialize_session_record(record)

    assert "untrusted-value" not in str(caught.value)


@pytest.mark.parametrize(
    ("record", "field"),
    [
        (serialize_session_record(make_session_meta()), "session_id"),
        (serialize_session_record(make_session_meta()), "workspace_path"),
        (serialize_session_record(make_turn_entry()), "turn_id"),
        (serialize_session_record(make_turn_entry()), "run_id"),
    ],
)
def test_deserializer_rejects_wrong_top_level_field_types(
    record: dict[str, object], field: str
) -> None:
    record[field] = 42

    with pytest.raises(SessionFormatError, match=field):
        deserialize_session_record(record)


@pytest.mark.parametrize("field", ["user_message", "assistant_message"])
def test_deserializer_rejects_non_object_messages(field: str) -> None:
    record = serialize_session_record(make_turn_entry())
    record[field] = "not an object"

    with pytest.raises(SessionFormatError, match=field):
        deserialize_session_record(record)


@pytest.mark.parametrize("field", ["user_message", "assistant_message"])
def test_deserializer_rejects_missing_or_extra_message_fields(field: str) -> None:
    missing = serialize_session_record(make_turn_entry())
    missing_message = missing[field]
    assert isinstance(missing_message, dict)
    del missing_message["content"]

    with pytest.raises(SessionFormatError, match="content"):
        deserialize_session_record(missing)

    extra = serialize_session_record(make_turn_entry())
    extra_message = extra[field]
    assert isinstance(extra_message, dict)
    extra_message["extra"] = "untrusted-value"

    with pytest.raises(SessionFormatError, match="unexpected fields"):
        deserialize_session_record(extra)


@pytest.mark.parametrize(
    ("field", "role"),
    [("user_message", "assistant"), ("assistant_message", "user")],
)
def test_deserializer_rejects_invalid_message_roles(field: str, role: str) -> None:
    record = serialize_session_record(make_turn_entry())
    message = record[field]
    assert isinstance(message, dict)
    message["role"] = role

    with pytest.raises(SessionFormatError, match=rf"{field}\.role"):
        deserialize_session_record(record)


@pytest.mark.parametrize("field", ["user_message", "assistant_message"])
def test_deserializer_rejects_non_text_message_content(field: str) -> None:
    record = serialize_session_record(make_turn_entry())
    message = record[field]
    assert isinstance(message, dict)
    message["content"] = 42

    with pytest.raises(SessionFormatError, match="content"):
        deserialize_session_record(record)


@pytest.mark.parametrize(
    ("record", "field"),
    [
        (serialize_session_record(make_session_meta()), "session_id"),
        (serialize_session_record(make_session_meta()), "workspace_id"),
        (serialize_session_record(make_session_meta()), "workspace_path"),
        (serialize_session_record(make_turn_entry()), "turn_id"),
        (serialize_session_record(make_turn_entry()), "run_id"),
    ],
)
def test_deserializer_rejects_blank_required_text(
    record: dict[str, object], field: str
) -> None:
    record[field] = "   "

    with pytest.raises(SessionFormatError, match=field):
        deserialize_session_record(record)


@pytest.mark.parametrize(
    ("record", "field"),
    [
        (serialize_session_record(make_session_meta()), "created_at"),
        (serialize_session_record(make_session_meta()), "updated_at"),
        (serialize_session_record(make_turn_entry()), "completed_at"),
    ],
)
@pytest.mark.parametrize(
    "timestamp",
    [
        "not-a-timestamp",
        "2026-08-21T03:04:05.123456",
        "2026-08-21T11:04:05.123456+08:00",
        42,
    ],
)
def test_deserializer_rejects_invalid_or_non_utc_timestamps(
    record: dict[str, object], field: str, timestamp: object
) -> None:
    record[field] = timestamp

    with pytest.raises(SessionFormatError, match=field):
        deserialize_session_record(record)


def test_deserializer_rejects_meta_with_updated_time_before_creation() -> None:
    record = serialize_session_record(make_session_meta())
    record["updated_at"] = "2026-08-21T03:04:05.123455+00:00"

    with pytest.raises(SessionFormatError, match="updated_at"):
        deserialize_session_record(record)


def test_format_errors_do_not_echo_untrusted_record_content() -> None:
    sensitive_marker = "credential-like-secret-marker"
    record = serialize_session_record(
        make_turn_entry(
            assistant_message=Message(MessageRole.ASSISTANT, sensitive_marker)
        )
    )
    record["completed_at"] = "invalid-time"

    with pytest.raises(SessionFormatError) as caught:
        deserialize_session_record(record)

    assert sensitive_marker not in str(caught.value)
    assert "invalid-time" not in str(caught.value)
