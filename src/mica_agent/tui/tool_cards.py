"""Textual tool-card views for safe, in-transcript tool lifecycle projection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from enum import Enum
from typing import ClassVar, Self

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Static

from mica_agent.tool_contracts import ToolResultStatus
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)


class ToolCardState(Enum):
    """User-visible lifecycle state of a tool card."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


_SAFE_FAILURE_MESSAGES = {
    ToolResultStatus.CANCELLED: "operation cancelled",
    ToolResultStatus.PERMISSION_DENIED: "permission denied",
    ToolResultStatus.INVALID_ARGUMENTS: "tool arguments are invalid",
    ToolResultStatus.UNKNOWN_TOOL: "tool execution failed",
    ToolResultStatus.EXECUTION_FAILED: "tool execution failed",
}


class ToolCard(Static):
    """Base card with one stable widget identity across lifecycle updates.

    The card only receives a safe :class:`ToolPresentationSnapshot`; it never
    stores a request, result, runtime object, or raw tool arguments.
    """

    DEFAULT_CSS = """
    ToolCard {
        width: 100%;
        height: auto;
        min-height: 1;
        padding: 0 1 0 3;
        margin-bottom: 1;
        background: #172554;
        color: $foreground;
    }
    ToolCard.card-pending, ToolCard.card-running {
        background: #172554;
    }
    ToolCard.card-success {
        background: #052e16;
    }
    ToolCard.card-failed, ToolCard.card-cancelled {
        background: #450a0a;
    }
    """

    def __init__(
        self,
        snapshot: ToolPresentationSnapshot,
        *,
        state: ToolCardState | None = None,
        id: str | None = None,
    ) -> None:
        if not isinstance(snapshot, ToolPresentationSnapshot):
            raise TypeError("snapshot must be a ToolPresentationSnapshot")
        self._snapshot = snapshot
        self._state = state or self._state_for_snapshot(snapshot)
        super().__init__(id=id)
        self._apply_state_class()

    @staticmethod
    def _state_for_snapshot(snapshot: ToolPresentationSnapshot) -> ToolCardState:
        return {
            ToolPresentationStatus.PENDING: ToolCardState.PENDING,
            ToolPresentationStatus.SUCCESS: ToolCardState.SUCCESS,
            ToolPresentationStatus.FAILED: ToolCardState.FAILED,
            ToolPresentationStatus.CANCELLED: ToolCardState.CANCELLED,
        }[snapshot.status]

    @property
    def snapshot(self) -> ToolPresentationSnapshot:
        """Current immutable safe display snapshot."""
        return self._snapshot

    @property
    def state(self) -> ToolCardState:
        """Current card state."""
        return self._state

    @property
    def call_id(self) -> str:
        """Stable identity used internally by the TUI event projector."""
        return self._snapshot.call_id

    @property
    def tool_name(self) -> str:
        return self._snapshot.tool_name

    def start(self) -> Self:
        """Move a prepared card into the executing state."""
        if self._state is ToolCardState.PENDING:
            self._state = ToolCardState.RUNNING
            self._apply_state_class()
            self.refresh(layout=True)
        return self

    def finish(
        self,
        status: ToolResultStatus,
        presentation: ToolPresentationSnapshot | None = None,
        *,
        message: str | None = None,
    ) -> Self:
        """Close this card in place, retaining its widget identity."""
        if not isinstance(status, ToolResultStatus):
            raise TypeError("status must be a ToolResultStatus")
        if self._state in {
            ToolCardState.SUCCESS,
            ToolCardState.FAILED,
            ToolCardState.CANCELLED,
        }:
            return self
        if presentation is not None:
            expected_status = {
                ToolResultStatus.SUCCESS: ToolPresentationStatus.SUCCESS,
                ToolResultStatus.CANCELLED: ToolPresentationStatus.CANCELLED,
            }.get(status, ToolPresentationStatus.FAILED)
            if presentation.status is not expected_status:
                raise ValueError("presentation status must match tool result")
            if (
                presentation.call_id != self.call_id
                or presentation.tool_name != self.tool_name
                or presentation.kind is not self._snapshot.kind
            ):
                raise ValueError("presentation identity must match card")
            self._snapshot = presentation
        else:
            final_status = {
                ToolResultStatus.SUCCESS: ToolPresentationStatus.SUCCESS,
                ToolResultStatus.CANCELLED: ToolPresentationStatus.CANCELLED,
            }.get(status, ToolPresentationStatus.FAILED)
            if message is None and status is not ToolResultStatus.SUCCESS:
                message = _SAFE_FAILURE_MESSAGES[status]
            final_payload = self._snapshot.payload
            if (
                self._snapshot.kind is ToolPresentationKind.EDIT
                and final_payload.get("matched") is None
            ):
                final_payload = dict(final_payload)
                final_payload["matched"] = False
            self._snapshot = replace(
                self._snapshot,
                status=final_status,
                payload=final_payload,
                message=message,
            )
        self._state = {
            ToolResultStatus.SUCCESS: ToolCardState.SUCCESS,
            ToolResultStatus.CANCELLED: ToolCardState.CANCELLED,
        }.get(status, ToolCardState.FAILED)
        self._apply_state_class()
        self.refresh(layout=True)
        return self

    def _apply_state_class(self) -> None:
        self.set_class(
            False,
            *(
                "card-pending",
                "card-running",
                "card-success",
                "card-failed",
                "card-cancelled",
            ),
        )
        self.add_class(f"card-{self._state.value}")

    def _display_path(self) -> str:
        path = self._snapshot.payload.get("path")
        return path if isinstance(path, str) and path else "unavailable"

    def _notify_transcript_resize(self) -> None:
        from .transcript import ConversationTranscript

        parent = self.parent
        while parent is not None:
            if isinstance(parent, ConversationTranscript):
                parent.content_resized()
                return
            parent = parent.parent

    def render(self) -> Text:
        text = Text()
        text.append(self.tool_name, style="bold")
        text.append("  ")
        text.append(self._display_path(), style="underline #7dd3fc")
        if self._snapshot.message is not None:
            text.append(" · ")
            text.append(self._snapshot.message, style="#94a3b8")
        return text

    def __repr__(self) -> str:
        return "<ToolCard>"


