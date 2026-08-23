"""Engine 输出的有序公共事件（阶段计划 4.3）。

所有事件携带 ``run_id``（关联 Run）和 ``seq``（Run 内单调递增的
顺序号），CLI 与前端可以按序消费，无需读取 Provider 私有状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .tool_contracts import ToolResultStatus, _validate_tool_name


def _validate_run_id(run_id: str) -> None:
    """run_id 必须是非空、非纯空白字符串。"""
    if not isinstance(run_id, str):
        raise TypeError(f"run_id must be a str, got {type(run_id).__name__}")
    if not run_id.strip():
        raise ValueError("run_id must be a non-empty, non-blank string")


def _validate_seq(seq: int) -> None:
    """seq 必须是非负整数（排斥 bool）。"""
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise TypeError(f"seq must be an int, got {type(seq).__name__}")
    if seq < 0:
        raise ValueError("seq must be a non-negative integer")


def _validate_message(message: str) -> None:
    """面向用户的诊断消息必须是非空、非纯空白字符串。"""
    _validate_non_blank_text(message, "message")


def _validate_non_blank_text(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a str, got {type(value).__name__}")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty, non-blank string")


class RunStatus(Enum):
    """Run 统一终态：正常完成、失败或取消。"""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ErrorCategory(Enum):
    """Run 失败的错误类别，用于面向用户的统一诊断与后续可重试判断。

    ``UNKNOWN`` 为兜底类别，对应未识别错误。
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
    """Run 开始；作为 Run 的第一个事件，seq 固定为 0。"""

    run_id: str
    seq: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)


@dataclass(frozen=True, slots=True)
class TextDelta:
    """按序排列的文本增量，CLI 将其连续追加即可还原完整回复。"""

    run_id: str
    text: str
    seq: int

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)


@dataclass(frozen=True, slots=True)
class ToolStarted:
    """一次工具调用开始，不包含工具参数。"""

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
    """一次工具调用结束，只公开状态而不公开完整结果。"""

    run_id: str
    seq: int
    call_id: str
    tool_name: str
    status: ToolResultStatus

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)
        _validate_non_blank_text(self.call_id, "call_id")
        _validate_tool_name(self.tool_name, "tool_name")
        if not isinstance(self.status, ToolResultStatus):
            raise TypeError("status must be a ToolResultStatus")


@dataclass(frozen=True, slots=True)
class PermissionRequested:
    """Engine 请求前端取得一次工具权限回答。"""

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
    """Engine 请求前端取得一个文本回答。"""

    run_id: str
    seq: int
    interaction_id: str
    question: str

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)
        _validate_non_blank_text(self.interaction_id, "interaction_id")
        _validate_non_blank_text(self.question, "question")


@dataclass(frozen=True, slots=True)
class RunCompleted:
    """Run 正常完成；status 固定为 ``COMPLETED``，调用者不能覆盖。"""

    run_id: str
    seq: int
    status: RunStatus = field(init=False, default=RunStatus.COMPLETED)

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)


@dataclass(frozen=True, slots=True)
class RunFailed:
    """Run 失败：携带错误类别和面向用户的诊断消息。

    status 固定为 ``FAILED``，调用者不能覆盖。
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
    """Run 被用户或运行上下文取消；status 固定为 ``CANCELLED``。"""

    run_id: str
    seq: int
    status: RunStatus = field(init=False, default=RunStatus.CANCELLED)

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_seq(self.seq)


EngineEvent = (
    RunStarted
    | TextDelta
    | ToolStarted
    | ToolFinished
    | PermissionRequested
    | UserInputRequested
    | RunCompleted
    | RunFailed
    | RunCancelled
)
"""Engine 向前端输出的统一事件类型。"""
