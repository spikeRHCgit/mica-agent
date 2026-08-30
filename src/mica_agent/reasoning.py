"""供应商无关的模型思考强度契约。"""

from __future__ import annotations

from enum import Enum


class ReasoningEffort(Enum):
    """Mica 统一使用的不可变思考强度。"""

    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


__all__ = ["ReasoningEffort"]
