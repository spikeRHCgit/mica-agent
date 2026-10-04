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


class ReasoningDisplayMode(Enum):
    """Presentation preference independent of provider reasoning content."""

    FULL = "full"
    COLLAPSED = "collapsed"
    HIDDEN = "hidden"


__all__ = ["ReasoningDisplayMode", "ReasoningEffort"]
