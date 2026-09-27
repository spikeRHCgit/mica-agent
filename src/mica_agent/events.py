"""Ordered public events emitted by the engine.

Every event carries ``run_id`` (the associated run) and ``seq`` (a monotonically
increasing sequence number within the run); the CLI and frontend can consume them in
order without reading provider-private state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .interactions import AskPrompt
from .tool_contracts import ToolResultStatus, _validate_tool_name
from .tool_presentation import (
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)


def _validate_run_id(run_id: str) -> None:
    """``run_id`` must be a non-empty, non-blank string."""
    if not isinstance(run_id, str):
        raise TypeError(f"run_id must be a str, got {type(run_id).__name__}")
    if not run_id.strip():
        raise ValueError("run_id must be a non-empty, non-blank string")


def _validate_seq(seq: int) -> None:
    """``seq`` must be a non-negative integer (``bool`` excluded)."""
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise TypeError(f"seq must be an int, got {type(seq).__name__}")
    if seq < 0:
        raise ValueError("seq must be a non-negative integer")


def _validate_message(message: str) -> None:
    """The user-facing diagnostic message must be a non-empty, non-blank string."""
    _validate_non_blank_text(message, "message")


def _validate_non_blank_text(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a str, got {type(value).__name__}")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty, non-blank string")


class RunStatus(Enum):
    """Unified run terminal status: completed, failed, or cancelled."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorCategory(Enum):
    """Error category of a failed run, used for unified user-facing diagnostics and retry
    decisions.

    ``UNKNOWN`` is the fallback category for unrecognized errors.
    """

    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    TEMPORARY = "temporary"
    MALFORMED = "malformed"
    STORAGE = "storage"
    LIMIT = "limit"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunStarted:
    """Run start; the first event of a run, with ``seq`` fixed to 0."""

    run_id: str
    seq: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    """Ordered model reasoning delta that is not part of the assistant text."""

    run_id: str
    text: str
    seq: int

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)
        if not isinstance(self.text, str):
            raise TypeError("text must be a str")
        if not self.text:
            raise ValueError("text must not be empty")


@dataclass(frozen=True, slots=True)
class TextDelta:
    """Ordered text delta; appending them in order reconstructs the complete reply."""

    run_id: str
    text: str
    seq: int

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)


@dataclass(frozen=True, slots=True)
class ToolOutputDelta:
    """Ordered, already sanitized tool output delta."""

    run_id: str
    seq: int
    call_id: str
    tool_name: str
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)
        _validate_non_blank_text(self.call_id, "call_id")
        _validate_tool_name(self.tool_name, "tool_name")
        if not isinstance(self.text, str):
            raise TypeError("text must be a str")
        if not self.text:
            raise ValueError("text must not be empty")


@dataclass(frozen=True, slots=True)
class ToolPrepared:
    """A tool call has been proposed but is not yet authorized or executed."""

    run_id: str
    seq: int
    presentation: ToolPresentationSnapshot

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)
        if not isinstance(self.presentation, ToolPresentationSnapshot):
            raise TypeError("presentation must be a ToolPresentationSnapshot")
        if self.presentation.status is not ToolPresentationStatus.PENDING:
            raise ValueError("prepared presentation must be pending")

    @property
    def call_id(self) -> str:
        """Read the stable tool call id from the presentation snapshot."""
        return self.presentation.call_id

    @property
    def tool_name(self) -> str:
        """Read the stable tool name from the presentation snapshot."""
        return self.presentation.tool_name


@dataclass(frozen=True, slots=True)
class ToolStarted:
    """One tool call started; tool arguments are not included."""

    run_id: str
    seq: int
    call_id: str
    tool_name: str

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)
        _validate_non_blank_text(self.call_id, "call_id")
        _validate_tool_name(self.tool_name, "tool_name")


@dataclass(frozen=True, slots=True)
class ToolFinished:
    """One tool call finished; only the status is exposed, not the full result."""

    run_id: str
    seq: int
    call_id: str
    tool_name: str
    status: ToolResultStatus
    presentation: ToolPresentationSnapshot | None = None

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)
        _validate_non_blank_text(self.call_id, "call_id")
        _validate_tool_name(self.tool_name, "tool_name")
        if not isinstance(self.status, ToolResultStatus):
            raise TypeError(
                f"status must be a ToolResultStatus, got {type(self.status).__name__}"
            )
        if self.presentation is not None:
            if not isinstance(self.presentation, ToolPresentationSnapshot):
                raise TypeError("presentation must be a ToolPresentationSnapshot")
            if (
                self.presentation.call_id != self.call_id
                or self.presentation.tool_name != self.tool_name
            ):
                raise ValueError("presentation identity must match tool event")
            expected_status = (
                ToolPresentationStatus.SUCCESS
                if self.status is ToolResultStatus.SUCCESS
                else ToolPresentationStatus.CANCELLED
                if self.status is ToolResultStatus.CANCELLED
                else ToolPresentationStatus.FAILED
            )
            if self.presentation.status is not expected_status:
                raise ValueError("presentation status does not match tool result")


@dataclass(frozen=True, slots=True)
class PermissionRequested:
    """The engine asks the frontend for one tool permission answer."""

    run_id: str
    seq: int
    interaction_id: str
    call_id: str
    tool_name: str
    message: str

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)
        _validate_non_blank_text(self.interaction_id, "interaction_id")
        _validate_non_blank_text(self.call_id, "call_id")
        _validate_tool_name(self.tool_name, "tool_name")
        _validate_message(self.message)


@dataclass(frozen=True, slots=True)
class UserInputRequested:
    """The engine asks the frontend for one text answer."""

    run_id: str
    seq: int
    interaction_id: str
    question: str
    prompt: AskPrompt | None = None

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)
        _validate_non_blank_text(self.interaction_id, "interaction_id")
        _validate_non_blank_text(self.question, "question")
        if self.prompt is None:
            object.__setattr__(self, "prompt", AskPrompt(self.question))
        elif not isinstance(self.prompt, AskPrompt):
            raise TypeError("prompt must be an AskPrompt or None")
        elif self.prompt.question != self.question:
            raise ValueError("prompt question must match question")


@dataclass(frozen=True, slots=True)
class RunCompleted:
    """Run completed normally; ``status`` is fixed to ``COMPLETED`` and cannot be
    overridden."""

    run_id: str
    seq: int
    status: RunStatus = field(init=False, default=RunStatus.COMPLETED)

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)


@dataclass(frozen=True, slots=True)
class RunFailed:
    """Run failed: carries the error category and a user-facing diagnostic message.

    ``status`` is fixed to ``FAILED`` and cannot be overridden.
    """

    run_id: str
    message: str
    category: ErrorCategory
    seq: int
    status: RunStatus = field(init=False, default=RunStatus.FAILED)

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_message(self.message)
        if not isinstance(self.category, ErrorCategory):
            raise TypeError(
                f"category must be an ErrorCategory, got {type(self.category).__name__}"
            )
        _validate_seq(self.seq)


@dataclass(frozen=True, slots=True)
class RunCancelled:
    """Run cancelled by the user or the run context; ``status`` is fixed to
    ``CANCELLED``."""

    run_id: str
    seq: int
    status: RunStatus = field(init=False, default=RunStatus.CANCELLED)

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)


EngineEvent = (
    RunStarted
    | ReasoningDelta
    | TextDelta
    | ToolOutputDelta
    | ToolPrepared
    | ToolStarted
    | ToolFinished
    | PermissionRequested
    | UserInputRequested
    | RunCompleted
    | RunFailed
    | RunCancelled
)
"""Engine 向前端输出的统一事件类型。"""
