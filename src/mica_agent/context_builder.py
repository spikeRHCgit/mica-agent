"""Provider-agnostic startup context and AGENTS.md loading."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .models import Message, MessageRole
from .workspace import WorkspaceIdentity

# Maximum size in bytes for each global or workspace AGENTS.md file.
MAX_AGENTS_FILE_BYTES = 64 * 1024

_INSTRUCTIONS_TAG_RE = re.compile(
    r"<\s*/?\s*instructions\b[^>]*>",
    flags=re.IGNORECASE,
)


class AgentFileSource(Enum):
    """Stable source identifier for AGENTS.md."""

    GLOBAL = "global"
    WORKSPACE = "workspace"


class ContextDiagnosticKind(Enum):
    """Stable diagnostic kinds for AGENTS.md load failures."""

    UNREADABLE = "unreadable"
    INVALID_UTF8 = "invalid_utf8"
    TOO_LARGE = "too_large"


@dataclass(frozen=True, slots=True)
class ContextDiagnostic:
    """Safe diagnostic that carries no path, content, or original exception."""

    source: AgentFileSource
    kind: ContextDiagnosticKind
    message: str


@dataclass(frozen=True, slots=True)
class ContextBuildResult:
    """Messages, diagnostics, and loaded sources produced by one context build."""

    messages: tuple[Message, ...]
    diagnostics: tuple[ContextDiagnostic, ...]
    loaded_sources: tuple[AgentFileSource, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.loaded_sources, tuple):
            raise TypeError("loaded_sources must be a tuple of AgentFileSource")
        if not all(
            isinstance(source, AgentFileSource) for source in self.loaded_sources
        ):
            raise TypeError("loaded_sources must contain only AgentFileSource")
        expected_order = (
            source
            for source in (AgentFileSource.GLOBAL, AgentFileSource.WORKSPACE)
            if source in self.loaded_sources
        )
        if self.loaded_sources != tuple(expected_order):
            raise ValueError(
                "loaded_sources must be unique and ordered GLOBAL, WORKSPACE"
            )


class ContextBuilder:
    """Reads and wraps AGENTS.md in a fixed order on each explicit call."""

    def __init__(
        self,
        *,
        data_root: Path | str | os.PathLike[str],
        workspace: WorkspaceIdentity,
        max_agents_file_bytes: int = MAX_AGENTS_FILE_BYTES,
    ) -> None:
        if not isinstance(workspace, WorkspaceIdentity):
            raise TypeError("workspace must be a WorkspaceIdentity")
        if isinstance(max_agents_file_bytes, bool) or not isinstance(
            max_agents_file_bytes, int
        ):
            raise TypeError("max_agents_file_bytes must be an int")
        if max_agents_file_bytes <= 0:
            raise ValueError("max_agents_file_bytes must be positive")
        self.data_root = Path(data_root)
        self.workspace = workspace
        self.max_agents_file_bytes = max_agents_file_bytes

    def build(
        self,
        session_messages: Iterable[Message] = (),
    ) -> ContextBuildResult:
        """Re-read both AGENTS.md files and return immutable standard context."""
        messages = self._validate_session_messages(session_messages)
        context: list[Message] = []
        diagnostics: list[ContextDiagnostic] = []
        loaded_sources: list[AgentFileSource] = []
        for source, path in self._agent_file_paths():
            content, diagnostic = self._read_agents_file(source, path)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
            elif content:
                context.append(
                    Message(MessageRole.SYSTEM, self._wrap_agents(source, content))
                )
                loaded_sources.append(source)
        context.extend(messages)
        return ContextBuildResult(
            tuple(context),
            tuple(diagnostics),
            tuple(loaded_sources),
        )

    def _agent_file_paths(
        self,
    ) -> tuple[tuple[AgentFileSource, Path], tuple[AgentFileSource, Path]]:
        return (
            (AgentFileSource.GLOBAL, self.data_root / "AGENTS.md"),
            (AgentFileSource.WORKSPACE, self.workspace.workspace_path / "AGENTS.md"),
        )

    def _read_agents_file(
        self,
        source: AgentFileSource,
        path: Path,
    ) -> tuple[str | None, ContextDiagnostic | None]:
        try:
            with path.open("rb") as stream:
                data = stream.read(self.max_agents_file_bytes + 1)
        except FileNotFoundError:
            return None, None
        except OSError:
            return None, self._diagnostic(source, ContextDiagnosticKind.UNREADABLE)

        if len(data) > self.max_agents_file_bytes:
            return None, self._diagnostic(source, ContextDiagnosticKind.TOO_LARGE)
        try:
            content = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None, self._diagnostic(source, ContextDiagnosticKind.INVALID_UTF8)
        return content, None

    @staticmethod
    def _wrap_agents(source: AgentFileSource, content: str) -> str:
        def escape_boundary(match: re.Match[str]) -> str:
            return (
                match.group(0)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )

        protected = _INSTRUCTIONS_TAG_RE.sub(escape_boundary, content)
        separator = "" if protected.endswith("\n") else "\n"
        return (
            f'<INSTRUCTIONS source="{source.value}">\n'
            f"{protected}{separator}</INSTRUCTIONS>"
        )

    @staticmethod
    def _validate_session_messages(
        session_messages: Iterable[Message],
    ) -> tuple[Message, ...]:
        try:
            messages = tuple(session_messages)
        except TypeError as exc:
            raise TypeError("session_messages must be an iterable of Message") from exc
        for index, message in enumerate(messages):
            if not isinstance(message, Message):
                raise TypeError(f"session_messages[{index}] must be a Message")
        return messages

    @staticmethod
    def _diagnostic(
        source: AgentFileSource,
        kind: ContextDiagnosticKind,
    ) -> ContextDiagnostic:
        descriptions = {
            ContextDiagnosticKind.UNREADABLE: "AGENTS.md is unreadable",
            ContextDiagnosticKind.INVALID_UTF8: "AGENTS.md is not valid UTF-8",
            ContextDiagnosticKind.TOO_LARGE: "AGENTS.md exceeds the size limit",
        }
        return ContextDiagnostic(
            source=source,
            kind=kind,
            message=f"{source.value} {descriptions[kind]}",
        )


__all__ = [
    "MAX_AGENTS_FILE_BYTES",
    "AgentFileSource",
    "ContextBuildResult",
    "ContextBuilder",
    "ContextDiagnostic",
    "ContextDiagnosticKind",
]
