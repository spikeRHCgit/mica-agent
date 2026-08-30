"""P5-11 统一思考强度公共契约测试。"""

from __future__ import annotations

import pytest

from mica_agent.reasoning import ReasoningEffort


def test_reasoning_effort_has_seven_stable_wire_values() -> None:
    assert [(effort.name, effort.value) for effort in ReasoningEffort] == [
        ("OFF", "off"),
        ("MINIMAL", "minimal"),
        ("LOW", "low"),
        ("MEDIUM", "medium"),
        ("HIGH", "high"),
        ("XHIGH", "xhigh"),
        ("MAX", "max"),
    ]


def test_reasoning_effort_is_immutable() -> None:
    with pytest.raises(AttributeError):
        ReasoningEffort.HIGH.value = "changed"  # type: ignore[misc]
