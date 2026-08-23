"""P5 权限系统共享的基础枚举。"""

from enum import Enum


class PermissionMode(Enum):
    """当前会话使用的权限模式。"""

    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    SOLO = "solo"


class PolicyDecision(Enum):
    """后续策略引擎返回的权限决定。"""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
