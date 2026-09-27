"""Shared base enums for the tool permission system."""

from enum import Enum


class PermissionMode(Enum):
    """Permission mode selected for the current session."""

    DEFAULT = "default"
    SOLO = "solo"


class PolicyDecision(Enum):
    """Permission decision returned by the policy engine."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
