"""只属于 TUI 的不可变视图状态。"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from mica_agent.context_builder import AgentFileSource


@dataclass(frozen=True, slots=True)
class StartupResourceSnapshot:
    """由外部装配层明确注入的安全启动事实。"""

    system_prompt_name: str | None = None
    agent_sources: tuple[AgentFileSource, ...] = ()
    tool_names: tuple[str, ...] = ()
    skill_names: tuple[str, ...] = ()
    extension_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._validate_optional_name("system_prompt_name", self.system_prompt_name)
        sources = self._copy_sequence("agent_sources", self.agent_sources)
        if not all(isinstance(source, AgentFileSource) for source in sources):
            raise TypeError("agent_sources must contain only AgentFileSource")
        expected_sources = tuple(
            source
            for source in (AgentFileSource.GLOBAL, AgentFileSource.WORKSPACE)
            if source in sources
        )
        if sources != expected_sources:
            raise ValueError(
                "agent_sources must be unique and ordered GLOBAL, WORKSPACE"
            )
        object.__setattr__(self, "agent_sources", sources)

        for field_name in ("tool_names", "skill_names", "extension_names"):
            values = self._copy_sequence(field_name, getattr(self, field_name))
            if not all(isinstance(value, str) for value in values):
                raise TypeError(f"{field_name} must contain only strings")
            if not all(self._is_safe_name(value) for value in values):
                raise ValueError(
                    f"{field_name} must contain safe single-line non-empty strings"
                )
            object.__setattr__(self, field_name, values)

    @staticmethod
    def _copy_sequence(field_name: str, value: object) -> tuple[object, ...]:
        if not isinstance(value, (tuple, list)):
            raise TypeError(f"{field_name} must be a tuple or list")
        return tuple(value)

    @classmethod
    def _validate_optional_name(cls, field_name: str, value: object) -> None:
        if value is None:
            return
        if not isinstance(value, str):
            raise TypeError(f"{field_name} must be a string or None")
        if not cls._is_safe_name(value):
            raise ValueError(
                f"{field_name} must be a safe single-line non-empty string"
            )

    @staticmethod
    def _is_safe_name(value: str) -> bool:
        return (
            bool(value.strip())
            and not PurePosixPath(value).is_absolute()
            and not PureWindowsPath(value).is_absolute()
            and not any(
                unicodedata.category(character).startswith("C")
                or unicodedata.category(character) in {"Zl", "Zp"}
                for character in value
            )
        )


__all__ = ["StartupResourceSnapshot"]
