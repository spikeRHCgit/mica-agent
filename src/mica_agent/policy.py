"""基于规范化权限事实的内存决策策略。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from .permissions import PermissionMode, PolicyDecision


class PolicyOperation(Enum):
    """权限矩阵识别的稳定操作类别。"""

    READ = "read"
    WRITE = "write"
    EDIT = "edit"
    SAFE_READ_ONLY_COMMAND = "safe_read_only_command"
    COMMAND = "command"
    ASK = "ask"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class PolicyInput:
    """后续安全边界规范化后提交的最小权限事实。"""

    tool_name: str
    operation: PolicyOperation
    hard_denied: bool
    authorized_once: bool
    protected_resource: bool = False
    command_risk: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str):
            raise TypeError("tool_name must be a str")
        if not self.tool_name.strip():
            raise ValueError("tool_name must be non-empty and non-blank")
        if not isinstance(self.operation, PolicyOperation):
            raise TypeError("operation must be a PolicyOperation")
        if not isinstance(self.hard_denied, bool):
            raise TypeError("hard_denied must be a bool")
        if not isinstance(self.authorized_once, bool):
            raise TypeError("authorized_once must be a bool")
        if not isinstance(self.protected_resource, bool):
            raise TypeError("protected_resource must be a bool")
        if not isinstance(self.command_risk, bool):
            raise TypeError("command_risk must be a bool")


_DEFAULT_MATRIX: dict[PermissionMode, dict[PolicyOperation, PolicyDecision]] = {
    PermissionMode.DEFAULT: {
        PolicyOperation.READ: PolicyDecision.ALLOW,
        PolicyOperation.WRITE: PolicyDecision.ALLOW,
        PolicyOperation.EDIT: PolicyDecision.ALLOW,
        PolicyOperation.SAFE_READ_ONLY_COMMAND: PolicyDecision.ALLOW,
        PolicyOperation.COMMAND: PolicyDecision.ASK,
        PolicyOperation.ASK: PolicyDecision.ALLOW,
        PolicyOperation.OTHER: PolicyDecision.ASK,
    },
    PermissionMode.SOLO: {
        PolicyOperation.READ: PolicyDecision.ALLOW,
        PolicyOperation.WRITE: PolicyDecision.ALLOW,
        PolicyOperation.EDIT: PolicyDecision.ALLOW,
        PolicyOperation.SAFE_READ_ONLY_COMMAND: PolicyDecision.ALLOW,
        PolicyOperation.COMMAND: PolicyDecision.ALLOW,
        PolicyOperation.ASK: PolicyDecision.ALLOW,
        PolicyOperation.OTHER: PolicyDecision.ALLOW,
    },
}


AllowRule: TypeAlias = Callable[[PolicyInput], bool]


class PolicyEngine:
    """按固定优先级决定规范化操作是否允许、询问或拒绝。"""

    def __init__(
        self,
        mode: PermissionMode,
        allow_rules: Iterable[AllowRule] = (),
    ) -> None:
        if not isinstance(mode, PermissionMode):
            raise TypeError("mode must be a PermissionMode")
        try:
            rules = tuple(allow_rules)
        except TypeError:
            raise TypeError("allow_rules must be an iterable of callables") from None
        if not all(callable(rule) for rule in rules):
            raise TypeError("allow_rules must contain only callables")
        self._mode = mode
        self._allow_rules = rules

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    def decide(self, policy_input: PolicyInput) -> PolicyDecision:
        """solo 绕过策略；default 按事实、规则和默认矩阵决定。"""
        if not isinstance(policy_input, PolicyInput):
            raise TypeError("policy_input must be a PolicyInput")
        if self._mode is PermissionMode.SOLO:
            return PolicyDecision.ALLOW
        if policy_input.hard_denied:
            return PolicyDecision.DENY
        if policy_input.authorized_once:
            return PolicyDecision.ALLOW
        if policy_input.protected_resource or policy_input.command_risk:
            return PolicyDecision.ASK
        for rule in self._allow_rules:
            try:
                matched = rule(policy_input)
            except Exception:  # noqa: BLE001, S112 - untrusted rule fails closed
                continue
            if matched is True:
                return PolicyDecision.ALLOW
        if policy_input.operation is PolicyOperation.COMMAND:
            return PolicyDecision.ALLOW
        return _DEFAULT_MATRIX[self._mode][policy_input.operation]
