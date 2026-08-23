"""Engine 与前端之间共享的最小交互响应。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


def _validate_interaction_id(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("interaction_id must be a str")
    if not value.strip():
        raise ValueError("interaction_id must be non-empty and non-blank")


class PermissionAnswer(Enum):
    """权限请求只允许对本次调用作答。"""

    ALLOW_ONCE = "allow_once"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PermissionResponse:
    """对一个权限交互的一次性响应。"""

    interaction_id: str
    answer: PermissionAnswer

    def __post_init__(self) -> None:
        _validate_interaction_id(self.interaction_id)
        if not isinstance(self.answer, PermissionAnswer):
            raise TypeError("answer must be a PermissionAnswer")


@dataclass(frozen=True, slots=True)
class UserInputResponse:
    """对一个用户输入交互的文本响应。"""

    interaction_id: str
    answer: str

    def __post_init__(self) -> None:
        _validate_interaction_id(self.interaction_id)
        if not isinstance(self.answer, str):
            raise TypeError("answer must be a str")