class WriteToolCard(ToolCard):
    """Plain-text file preview card with a ten-line display fold."""

    can_focus = True
    BINDINGS: ClassVar = [Binding("ctrl+o", "toggle_expanded", show=False)]
    MAX_VISIBLE_LINES = 10

    DEFAULT_CSS = """
    WriteToolCard {
        height: auto;
        text-wrap: wrap;
    }
    """

    def __init__(
        self,
        snapshot: ToolPresentationSnapshot,
        *,
        id: str | None = None,
    ) -> None:
        if snapshot.kind is not ToolPresentationKind.WRITE:
            raise ValueError("WriteToolCard requires a write presentation")
        if not isinstance(snapshot.payload.get("content"), str):
            raise TypeError("write presentation content must be a string")
        self._expanded = False
        super().__init__(snapshot, id=id)

    @property
    def expanded(self) -> bool:
        """Whether the complete content preview is currently displayed."""
        return self._expanded

    @property
    def logical_line_count(self) -> int:
        """Number of logical content lines, independent of terminal wrapping."""
        return len(self._content_lines())

    def action_toggle_expanded(self) -> None:
        """Toggle only this card's display fold when content needs one."""
        if self.logical_line_count <= self.MAX_VISIBLE_LINES:
            return
        self._expanded = not self._expanded
        self.refresh(layout=True)
        self._notify_transcript_resize()

    def _content_lines(self) -> list[str]:
        content = self._snapshot.payload["content"]
        assert isinstance(content, str)
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.removesuffix("\n").split("\n")

    def render(self) -> Text:
        text = Text()
        text.append(self.tool_name, style="bold")
        text.append("  ")
        text.append(self._display_path(), style="underline #7dd3fc")
        text.append("\n\n")

        lines = self._content_lines()
        total = len(lines)
        visible = lines if self._expanded else lines[: self.MAX_VISIBLE_LINES]
        for index, line in enumerate(visible):
            if index:
                text.append("\n")
            text.append(line)
        if total > self.MAX_VISIBLE_LINES:
            text.append("\n")
            if self._expanded:
                fold = f"... ({total} total, ctrl+o to collapse)"
            else:
                fold = (
                    f"... ({total - self.MAX_VISIBLE_LINES} more lines, "
                    f"{total} total, ctrl+o to expand)"
                )
            text.append(fold, style="#94a3b8")
        if self._snapshot.message is not None:
            text.append("\n")
            text.append(self._snapshot.message, style="#94a3b8")
        return text

    def __repr__(self) -> str:
        return "<WriteToolCard>"


