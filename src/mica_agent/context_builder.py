"""供应商无关的启动上下文与 AGENTS.md 加载。"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .models import Message, MessageRole
from .workspace import WorkspaceIdentity

MAX_AGENTS_FILE_BYTES = 64 * 1024
"""每个 global/workspace AGENTS.md 的统一字节上限。"""

_INSTRUCTIONS_TAG_RE = re.compile(
    r"<\s*/?\s*instructions\b[^>]*>",
    flags=re.IGNORECASE,
)


class AgentFileSource(Enum):
    """AGENTS.md 的稳定来源标识。"""

    GLOBAL = "global"
    WORKSPACE = "workspace"


class ContextDiagnosticKind(Enum):
    """AGENTS.md 加载失败的稳定诊断类型。"""

    UNREADABLE = "unreadable"
    INVALID_UTF8 = "invalid_utf8"
    TOO_LARGE = "too_large"


@dataclass(frozen=True, slots=True)
class ContextDiagnostic:
    """不包含路径、内容或原始异常的安全诊断。"""

    source: AgentFileSource
    kind: ContextDiagnosticKind
    message: str


@dataclass(frozen=True, slots=True)
class ContextBuildResult:
    """一次上下文构建产生的新消息序列与诊断。"""

    messages: tuple[Message, ...]
    diagnostics: tuple[ContextDiagnostic, ...]


class ContextBuilder:
    """每次显式调用按固定顺序读取并封装 AGENTS.md。"""

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
        """重新读取两个 AGENTS.md，并返回不可变标准上下文。"""
        messages = self._validate_session_messages(session_messages)
        context: list[Message] = []
        diagnostics: list[ContextDiagnostic] = []
        for source, path in self._agent_file_paths():
            content, diagnostic = self._read_agents_file(source, path)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
            elif content:
                context.append(
                    Message(MessageRole.SYSTEM, self._wrap_agents(source, content))
                )
        context.extend(messages)
        return ContextBuildResult(tuple(context), tuple(diagnostics))

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
