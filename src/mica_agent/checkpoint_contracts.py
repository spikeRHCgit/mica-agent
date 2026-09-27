"""Immutable public contracts for interrupted-run progress."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .message_sanitizer import sanitize_tool_message
from .models import Message, MessageRole
from .tool_contracts import ToolCall
from .tool_presentation import ToolPresentationSnapshot, ToolPresentationStatus


class RunCheckpointKind(Enum):
    """Host termination kind that produced an interrupted checkpoint."""

    CANCELLED = "cancelled"
    RECOVERABLE_FAILURE = "recoverable_failure"


class RunCheckpointToolStatus(Enum):
    """True boundary of a declared tool call at checkpoint time."""

    SUCCESS = "success"
    FAILED = "failed"
    IN_PROGRESS = "in_progress"
    NOT_STARTED = "not_started"


def _validate_non_blank(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a str")
    if not value.strip():
        raise ValueError(f"{field} must be non-empty and non-blank")


@dataclass(frozen=True, slots=True)
class RunCheckpointToolProgress:
    """One model tool call and its true completion state up to the checkpoint."""

    call: ToolCall = field(repr=False)
    status: RunCheckpointToolStatus
    result: Message | None = field(default=None, repr=False)
    presentation: ToolPresentationSnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.call, ToolCall):
            raise TypeError("call must be a ToolCall")
        if not isinstance(self.status, RunCheckpointToolStatus):
            raise TypeError("status must be a RunCheckpointToolStatus")
        if self.result is not None:
            if not isinstance(self.result, Message):
                raise TypeError("result must be a Message or None")
            if (
                self.result.role is not MessageRole.TOOL
                or self.result.tool_call_id != self.call.call_id
            ):
                raise ValueError("tool result must match the declared call")
        if self.presentation is not None:
            if not isinstance(self.presentation, ToolPresentationSnapshot):
                raise TypeError(
                    "presentation must be a ToolPresentationSnapshot or None"
                )
            if (
                self.presentation.call_id != self.call.call_id
                or self.presentation.tool_name != self.call.name
            ):
                raise ValueError("presentation identity must match the declared call")
            expected_kind = {
                "read": "read",
                "write": "write",
                "edit": "edit",
                "bash": "bash",
                "ask": "ask",
            }.get(self.call.name, "generic")
            if self.presentation.kind.value != expected_kind:
                raise ValueError("presentation kind must match the declared call")
        terminal = self.status in {
            RunCheckpointToolStatus.SUCCESS,
            RunCheckpointToolStatus.FAILED,
        }
        if terminal and (self.result is None or self.presentation is None):
            raise ValueError(
                "terminal tool progress requires a result and presentation"
            )
        if not terminal and (self.result is not None or self.presentation is not None):
            raise ValueError("incomplete tool progress cannot contain a result")
        if (
            self.status is RunCheckpointToolStatus.SUCCESS
            and self.presentation is not None
            and self.presentation.status is not ToolPresentationStatus.SUCCESS
        ):
            raise ValueError("successful tool progress needs a successful presentation")
        if (
            self.status is RunCheckpointToolStatus.FAILED
            and self.presentation is not None
            and self.presentation.status is not ToolPresentationStatus.FAILED
        ):
            raise ValueError("failed tool progress needs a failed presentation")
        safe_call = sanitize_tool_message(
            Message(MessageRole.ASSISTANT, "", (self.call,))
        ).tool_calls[0]
        object.__setattr__(self, "call", safe_call)
        if self.result is not None:
            object.__setattr__(self, "result", sanitize_tool_message(self.result))


@dataclass(frozen=True, slots=True)
class RunCheckpointProgress:
    """One model step stored in occurrence order."""

    assistant_message: Message | None = field(default=None, repr=False)
    tools: tuple[RunCheckpointToolProgress, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if self.assistant_message is not None:
            if not isinstance(self.assistant_message, Message):
                raise TypeError("assistant_message must be a Message or None")
            if self.assistant_message.role is not MessageRole.ASSISTANT:
                raise ValueError("checkpoint assistant_message must be assistant")
            if self.assistant_message.tool_calls:
                raise ValueError("partial assistant message cannot carry tool_calls")
        if not isinstance(self.tools, tuple):
            raise TypeError("tools must be a tuple")
        if not all(isinstance(item, RunCheckpointToolProgress) for item in self.tools):
            raise TypeError("tools must contain only RunCheckpointToolProgress values")
        call_ids = [item.call.call_id for item in self.tools]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("duplicate tool call_id in checkpoint progress")
        incomplete_seen = False
        for item in self.tools:
            if (
                incomplete_seen
                and item.status is not RunCheckpointToolStatus.NOT_STARTED
            ):
                raise ValueError(
                    "tool progress after an incomplete call must be not_started"
                )
            if item.status in {
                RunCheckpointToolStatus.IN_PROGRESS,
                RunCheckpointToolStatus.NOT_STARTED,
            }:
                incomplete_seen = True


@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    """Immutable, sanitized checkpoint of an unfinished run; not a successful
    assistant reply."""

    checkpoint_id: str = field(repr=False)
    run_id: str = field(repr=False)
    kind: RunCheckpointKind
    user_message: Message = field(repr=False)
    recorded_at: datetime
    progress: tuple[RunCheckpointProgress, ...] = field(default=(), repr=False)
    partial_assistant_message: Message | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_non_blank(self.checkpoint_id, "checkpoint_id")
        _validate_non_blank(self.run_id, "run_id")
        if not isinstance(self.kind, RunCheckpointKind):
            raise TypeError("kind must be a RunCheckpointKind")
        if not isinstance(self.user_message, Message):
            raise TypeError("user_message must be a Message")
        if self.user_message.role is not MessageRole.USER:
            raise ValueError("user_message role must be user")
        if not isinstance(self.progress, tuple):
            raise TypeError("progress must be a tuple")
        if not all(isinstance(step, RunCheckpointProgress) for step in self.progress):
            raise TypeError("progress must contain only RunCheckpointProgress values")
        if self.partial_assistant_message is not None:
            if not isinstance(self.partial_assistant_message, Message):
                raise TypeError("partial_assistant_message must be a Message or None")
            if self.partial_assistant_message.role is not MessageRole.ASSISTANT:
                raise ValueError("partial assistant message must be assistant")
            if self.partial_assistant_message.tool_calls:
                raise ValueError("partial assistant message cannot carry tool_calls")
        if not isinstance(self.recorded_at, datetime):
            raise TypeError("recorded_at must be a datetime")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware UTC")
        if self.recorded_at.utcoffset().total_seconds() != 0:
            raise ValueError("recorded_at must use UTC")
        all_calls = [item.call.call_id for step in self.progress for item in step.tools]
        if len(all_calls) != len(set(all_calls)):
            raise ValueError("duplicate tool call_id in checkpoint")
        incomplete_steps = [
            index
            for index, step in enumerate(self.progress)
            if any(
                item.status
                in {
                    RunCheckpointToolStatus.IN_PROGRESS,
                    RunCheckpointToolStatus.NOT_STARTED,
                }
                for item in step.tools
            )
        ]
        if incomplete_steps:
            if incomplete_steps[-1] != len(self.progress) - 1:
                raise ValueError("incomplete tool progress must be in the final step")
            if len(incomplete_steps) != 1:
                raise ValueError("incomplete tool progress must be in one final step")
            if self.partial_assistant_message is not None:
                raise ValueError(
                    "partial assistant message cannot follow incomplete tool progress"
                )

    @property
    def completed_tool_progress(self) -> tuple[RunCheckpointToolProgress, ...]:
        return tuple(
            item
            for step in self.progress
            for item in step.tools
            if item.status
            in {RunCheckpointToolStatus.SUCCESS, RunCheckpointToolStatus.FAILED}
        )


# Short aliases keep the public contract discoverable without duplicating types.
CheckpointProgress = RunCheckpointProgress
CheckpointToolProgress = RunCheckpointToolProgress
CheckpointToolStatus = RunCheckpointToolStatus

__all__ = [
    "CheckpointProgress",
    "CheckpointToolProgress",
    "CheckpointToolStatus",
    "RunCheckpoint",
    "RunCheckpointKind",
    "RunCheckpointProgress",
    "RunCheckpointToolProgress",
    "RunCheckpointToolStatus",
]