class EditToolCard(ToolCard):
    """Line-oriented edit diff card with a twenty-change display fold."""

    can_focus = True
    BINDINGS: ClassVar = [Binding("ctrl+o", "toggle_expanded", show=False)]
    MAX_VISIBLE_CHANGES = 20
    MAX_RETAINED_CHANGES = 10

    DEFAULT_CSS = """
    EditToolCard {
        height: auto;
        text-wrap: wrap;
    }
    """

    _LINE_STYLES: ClassVar[dict[str, str]] = {
        "context": "#e2e8f0",
        "removed": "#fca5a5",
        "added": "#bef264",
    }

    def __init__(
        self,
        snapshot: ToolPresentationSnapshot,
        *,
        id: str | None = None,
    ) -> None:
        if snapshot.kind is not ToolPresentationKind.EDIT:
            raise ValueError("EditToolCard requires an edit presentation")
        self._expanded = False
        super().__init__(snapshot, id=id)

    @property
    def expanded(self) -> bool:
        """Whether the complete diff is currently displayed."""
        return self._expanded

    @property
    def matched(self) -> bool | None:
        """Whether the persisted edit detail found a unique match."""
        value = self._snapshot.payload["matched"]
        return value if isinstance(value, bool) else None

    @property
    def change_line_count(self) -> int:
        """Number of added and removed lines in the immutable snapshot."""
        return sum(1 for line in self._lines() if line["kind"] in {"removed", "added"})

    def action_toggle_expanded(self) -> None:
        """Toggle only this card's display fold when the diff needs one."""
        if self.change_line_count <= self.MAX_VISIBLE_CHANGES:
            return
        self._expanded = not self._expanded
        self.refresh(layout=True)
        self._notify_transcript_resize()

    def _lines(self) -> tuple[Mapping[str, object], ...]:
        lines = self._snapshot.payload["lines"]
        assert isinstance(lines, tuple)
        return lines

    def _visible_indices(self) -> tuple[int, ...]:
        lines = self._lines()
        if self._expanded or self.change_line_count <= self.MAX_VISIBLE_CHANGES:
            return tuple(range(len(lines)))
        change_indices = [
            index
            for index, line in enumerate(lines)
            if line["kind"] in {"removed", "added"}
        ]
        first_end = change_indices[self.MAX_RETAINED_CHANGES - 1]
        while first_end + 1 < len(lines) and lines[first_end + 1]["kind"] == "context":
            first_end += 1
        last_start = change_indices[-self.MAX_RETAINED_CHANGES]
        while last_start > 0 and lines[last_start - 1]["kind"] == "context":
            last_start -= 1
        return tuple(range(first_end + 1)) + tuple(range(last_start, len(lines)))

    def _line_number_width(self, indices: tuple[int, ...]) -> int:
        numbers = []
        for index in indices:
            line = self._lines()[index]
            if self.matched is not True:
                continue
            number = (
                line["new_line"]
                if line["kind"] in {"context", "added"}
                else line["old_line"]
            )
            if isinstance(number, int):
                numbers.append(number)
        return max((len(str(number)) for number in numbers), default=1)

    def _append_diff_line(
        self, text: Text, line: Mapping[str, object], width: int
    ) -> None:
        kind = line["kind"]
        assert isinstance(kind, str)
        style = self._LINE_STYLES[kind]
        value = line["text"]
        assert isinstance(value, str)
        if self.matched is not True:
            marker = "  " if kind == "context" else "+ " if kind == "added" else "- "
            text.append(marker, style=style)
            text.append(value, style=style)
            return
        number = line["new_line"] if kind in {"context", "added"} else line["old_line"]
        marker = "  " if kind == "context" else "+ " if kind == "added" else "- "
        number_text = (
            str(number).rjust(width) if isinstance(number, int) else " " * width
        )
        text.append(f"{marker}{number_text}  ", style=style)
        text.append(value, style=style)

    def render(self) -> Text:
        text = Text()
        text.append(self.tool_name, style="bold")
        text.append("  ")
        text.append(self._display_path(), style="underline #7dd3fc")
        text.append("\n\n")

        lines = self._lines()
        indices = self._visible_indices()
        width = self._line_number_width(indices)
        previous = None
        for index in indices:
            if previous is not None:
                text.append("\n")
                if index != previous + 1:
                    hidden = self.change_line_count - self.MAX_VISIBLE_CHANGES
                    text.append(
                        f"... ({hidden} change lines hidden, ctrl+o to expand)\n",
                        style="#94a3b8",
                    )
            self._append_diff_line(text, lines[index], width)
            previous = index
        if self._expanded and self.change_line_count > self.MAX_VISIBLE_CHANGES:
            text.append(
                f"\n... ({self.change_line_count} change lines, ctrl+o to collapse)",
                style="#94a3b8",
            )
        reason = self._safe_reason()
        if reason is not None:
            text.append("\n")
            text.append(reason, style="#94a3b8")
        return text

    def _safe_reason(self) -> str | None:
        message = self._snapshot.message
        if self.matched is False and message != "file was not changed":
            return (
                f"{message} · file was not changed"
                if message is not None
                else "file was not changed"
            )
        return message

    def __repr__(self) -> str:
        return "<EditToolCard>"


class ReadToolCard(ToolCard):
    """Compact single-line card for the built-in ``read`` tool."""

    DEFAULT_CSS = """
    ReadToolCard {
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def __init__(
        self,
        snapshot: ToolPresentationSnapshot,
        *,
        id: str | None = None,
    ) -> None:
        if snapshot.kind is not ToolPresentationKind.READ:
            raise ValueError("ReadToolCard requires a read presentation")
        super().__init__(snapshot, id=id)

    def __repr__(self) -> str:
        return "<ReadToolCard>"


__all__ = [
    "EditToolCard",
    "ReadToolCard",
    "ToolCard",
    "ToolCardState",
    "ToolCardStatus",
    "WriteToolCard",
]

# Stable descriptive alias for callers that prefer the status terminology.
ToolCardStatus = ToolCardState
