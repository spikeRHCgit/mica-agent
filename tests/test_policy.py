"""P5-02 PolicyEngine 公共决策测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mica_agent.permissions import PermissionMode, PolicyDecision
from mica_agent.policy import PolicyEngine, PolicyInput, PolicyOperation


@pytest.mark.parametrize(
    ("operation", "wire_value"),
    [
        (PolicyOperation.READ, "read"),
        (PolicyOperation.WRITE, "write"),
        (PolicyOperation.EDIT, "edit"),
        (PolicyOperation.SAFE_READ_ONLY_COMMAND, "safe_read_only_command"),
        (PolicyOperation.COMMAND, "command"),
        (PolicyOperation.ASK, "ask"),
        (PolicyOperation.OTHER, "other"),
    ],
)
def test_policy_operation_wire_values(
    operation: PolicyOperation, wire_value: str
) -> None:
    assert operation.value == wire_value


def test_policy_operation_has_no_list_member() -> None:
    assert not hasattr(PolicyOperation, "LIST")


def test_policy_input_is_immutable_and_strictly_typed() -> None:
    policy_input = PolicyInput(
        tool_name="read",
        operation=PolicyOperation.READ,
        hard_denied=False,
        authorized_once=True,
    )

    with pytest.raises(FrozenInstanceError):
        policy_input.authorized_once = False  # type: ignore[misc]
    with pytest.raises(TypeError, match="tool_name"):
        PolicyInput(42, PolicyOperation.READ, False, False)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="tool_name"):
        PolicyInput("   ", PolicyOperation.READ, False, False)
    with pytest.raises(TypeError, match="operation"):
        PolicyInput("read", "read", False, False)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="hard_denied"):
        PolicyInput("read", PolicyOperation.READ, 1, False)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="authorized_once"):
        PolicyInput("read", PolicyOperation.READ, False, 0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mode", "operation", "command_risk", "expected"),
    [
        (PermissionMode.DEFAULT, PolicyOperation.READ, False, PolicyDecision.ALLOW),
        (PermissionMode.DEFAULT, PolicyOperation.WRITE, False, PolicyDecision.ALLOW),
        (PermissionMode.DEFAULT, PolicyOperation.EDIT, False, PolicyDecision.ALLOW),
        (
            PermissionMode.DEFAULT,
            PolicyOperation.SAFE_READ_ONLY_COMMAND,
            False,
            PolicyDecision.ALLOW,
        ),
        (PermissionMode.DEFAULT, PolicyOperation.COMMAND, False, PolicyDecision.ALLOW),
        (PermissionMode.DEFAULT, PolicyOperation.COMMAND, True, PolicyDecision.ASK),
        (PermissionMode.DEFAULT, PolicyOperation.ASK, False, PolicyDecision.ALLOW),
        (PermissionMode.DEFAULT, PolicyOperation.OTHER, False, PolicyDecision.ASK),
        (PermissionMode.SOLO, PolicyOperation.READ, False, PolicyDecision.ALLOW),
        (PermissionMode.SOLO, PolicyOperation.WRITE, False, PolicyDecision.ALLOW),
        (PermissionMode.SOLO, PolicyOperation.EDIT, False, PolicyDecision.ALLOW),
        (
            PermissionMode.SOLO,
            PolicyOperation.SAFE_READ_ONLY_COMMAND,
            False,
            PolicyDecision.ALLOW,
        ),
        (PermissionMode.SOLO, PolicyOperation.COMMAND, True, PolicyDecision.ALLOW),
        (PermissionMode.SOLO, PolicyOperation.ASK, False, PolicyDecision.ALLOW),
        (PermissionMode.SOLO, PolicyOperation.OTHER, False, PolicyDecision.ALLOW),
    ],
)
def test_policy_engine_default_matrix(
    mode: PermissionMode,
    operation: PolicyOperation,
    command_risk: bool,
    expected: PolicyDecision,
) -> None:
    policy_input = PolicyInput(
        "tool", operation, False, False, command_risk=command_risk
    )

    assert PolicyEngine(mode).decide(policy_input) is expected


def test_policy_engine_validates_public_inputs() -> None:
    with pytest.raises(TypeError, match="PermissionMode"):
        PolicyEngine("default")  # type: ignore[arg-type]

    engine = PolicyEngine(PermissionMode.DEFAULT)
    with pytest.raises(TypeError, match="PolicyInput"):
        engine.decide({})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="allow_rules"):
        PolicyEngine(PermissionMode.DEFAULT, allow_rules=[object()])  # type: ignore[list-item]


def test_hard_deny_overrides_authorization_and_does_not_run_rules() -> None:
    rule_calls = 0

    def allowing_rule(policy_input: PolicyInput) -> bool:
        nonlocal rule_calls
        rule_calls += 1
        return True

    engine = PolicyEngine(PermissionMode.DEFAULT, allow_rules=[allowing_rule])
    policy_input = PolicyInput("write", PolicyOperation.WRITE, True, True)

    assert engine.decide(policy_input) is PolicyDecision.DENY
    assert rule_calls == 0


def test_authorized_once_precedes_rules_and_is_not_cached() -> None:
    def exploding_rule(policy_input: PolicyInput) -> bool:
        raise RuntimeError("credential-like-rule-failure")

    engine = PolicyEngine(PermissionMode.DEFAULT, allow_rules=[exploding_rule])
    authorized = PolicyInput("write", PolicyOperation.OTHER, False, True)
    next_call = PolicyInput("write", PolicyOperation.OTHER, False, False)

    assert engine.decide(authorized) is PolicyDecision.ALLOW
    assert engine.decide(next_call) is PolicyDecision.ASK


def test_allow_rule_receives_policy_input_and_can_allow_current_call() -> None:
    received: list[PolicyInput] = []

    def matching_rule(policy_input: PolicyInput) -> bool:
        received.append(policy_input)
        return policy_input.tool_name == "approved"

    engine = PolicyEngine(PermissionMode.DEFAULT, allow_rules=[matching_rule])
    approved = PolicyInput("approved", PolicyOperation.OTHER, False, False)
    unmatched = PolicyInput("other", PolicyOperation.OTHER, False, False)

    assert engine.decide(approved) is PolicyDecision.ALLOW
    assert engine.decide(unmatched) is PolicyDecision.ASK
    assert received == [approved, unmatched]


def test_allow_rule_exception_and_non_bool_result_do_not_grant_access() -> None:
    def exploding_rule(policy_input: PolicyInput) -> bool:
        raise RuntimeError("credential-like-rule-failure")

    def unsafe_truthy_rule(policy_input: PolicyInput) -> bool:
        return "allow"  # type: ignore[return-value]

    policy_input = PolicyInput("write", PolicyOperation.OTHER, False, False)

    assert (
        PolicyEngine(PermissionMode.DEFAULT, [exploding_rule]).decide(policy_input)
        is PolicyDecision.ASK
    )
    assert (
        PolicyEngine(PermissionMode.DEFAULT, [unsafe_truthy_rule]).decide(policy_input)
        is PolicyDecision.ASK
    )
