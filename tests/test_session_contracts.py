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
from mica_agent.tool_contracts import ToolCall
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
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
    assert meta.format_version == 3
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


@pytest.mark.parametrize("version", [0, 4, -1])
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


def fixed_v1_turn_record() -> dict[str, object]:
    return {
        "record_type": "turn",
        "format_version": 1,
        "turn_id": "turn-1",
        "run_id": "run-1",
        "user_message": {"role": "user", "content": "hello"},
        "assistant_message": {"role": "assistant", "content": "hi"},
        "completed_at": "2026-08-21T03:04:05.123456+00:00",
    }


def test_v3_tool_turn_keeps_presentations_separate_from_context_messages() -> None:
    call = ToolCall(
        "call-1", "edit", {"path": "notes.md", "old_text": "old", "new_text": "new"}
    )
    presentation = ToolPresentationSnapshot(
        "call-1",
        "edit",
        ToolPresentationKind.EDIT,
        ToolPresentationStatus.SUCCESS,
        {
            "path": "notes.md",
            "matched": True,
            "lines": [
                {"kind": "removed", "text": "old", "old_line": 1, "new_line": None},
                {"kind": "added", "text": "new", "old_line": None, "new_line": 1},
            ],
        },
    )
    turn = make_turn_entry(
        intermediate_messages=(
            Message(MessageRole.ASSISTANT, "", (call,)),
            Message(MessageRole.TOOL, '{"status":"success"}', tool_call_id="call-1"),
        ),
        tool_presentations=(presentation,),
    )

    wire = serialize_session_record(turn)
    restored = deserialize_session_record(wire)

    assert wire["format_version"] == 3
    assert wire["tool_presentations"] == [
        {
            "call_id": "call-1",
            "tool_name": "edit",
            "kind": "edit",
            "status": "success",
            "payload": {
                "path": "notes.md",
                "matched": True,
                "lines": [
                    {"kind": "removed", "text": "old", "old_line": 1, "new_line": None},
                    {"kind": "added", "text": "new", "old_line": None, "new_line": 1},
                ],
            },
            "message": None,
        }
    ]
    assert isinstance(restored, TurnEntry)
    assert restored.tool_presentations == (presentation,)
    assert restored.context_messages == turn.context_messages


def _v3_tool_record() -> dict[str, object]:
    call = ToolCall("call-1", "read", {"path": "notes.md"})
    turn = make_turn_entry(
        intermediate_messages=(
            Message(MessageRole.ASSISTANT, "", (call,)),
            Message(MessageRole.TOOL, '{"status":"success"}', tool_call_id="call-1"),
        ),
        tool_presentations=(
            ToolPresentationSnapshot(
                "call-1",
                "read",
                ToolPresentationKind.READ,
                ToolPresentationStatus.SUCCESS,
                {"path": "notes.md"},
            ),
        ),
    )
    return serialize_session_record(turn)


def test_v3_reader_strictly_validates_snapshot_shape_and_terminal_status() -> None:
    missing = _v3_tool_record()
    del missing["tool_presentations"]
    with pytest.raises(SessionFormatError):
        deserialize_session_record(missing)

    extra = _v3_tool_record()
    assert isinstance(extra["tool_presentations"], list)
    extra["tool_presentations"][0]["unexpected"] = "credential-like-marker"  # type: ignore[index]
    with pytest.raises(SessionFormatError) as caught:
        deserialize_session_record(extra)
    assert "credential-like-marker" not in str(caught.value)

    pending = _v3_tool_record()
    assert isinstance(pending["tool_presentations"], list)
    pending["tool_presentations"][0]["status"] = "pending"  # type: ignore[index]
    with pytest.raises(SessionFormatError):
        deserialize_session_record(pending)

    bad_path = _v3_tool_record()
    assert isinstance(bad_path["tool_presentations"], list)
    payload = bad_path["tool_presentations"][0]["payload"]  # type: ignore[index]
    assert isinstance(payload, dict)
    payload["path"] = "C:/outside"
    with pytest.raises(SessionFormatError):
        deserialize_session_record(bad_path)


def test_v3_reader_round_trips_failed_edit_without_fabricated_line_numbers() -> None:
    call = ToolCall("call-edit", "edit", {"path": "notes.md"})
    presentation = ToolPresentationSnapshot(
        "call-edit",
        "edit",
        ToolPresentationKind.EDIT,
        ToolPresentationStatus.FAILED,
        {
            "path": "notes.md",
            "matched": False,
            "lines": [
                {"kind": "removed", "text": "old", "old_line": None, "new_line": None},
                {"kind": "added", "text": "new", "old_line": None, "new_line": None},
            ],
        },
    )
    turn = make_turn_entry(
        intermediate_messages=(
            Message(MessageRole.ASSISTANT, "", (call,)),
            Message(MessageRole.TOOL, "failed", tool_call_id="call-edit"),
        ),
        tool_presentations=(presentation,),
    )

    restored = deserialize_session_record(serialize_session_record(turn))

    assert restored == turn


def test_v3_reader_rejects_invalid_edit_line_numbers_and_kinds() -> None:
    record = _v3_tool_record()
    assert isinstance(record["tool_presentations"], list)
    presentation = record["tool_presentations"][0]
    assert isinstance(presentation, dict)
    presentation["kind"] = "edit"
    payload = {"path": "notes.md", "matched": True, "lines": []}
    presentation["payload"] = payload
    call = record["messages"][1] if isinstance(record["messages"], list) else None
    assert call is not None
    # The read ToolCall remains deliberately mismatched, so the identity check
    # is also exercised without exposing the invalid payload.
    with pytest.raises(SessionFormatError):
        deserialize_session_record(record)


