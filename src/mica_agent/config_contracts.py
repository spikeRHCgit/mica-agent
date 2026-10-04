"""Immutable configuration views; credentials travel through a separate binding."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .permissions import PermissionMode
from .reasoning import ReasoningDisplayMode, ReasoningEffort
from .session_contracts import SessionSelection


class ConfigError(Exception):
    """Safe configuration failure; never includes untrusted input."""


class CredentialProtectionError(ConfigError):
    """Authentication protection is unconfirmed; other sources remain usable."""


class ConfigConflictError(ConfigError):
    """The target setting changed since it was read."""


@dataclass(frozen=True, slots=True)
class EffectiveSettings:
    reasoning_effort: ReasoningEffort = ReasoningEffort.OFF
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    reasoning_display: ReasoningDisplayMode = ReasoningDisplayMode.COLLAPSED
    sources: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.reasoning_effort, ReasoningEffort):
            raise TypeError("invalid reasoning preference")
        if not isinstance(self.permission_mode, PermissionMode):
            raise TypeError("invalid permission preference")
        if not isinstance(self.reasoning_display, ReasoningDisplayMode):
            raise TypeError("invalid display preference")
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    context_window: int = 200000
    max_tokens: int | None = None
    input: tuple[str, ...] = ("text",)
    reasoning: bool = False
    thinking_level_map: Mapping[ReasoningEffort, str] = field(default_factory=dict)
    sources: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value in (self.context_window, self.max_tokens):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("budget must be a positive integer")
        if type(self.reasoning) is not bool:
            raise TypeError("reasoning must be boolean")
        if (
            not isinstance(self.input, tuple)
            or not self.input
            or "text" not in self.input
            or any(v not in {"text", "image"} for v in self.input)
            or len(set(self.input)) != len(self.input)
        ):
            raise ValueError("invalid input capabilities")
        mapping = dict(self.thinking_level_map)
        if any(
            not isinstance(k, ReasoningEffort)
            or k is ReasoningEffort.OFF
            or not isinstance(v, str)
            or not v.strip()
            or not v.isprintable()
            for k, v in mapping.items()
        ):
            raise ValueError("invalid thinking map")
        object.__setattr__(self, "thinking_level_map", MappingProxyType(mapping))
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


@dataclass(frozen=True, slots=True, repr=False)
class ModelConfig:
    selection: SessionSelection
    name: str
    api: str
    capabilities: ModelCapabilities


@dataclass(frozen=True, slots=True, repr=False)
class ProviderConfig:
    provider_id: str
    name: str
    channel: str
    enabled: bool
    api: str
    models: tuple[ModelConfig, ...]
    # Transport addresses stay outside ordinary configuration snapshots.


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    settings: EffectiveSettings
    default_model: SessionSelection | None
    default_source: str
    providers: tuple[ProviderConfig, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, repr=False)
class ConnectionBinding:
    selection: SessionSelection
    api_url: str
    api_key: str
    model: ModelConfig
    source: str

    def __repr__(self) -> str:
        return "ConnectionBinding(<redacted>)"


@dataclass(frozen=True, slots=True)
class SelectionResolution:
    selection: SessionSelection | None
    source: str
    status: str
    connection: ConnectionBinding | None = field(default=None, repr=False)
    diagnostic: str | None = None
