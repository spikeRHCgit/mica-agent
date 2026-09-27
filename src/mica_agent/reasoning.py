"""Provider-agnostic model reasoning-effort contract."""

from __future__ import annotations

from enum import Enum


class ReasoningEffort(Enum):
    """Immutable reasoning effort shared across Mica."""

    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


__all__ = ["ReasoningEffort"]