def test_tool_turn_round_trips_complete_context_order() -> None:
    first_call = ToolCall("call-1", "read", {"path": "a.txt"})
    second_call = ToolCall("call-2", "ls", {"path": "."})
    intermediate = (
        Message(MessageRole.ASSISTANT, "checking", (first_call,)),
        Message(MessageRole.TOOL, '{"status":"success"}', tool_call_id="call-1"),
        Message(MessageRole.ASSISTANT, "", (second_call,)),
        Message(MessageRole.TOOL, '{"status":"success"}', tool_call_id="call-2"),
    )
    turn = TurnEntry(
        "turn-tools",
        "run-tools",
        Message(MessageRole.USER, "inspect"),
        Message(MessageRole.ASSISTANT, "done"),
        UTC_NOW,
        intermediate,
    )

    restored = deserialize_session_record(serialize_session_record(turn))

    assert restored == turn
    assert isinstance(restored, TurnEntry)
    assert restored.context_messages == (
        turn.user_message,
        *intermediate,
        turn.assistant_message,
    )


def test_turn_entry_uses_redacted_tool_snapshot_without_mutating_input() -> None:
    secret = "sk-direct-store-sensitive"
    call = ToolCall("call-secret", "read", {"api_key": secret})
    assistant = Message(MessageRole.ASSISTANT, "", (call,))
    tool = Message(
        MessageRole.TOOL,
        '{"token":"Bearer direct-private-token"}',
        tool_call_id="call-secret",
    )

    turn = make_turn_entry(intermediate_messages=(assistant, tool))
    serialized = serialize_session_record(turn)

    observed = repr((turn.intermediate_messages, serialized))
    assert secret not in observed
    assert "direct-private-token" not in observed
    assert "[REDACTED]" in observed
    assert assistant.tool_calls[0].arguments["api_key"] == secret
    assert tool.content == '{"token":"Bearer direct-private-token"}'


def test_invalid_v2_tool_sequence_fails_without_echoing_sensitive_content() -> None:
    sensitive = "sk-sensitive-session-sentinel"
    record = {
        "record_type": "turn",
        "format_version": 2,
        "turn_id": "bad-turn",
        "run_id": "bad-run",
        "messages": [
            {"role": "user", "content": "question"},
            {"role": "tool", "content": sensitive, "tool_call_id": "orphan"},
            {"role": "assistant", "content": "answer"},
        ],
        "completed_at": "2026-08-21T03:04:05.123456+00:00",
    }

    with pytest.raises(SessionFormatError) as caught:
        deserialize_session_record(record)

    assert sensitive not in str(caught.value)
    assert sensitive not in repr(caught.value)


def test_turn_entry_rejects_unmatched_tool_result_order() -> None:
    call = ToolCall("call-1", "read", {})

    with pytest.raises(ValueError, match="order"):
        make_turn_entry(
            intermediate_messages=(
                Message(MessageRole.ASSISTANT, "", (call,)),
                Message(MessageRole.TOOL, "result", tool_call_id="wrong-call"),
            )
        )


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
        "format_version": 3,
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
        "format_version": 3,
        "turn_id": "turn-1",
        "run_id": "run-1",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
        "tool_presentations": [],
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
@pytest.mark.parametrize("version", [0, 4, -1])
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


def test_deserializer_rejects_a_turn_without_format_version() -> None:
    with pytest.raises(SessionFormatError, match="format_version") as caught:
        deserialize_session_record({"record_type": "turn"})

    assert type(caught.value) is SessionFormatError


def test_deserializer_does_not_echo_messages_when_turn_lacks_format_version() -> None:
    sensitive = "sk-missing-version-sensitive-sentinel"
    record = {
        "record_type": "turn",
        "messages": [
            {"role": "user", "content": sensitive},
            {"role": "assistant", "content": "answer"},
        ],
    }

    with pytest.raises(SessionFormatError, match="format_version") as caught:
        deserialize_session_record(record)

    assert type(caught.value) is SessionFormatError
    assert sensitive not in str(caught.value)
    assert sensitive not in repr(caught.value)


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
    record = fixed_v1_turn_record()
    record[field] = "not an object"

    with pytest.raises(SessionFormatError, match=field):
        deserialize_session_record(record)


@pytest.mark.parametrize("field", ["user_message", "assistant_message"])
def test_deserializer_rejects_missing_or_extra_message_fields(field: str) -> None:
    missing = fixed_v1_turn_record()
    missing_message = missing[field]
    assert isinstance(missing_message, dict)
    del missing_message["content"]

    with pytest.raises(SessionFormatError, match="content"):
        deserialize_session_record(missing)

    extra = fixed_v1_turn_record()
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
    record = fixed_v1_turn_record()
    message = record[field]
    assert isinstance(message, dict)
    message["role"] = role

    with pytest.raises(SessionFormatError, match=rf"{field}\.role"):
        deserialize_session_record(record)


@pytest.mark.parametrize("field", ["user_message", "assistant_message"])
def test_deserializer_rejects_non_text_message_content(field: str) -> None:
    record = fixed_v1_turn_record()
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
