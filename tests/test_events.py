"""Engine Event 公共契约测试。

验证运行、思考与文本增量、工具及交互事件的身份、顺序、字段校验和不可变性；
终态带统一 RunStatus，run_failed 带错误类别和面向用户的安全诊断；
工具展示与输出增量遵守数据和安全边界。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from typing import get_args

import pytest

from mica_agent.events import (
    EngineEvent,
    ErrorCategory,
    PermissionRequested,
    ReasoningDelta,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    RunStatus,
    TextDelta,
    ToolFinished,
    ToolPrepared,
    ToolStarted,
    UserInputRequested,
)
from mica_agent.interactions import AskOption, AskPrompt
from mica_agent.tool_contracts import ToolResultStatus
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)


def test_run_started_carries_run_id_and_opens_at_seq_zero() -> None:
    event = RunStarted(run_id="run-1")

    assert event.run_id == "run-1"
    assert event.seq == 0


def test_terminal_status_cannot_be_overridden() -> None:
    with pytest.raises(TypeError):
        RunCompleted(run_id="run-1", seq=1, status=RunStatus.FAILED)

    with pytest.raises(TypeError):
        RunFailed(
            run_id="run-1",
            seq=1,
            message="boom",
            category=ErrorCategory.UNKNOWN,
            status=RunStatus.COMPLETED,
        )

    with pytest.raises(TypeError):
        RunCancelled(run_id="run-1", seq=1, status=RunStatus.COMPLETED)


def test_reasoning_delta_carries_run_id_text_and_seq() -> None:
    event = ReasoningDelta(run_id="run-1", text="先分析", seq=1)

    assert event.run_id == "run-1"
    assert event.text == "先分析"
    assert event.seq == 1
    assert isinstance(event, get_args(EngineEvent))
    with pytest.raises(FrozenInstanceError):
        event.text = "changed"  # type: ignore[misc]


def test_text_delta_carries_run_id_text_and_seq() -> None:
    event = TextDelta(run_id="run-1", text="hello", seq=1)

    assert event.run_id == "run-1"
    assert event.text == "hello"
    assert event.seq == 1


def test_run_completed_carries_run_id_and_completed_status() -> None:
    event = RunCompleted(run_id="run-1", seq=2)

    assert event.run_id == "run-1"
    assert event.seq == 2
    assert event.status is RunStatus.COMPLETED


def test_run_failed_carries_diagnostic_and_category() -> None:
    event = RunFailed(
        run_id="run-1",
        seq=2,
        message="request timed out after 30s",
        category=ErrorCategory.TIMEOUT,
    )

    assert event.run_id == "run-1"
    assert event.seq == 2
    assert event.message == "request timed out after 30s"
    assert event.category is ErrorCategory.TIMEOUT
    assert event.status is RunStatus.FAILED


def test_run_cancelled_carries_run_id_and_cancelled_status() -> None:
    event = RunCancelled(run_id="run-1", seq=3)

    assert event.run_id == "run-1"
    assert event.seq == 3
    assert event.status is RunStatus.CANCELLED


@pytest.mark.parametrize(
    ("status", "wire_value"),
    [
        (RunStatus.COMPLETED, "completed"),
        (RunStatus.FAILED, "failed"),
        (RunStatus.CANCELLED, "cancelled"),
    ],
)
def test_run_status_wire_values(status: RunStatus, wire_value: str) -> None:
    assert status.value == wire_value


@pytest.mark.parametrize(
    ("category", "wire_value"),
    [
        (ErrorCategory.AUTHENTICATION, "authentication"),
        (ErrorCategory.RATE_LIMIT, "rate_limit"),
        (ErrorCategory.TIMEOUT, "timeout"),
        (ErrorCategory.TEMPORARY, "temporary"),
        (ErrorCategory.MALFORMED, "malformed"),
        (ErrorCategory.LIMIT, "limit"),
        (ErrorCategory.UNKNOWN, "unknown"),
    ],
)
def test_error_category_wire_values(category: ErrorCategory, wire_value: str) -> None:
    assert category.value == wire_value


def test_every_engine_event_carries_run_id_and_seq() -> None:
    events = [
        RunStarted(run_id="run-1"),
        TextDelta(run_id="run-1", text="x", seq=1),
        RunCompleted(run_id="run-1", seq=2),
        RunFailed(
            run_id="run-1", seq=2, message="boom", category=ErrorCategory.UNKNOWN
        ),
        RunCancelled(run_id="run-1", seq=3),
    ]

    for event in events:
        assert isinstance(event.run_id, str) and event.run_id
        assert isinstance(event.seq, int)
        assert isinstance(event, get_args(EngineEvent))


def test_run_failed_reports_user_facing_diagnostic_not_sensitive_data() -> None:
    event = RunFailed(
        run_id="run-1",
        seq=2,
        message="authentication rejected",
        category=ErrorCategory.AUTHENTICATION,
    )

    assert "api" not in event.message.lower()
    assert "authorization" not in event.message.lower()
    assert "sk-" not in event.message.lower()


def test_engine_events_are_immutable() -> None:
    event = TextDelta(run_id="run-1", text="x", seq=1)

    with pytest.raises(FrozenInstanceError):
        event.text = "tampered"  # type: ignore[misc]


def test_run_id_must_be_non_blank() -> None:
    builders = (
        lambda run_id: RunStarted(run_id=run_id),
        lambda run_id: TextDelta(run_id=run_id, text="x", seq=1),
        lambda run_id: RunCompleted(run_id=run_id, seq=1),
        lambda run_id: RunFailed(
            run_id=run_id, message="boom", category=ErrorCategory.UNKNOWN, seq=1
        ),
        lambda run_id: RunCancelled(run_id=run_id, seq=1),
    )
    for run_id in ("", "   ", "\t\n"):
        for build in builders:
            with pytest.raises(ValueError):
                build(run_id)


def test_run_id_must_be_str() -> None:
    builders = (
        lambda run_id: RunStarted(run_id=run_id),  # type: ignore[arg-type]
        lambda run_id: TextDelta(run_id=run_id, text="x", seq=1),  # type: ignore[arg-type]
        lambda run_id: RunCompleted(run_id=run_id, seq=1),  # type: ignore[arg-type]
    )
    for run_id in (123, None, 3.14):
        for build in builders:
            with pytest.raises(TypeError):
                build(run_id)


def test_run_started_seq_is_fixed_to_zero() -> None:
    event = RunStarted(run_id="run-1")

    assert event.seq == 0
    with pytest.raises(TypeError):
        RunStarted(run_id="run-1", seq=0)  # init=False，不可覆盖


@pytest.mark.parametrize("seq", [-1, -100])
def test_seq_must_be_non_negative(seq: int) -> None:
    for build in (
        lambda: TextDelta(run_id="run-1", text="x", seq=seq),
        lambda: RunCompleted(run_id="run-1", seq=seq),
        lambda: RunFailed(
            run_id="run-1", message="boom", category=ErrorCategory.UNKNOWN, seq=seq
        ),
        lambda: RunCancelled(run_id="run-1", seq=seq),
    ):
        with pytest.raises(ValueError):
            build()


@pytest.mark.parametrize("seq", ["1", 1.5, True])
def test_seq_must_be_int(seq: object) -> None:
    for build in (
        lambda: TextDelta(run_id="run-1", text="x", seq=seq),  # type: ignore[arg-type]
        lambda: RunCompleted(run_id="run-1", seq=seq),  # type: ignore[arg-type]
        lambda: RunFailed(
            run_id="run-1",
            message="boom",
            category=ErrorCategory.UNKNOWN,
            seq=seq,  # type: ignore[arg-type]
        ),
        lambda: RunCancelled(run_id="run-1", seq=seq),  # type: ignore[arg-type]
    ):
        with pytest.raises(TypeError):
            build()


@pytest.mark.parametrize("message", ["", "   "])
def test_run_failed_message_must_be_non_blank(message: str) -> None:
    with pytest.raises(ValueError):
        RunFailed(
            run_id="run-1", message=message, category=ErrorCategory.UNKNOWN, seq=1
        )


def test_run_failed_message_must_be_str() -> None:
    with pytest.raises(TypeError):
        RunFailed(  # type: ignore[arg-type]
            run_id="run-1", message=b"boom", category=ErrorCategory.UNKNOWN, seq=1
        )


def test_run_failed_category_must_be_error_category() -> None:
    with pytest.raises(TypeError):
        RunFailed(  # type: ignore[arg-type]
            run_id="run-1", message="boom", category="unknown", seq=1
        )


def test_new_tool_and_interaction_events_join_engine_event_union() -> None:
    events = [
        ToolStarted("run-1", 1, "call-1", "read"),
        ToolFinished("run-1", 2, "call-1", "read", ToolResultStatus.SUCCESS),
        PermissionRequested(
            "run-1", 3, "interaction-1", "call-1", "write", "write access"
        ),
        UserInputRequested("run-1", 4, "interaction-2", "Which encoding?"),
    ]

    assert all(isinstance(event, get_args(EngineEvent)) for event in events)
    assert [event.seq for event in events] == [1, 2, 3, 4]


def test_tool_events_carry_only_safe_summary_fields() -> None:
    started = ToolStarted("run-1", 1, "call-1", "bash")
    finished = ToolFinished(
        "run-1", 2, "call-1", "bash", ToolResultStatus.EXECUTION_FAILED
    )

    assert started.call_id == finished.call_id == "call-1"
    assert started.tool_name == finished.tool_name == "bash"
    assert finished.status is ToolResultStatus.EXECUTION_FAILED
    assert not hasattr(finished, "result")
    assert not hasattr(finished, "output")
    assert not hasattr(finished, "exception")


def test_interaction_events_carry_frontend_independent_data() -> None:
    permission = PermissionRequested(
        "run-1", 1, "interaction-1", "call-1", "write", "write access"
    )
    user_input = UserInputRequested("run-1", 2, "interaction-2", "Which encoding?")

    assert permission.interaction_id == "interaction-1"
    assert permission.message == "write access"
    assert user_input.interaction_id == "interaction-2"
    assert user_input.question == "Which encoding?"
    assert not hasattr(permission, "command")
    assert not hasattr(permission, "content")
    assert not hasattr(permission, "exception")
    assert not hasattr(user_input, "ui")
    assert not hasattr(user_input, "terminal")


def test_user_input_requested_preserves_or_builds_compatible_ask_prompt() -> None:
    legacy = UserInputRequested("run-1", 2, "interaction-legacy", "Continue?")
    prompt = AskPrompt(
        "Which encoding?",
        "Encoding",
        (AskOption("UTF-8", recommended=True), AskOption("Latin-1")),
        allow_custom=True,
    )
    structured = UserInputRequested(
        "run-1", 3, "interaction-structured", prompt.question, prompt
    )

    assert legacy.prompt == AskPrompt("Continue?")
    assert structured.question == prompt.question
    assert structured.prompt is prompt
    with pytest.raises(ValueError, match="prompt"):
        UserInputRequested("run-1", 4, "interaction-bad", "Different?", prompt)


@pytest.mark.parametrize("run_id", ["", "   ", "\t\n"])
def test_new_events_reuse_run_id_validation(run_id: str) -> None:
    builders = (
        lambda: ToolStarted(run_id, 1, "call-1", "read"),
        lambda: ToolFinished(run_id, 1, "call-1", "read", ToolResultStatus.SUCCESS),
        lambda: PermissionRequested(
            run_id, 1, "interaction-1", "call-1", "read", "read access"
        ),
        lambda: UserInputRequested(run_id, 1, "interaction-1", "Question?"),
    )
    for build in builders:
        with pytest.raises(ValueError, match="run_id"):
            build()


@pytest.mark.parametrize("seq", [-1, True, "1"])
def test_new_events_reuse_seq_validation(seq: object) -> None:
    builders = (
        lambda: ToolStarted("run-1", seq, "call-1", "read"),  # type: ignore[arg-type]
        lambda: ToolFinished(  # type: ignore[arg-type]
            "run-1", seq, "call-1", "read", ToolResultStatus.SUCCESS
        ),
        lambda: PermissionRequested(  # type: ignore[arg-type]
            "run-1", seq, "interaction-1", "call-1", "read", "read access"
        ),
        lambda: UserInputRequested(  # type: ignore[arg-type]
            "run-1", seq, "interaction-1", "Question?"
        ),
    )
    for build in builders:
        with pytest.raises((TypeError, ValueError)):
            build()


@pytest.mark.parametrize("call_id", ["", "   ", "\t\n"])
def test_tool_related_events_reject_blank_call_ids(call_id: str) -> None:
    builders = (
        lambda: ToolStarted("run-1", 1, call_id, "read"),
        lambda: ToolFinished("run-1", 1, call_id, "read", ToolResultStatus.SUCCESS),
        lambda: PermissionRequested(
            "run-1", 1, "interaction-1", call_id, "read", "read access"
        ),
    )
    for build in builders:
        with pytest.raises(ValueError, match="call_id"):
            build()


@pytest.mark.parametrize("tool_name", ["", "1read", "-read", "has space", "a" * 65])
def test_tool_related_events_reject_invalid_tool_names(tool_name: str) -> None:
    builders = (
        lambda: ToolStarted("run-1", 1, "call-1", tool_name),
        lambda: ToolFinished("run-1", 1, "call-1", tool_name, ToolResultStatus.SUCCESS),
        lambda: PermissionRequested(
            "run-1", 1, "interaction-1", "call-1", tool_name, "access"
        ),
    )
    for build in builders:
        with pytest.raises(ValueError, match="tool_name"):
            build()


@pytest.mark.parametrize("interaction_id", ["", "   ", "\t\n"])
def test_interaction_events_reject_blank_interaction_ids(
    interaction_id: str,
) -> None:
    with pytest.raises(ValueError, match="interaction_id"):
        PermissionRequested("run-1", 1, interaction_id, "call-1", "read", "read access")
    with pytest.raises(ValueError, match="interaction_id"):
        UserInputRequested("run-1", 1, interaction_id, "Question?")


def test_new_event_identifiers_and_tool_names_must_be_strings() -> None:
    builders = (
        lambda: ToolStarted("run-1", 1, 42, "read"),  # type: ignore[arg-type]
        lambda: ToolFinished(  # type: ignore[arg-type]
            "run-1", 1, "call-1", 42, ToolResultStatus.SUCCESS
        ),
        lambda: PermissionRequested(  # type: ignore[arg-type]
            "run-1", 1, 42, "call-1", "read", "access"
        ),
        lambda: UserInputRequested(  # type: ignore[arg-type]
            "run-1", 1, 42, "Question?"
        ),
    )
    for build in builders:
        with pytest.raises(TypeError):
            build()


def test_new_events_validate_status_and_frontend_text() -> None:
    with pytest.raises(TypeError, match="status"):
        ToolFinished(  # type: ignore[arg-type]
            "run-1", 1, "call-1", "read", "success"
        )
    with pytest.raises(ValueError, match="message"):
        PermissionRequested("run-1", 1, "interaction-1", "call-1", "read", "   ")
    with pytest.raises(TypeError, match="message"):
        PermissionRequested(  # type: ignore[arg-type]
            "run-1", 1, "interaction-1", "call-1", "read", b"raw"
        )
    with pytest.raises(ValueError, match="question"):
        UserInputRequested("run-1", 1, "interaction-1", "   ")
    with pytest.raises(TypeError, match="question"):
        UserInputRequested(  # type: ignore[arg-type]
            "run-1", 1, "interaction-1", b"raw"
        )


def test_new_events_are_immutable() -> None:
    event = ToolStarted("run-1", 1, "call-1", "read")

    with pytest.raises(FrozenInstanceError):
        event.tool_name = "write"  # type: ignore[misc]


def test_tool_prepared_and_finished_carry_presentation_contract() -> None:
    pending = ToolPresentationSnapshot(
        "call-1",
        "write",
        ToolPresentationKind.WRITE,
        ToolPresentationStatus.PENDING,
        {"path": "notes.md", "content": "text"},
    )
    prepared = ToolPrepared("run-1", 1, pending)
    finished = ToolFinished(
        "run-1",
        2,
        "call-1",
        "write",
        ToolResultStatus.SUCCESS,
        ToolPresentationSnapshot(
            "call-1",
            "write",
            ToolPresentationKind.WRITE,
            ToolPresentationStatus.SUCCESS,
            {"path": "notes.md", "content": "text"},
        ),
    )

    assert prepared.presentation.status is ToolPresentationStatus.PENDING
    assert prepared.call_id == "call-1"
    assert prepared.tool_name == "write"
    assert finished.presentation is not None
    assert finished.presentation.status is ToolPresentationStatus.SUCCESS
    assert ToolPrepared in get_args(EngineEvent)
    assert (
        ToolFinished(
            "run-1", 2, "call-1", "write", ToolResultStatus.SUCCESS
        ).presentation
        is None
    )


def test_tool_output_delta_carries_clean_output_and_joins_engine_event() -> None:
    from mica_agent.events import ToolOutputDelta

    event = ToolOutputDelta(
        run_id="run-1",
        seq=3,
        call_id="call-1",
        tool_name="bash",
        text="line 1\n",
    )

    assert event.run_id == "run-1"
    assert event.seq == 3
    assert event.call_id == "call-1"
    assert event.tool_name == "bash"
    assert event.text == "line 1\n"
    assert isinstance(event, get_args(EngineEvent))


@pytest.mark.parametrize(
    "text",
    [
        "plain ASCII",
        "中文输出 🙂",
        "\n",
        "first\r\nsecond\r\n",
        "partial line",
        "  leading and trailing  ",
    ],
)
def test_tool_output_delta_preserves_text_exactly(text: str) -> None:
    from mica_agent.events import ToolOutputDelta

    event = ToolOutputDelta("run-1", 3, "call-1", "bash", text)

    assert event.text == text


@pytest.mark.parametrize(
    ("field_name", "value", "error_type"),
    [
        ("run_id", 123, TypeError),
        ("run_id", "   ", ValueError),
        ("seq", -1, ValueError),
        ("seq", True, TypeError),
        ("call_id", 123, TypeError),
        ("call_id", "\t\n", ValueError),
        ("tool_name", "has space", ValueError),
        ("tool_name", 123, TypeError),
        ("text", b"tool-output-sensitive-marker", TypeError),
        ("text", "", ValueError),
    ],
)
def test_tool_output_delta_rejects_invalid_fields_without_echoing_text(
    field_name: str, value: object, error_type: type[Exception]
) -> None:
    from mica_agent.events import ToolOutputDelta

    values: dict[str, object] = {
        "run_id": "run-1",
        "seq": 3,
        "call_id": "call-1",
        "tool_name": "bash",
        "text": "safe text",
    }
    values[field_name] = value

    with pytest.raises(error_type) as exc_info:
        ToolOutputDelta(**values)  # type: ignore[arg-type]

    assert "tool-output-sensitive-marker" not in str(exc_info.value)


def test_tool_output_delta_has_only_five_immutable_slot_fields() -> None:
    from mica_agent.events import ToolOutputDelta

    event = ToolOutputDelta("run-1", 3, "call-1", "bash", "output")

    assert tuple(item.name for item in fields(event)) == (
        "run_id",
        "seq",
        "call_id",
        "tool_name",
        "text",
    )
    assert not hasattr(event, "__dict__")
    with pytest.raises(FrozenInstanceError):
        event.text = "changed"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        event.extra = "not allowed"  # type: ignore[attr-defined]


def test_tool_output_delta_repr_omits_sensitive_text() -> None:
    from mica_agent.events import ToolOutputDelta

    marker = "tool-output-sensitive-marker"
    event = ToolOutputDelta("run-1", 3, "call-1", "bash", marker)

    assert event.text == marker
    representation = repr(event)
    assert "ToolOutputDelta" in representation
    assert "run-1" in representation
    assert "call-1" in representation
    assert "bash" in representation
    assert marker not in representation
    assert "traceback" not in representation.lower()

    with pytest.raises(FrozenInstanceError) as exc_info:
        event.text = marker  # type: ignore[misc]
    assert marker not in str(exc_info.value)
