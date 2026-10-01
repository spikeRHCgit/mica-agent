"""权限基础枚举测试。"""

from __future__ import annotations

import pytest

from mica_agent.permissions import PermissionMode, PolicyDecision


@pytest.mark.parametrize(
    ("mode", "wire_value"),
    [
        (PermissionMode.DEFAULT, "default"),
        (PermissionMode.SOLO, "solo"),
    ],
)
def test_permission_mode_wire_values(mode: PermissionMode, wire_value: str) -> None:
    assert mode.value == wire_value


@pytest.mark.parametrize(
    ("decision", "wire_value"),
    [
        (PolicyDecision.ALLOW, "allow"),
        (PolicyDecision.ASK, "ask"),
        (PolicyDecision.DENY, "deny"),
    ],
)
def test_policy_decision_wire_values(decision: PolicyDecision, wire_value: str) -> None:
    assert decision.value == wire_value
