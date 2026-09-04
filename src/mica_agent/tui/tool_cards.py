"""Textual tool-card views for safe, in-transcript tool lifecycle projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from enum import Enum
from typing import ClassVar, Self

from rich.console import Console
from rich.text import Text
from textual.binding import Binding
from textual.events import Key
from textual.widgets import Static

from mica_agent.tool_contracts import (
    ToolResultStatus,
    _validate_tool_name,
)
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


class BashToolCard(ToolCard):
    """Safe bash command card with streaming output and bounded viewports."""

    can_focus = True
    BINDINGS: ClassVar = [Binding("ctrl+o", "toggle_expanded", show=False)]
    MAX_COMMAND_DISPLAY_LINES = 3
    MAX_OUTPUT_LINES = 8

    DEFAULT_CSS = """
    BashToolCard {
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
        if snapshot.kind is not ToolPresentationKind.BASH:
            raise ValueError("BashToolCard requires a bash presentation")
        command = snapshot.payload.get("command")
        if not isinstance(command, str):
            raise TypeError("bash presentation command must be a string")
        output = snapshot.payload.get("output", "")
        if not isinstance(output, str):
            raise TypeError("bash presentation output must be a string")
        self._expanded = False
        self._output = output
        self._output_start = 0
        self._output_following = True
        self._unread_lines = 0
        super().__init__(snapshot, id=id)
        self._output_start = self._latest_output_start()

    @property
    def expanded(self) -> bool:
        """Whether the complete command and terminal output are displayed."""
        return self._expanded

    @property
    def output(self) -> str:
        """The unmodified safe output received by the card."""
        return self._output

    @property
    def output_following(self) -> bool:
        """Whether new output is shown at the latest retained lines."""
        return self._output_following

    @property
    def output_start(self) -> int:
        """Logical line index at the start of the current output viewport."""
        return self._output_start

    @property
    def unread_lines(self) -> int:
        """Number of logical lines received while browsing older output."""
        return self._unread_lines

    @property
    def logical_output_line_count(self) -> int:
        return len(self._logical_output_lines())

    def append_output(self, value: str) -> None:
        """Append one already-sanitized output delta in event order."""
        if not isinstance(value, str):
            raise TypeError("output delta must be a string")
        if self.state is not ToolCardState.RUNNING or not value:
            return
        before = self.logical_output_line_count
        self._output += value
        after = self.logical_output_line_count
        if not self._output_following:
            self._unread_lines += max(0, after - before)
        self._clamp_output_start()
        self.refresh(layout=True)
        self._notify_transcript_resize()

    def finish(
        self,
        status: ToolResultStatus,
        presentation: ToolPresentationSnapshot | None = None,
        *,
        message: str | None = None,
    ) -> Self:
        """Finalize in place, replacing streaming output with the final snapshot."""
        if self.state in {
            ToolCardState.SUCCESS,
            ToolCardState.FAILED,
            ToolCardState.CANCELLED,
        }:
            return self
        was_following = self._output_following
        before = self.logical_output_line_count
        if presentation is None:
            final_status = {
                ToolResultStatus.SUCCESS: ToolPresentationStatus.SUCCESS,
                ToolResultStatus.CANCELLED: ToolPresentationStatus.CANCELLED,
            }.get(status, ToolPresentationStatus.FAILED)
            if message is None and status is not ToolResultStatus.SUCCESS:
                message = _SAFE_FAILURE_MESSAGES[status]
            presentation = replace(
                self._snapshot,
                status=final_status,
                payload={
                    "command": self._snapshot.payload["command"],
                    "output": self._output,
                    "exit_code": None,
                },
                message=message,
            )
        else:
            if presentation.kind is not ToolPresentationKind.BASH:
                raise ValueError("BashToolCard requires a bash presentation")
            output = presentation.payload.get("output")
            if not isinstance(output, str):
                raise TypeError("bash presentation output must be a string")
            self._output = output
        result = super().finish(status, presentation)
        after = self.logical_output_line_count
        if not was_following:
            self._unread_lines += max(0, after - before)
        self._output_following = was_following
        self._clamp_output_start()
        return result

    def action_toggle_expanded(self) -> None:
        """Toggle the one command/output detail state for this card."""
        if not self._needs_expansion():
            return
        self._expanded = not self._expanded
        self._clamp_output_start()
        self.refresh(layout=True)
        self._notify_transcript_resize()

    def on_key(self, event: Key) -> None:
        """Browse retained logical output without creating a scrolling worker."""
        if event.key == "end":
            if self.logical_output_line_count <= self.MAX_OUTPUT_LINES:
                return
            latest = self._latest_output_start()
            if (
                self._output_following
                and self._output_start == latest
                and self._unread_lines == 0
            ):
                return
            self._output_start = latest
            self._output_following = True
            self._unread_lines = 0
            self.refresh(layout=True)
            event.stop()
            return
        if self._expanded or self.logical_output_line_count <= self.MAX_OUTPUT_LINES:
            return
        if event.key == "up":
            consumed = self._move_output(-1)
        elif event.key == "down":
            consumed = self._move_output(1)
        elif event.key == "pageup":
            consumed = self._move_output(-self.MAX_OUTPUT_LINES)
        elif event.key == "pagedown":
            consumed = self._move_output(self.MAX_OUTPUT_LINES)
        else:
            consumed = False
        if consumed:
            event.stop()

    def _move_output(self, delta: int) -> bool:
        latest = self._latest_output_start()
        current = latest if self._output_following else self._output_start
        target = max(0, min(latest, current + delta))
        if target == current:
            return False
        self._output_start = target
        self._output_following = target == latest
        if self._output_following:
            self._unread_lines = 0
        self.refresh()
        return True

    def _needs_expansion(self) -> bool:
        return self._command_display_line_count() > self.MAX_COMMAND_DISPLAY_LINES or (
            self.state is not ToolCardState.RUNNING
            and self.logical_output_line_count > self.MAX_OUTPUT_LINES
        )

    def _command_display_line_count(self) -> int:
        return len(self._wrapped_command_lines())

    def _wrapped_command_lines(self) -> list[str]:
        command = self._snapshot.payload["command"]
        assert isinstance(command, str)
        width = max(1, self.content_region.width or self.size.width or 80)
        wrapped = Text(f"$ {command}").wrap(
            Console(width=width), width=width, overflow="fold"
        )
        return [line.plain for line in wrapped] or [""]

    def _logical_output_lines(self) -> list[str]:
        normalized = self._output.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized:
            return []
        return normalized.removesuffix("\n").split("\n")

    def _latest_output_start(self) -> int:
        return max(0, self.logical_output_line_count - self.MAX_OUTPUT_LINES)

    def _clamp_output_start(self) -> None:
        self._output_start = max(
            0, min(self._latest_output_start(), self._output_start)
        )
        if self._output_following:
            self._output_start = self._latest_output_start()

    def _render_output(self, text: Text) -> None:
        lines = self._logical_output_lines()
        if not lines:
            if self.state is not ToolCardState.RUNNING:
                text.append("(no output)", style="#94a3b8")
            return
        if self._expanded or self.state is ToolCardState.RUNNING:
            start = (
                0
                if self._expanded and self.state is not ToolCardState.RUNNING
                else (
                    self._latest_output_start()
                    if self._output_following
                    else self._output_start
                )
            )
            visible = lines[start:]
            hidden = start
        else:
            start = self._latest_output_start()
            visible = lines[start:]
            hidden = start
        text.append("\n".join(visible), style="#e2e8f0")
        if hidden and not self._expanded:
            suffix = (
                f"\n... ({hidden} earlier lines, ctrl+o to expand)"
                if self.state is not ToolCardState.RUNNING
                else ""
            )
            text.append(suffix, style="#94a3b8")
        if self._unread_lines and not self._output_following:
            text.append(f"\n↓ {self._unread_lines} new lines", style="#94a3b8")

    def render(self) -> Text:
        text = Text()
        text.append(self.tool_name, style="bold")
        text.append("\n")
        command_lines = self._wrapped_command_lines()
        if self._expanded:
            text.append("\n".join(command_lines), style="#e2e8f0")
            if len(command_lines) > self.MAX_COMMAND_DISPLAY_LINES:
                text.append(
                    f"\n... ({len(command_lines) - self.MAX_COMMAND_DISPLAY_LINES} display lines, "
                    "ctrl+o to collapse)",
                    style="#94a3b8",
                )
        else:
            visible = command_lines[: self.MAX_COMMAND_DISPLAY_LINES]
            text.append("\n".join(visible), style="#e2e8f0")
            if len(command_lines) > self.MAX_COMMAND_DISPLAY_LINES:
                text.append(
                    f"\n... ({len(command_lines) - self.MAX_COMMAND_DISPLAY_LINES} more "
                    "display lines, ctrl+o to expand)",
                    style="#94a3b8",
                )
        if self._output or self.state in {
            ToolCardState.SUCCESS,
            ToolCardState.FAILED,
            ToolCardState.CANCELLED,
        }:
            text.append("\n\n")
            self._render_output(text)
        if (
            self._expanded
            and self.state is not ToolCardState.RUNNING
            and self.logical_output_line_count > self.MAX_OUTPUT_LINES
        ):
            text.append("\n... (full output, ctrl+o to collapse)", style="#94a3b8")
        if self.snapshot.payload.get("exit_code") is not None:
            exit_code = self.snapshot.payload["exit_code"]
            if isinstance(exit_code, int) and exit_code > 0:
                text.append(f"\nexit {exit_code}", style="#fca5a5")
        if self.snapshot.message is not None:
            message = self.snapshot.message
        elif self.state is ToolCardState.CANCELLED:
            message = _SAFE_FAILURE_MESSAGES[ToolResultStatus.CANCELLED]
        elif self.state is ToolCardState.FAILED:
            message = _SAFE_FAILURE_MESSAGES[ToolResultStatus.EXECUTION_FAILED]
        else:
            message = None
        if message is not None:
            text.append("\n")
            text.append(message, style="#94a3b8")
        return text

    def __repr__(self) -> str:
        return "<BashToolCard>"


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


class GenericToolCard(ToolCard):
    """Compact safe fallback card for generic tool presentations."""

    DEFAULT_CSS = """
    GenericToolCard {
        height: 2;
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
        if snapshot.kind is not ToolPresentationKind.GENERIC:
            raise ValueError("GenericToolCard requires a generic presentation")
        self._failure_message: str | None = None
        super().__init__(snapshot, id=id)

    def finish(
        self,
        status: ToolResultStatus,
        presentation: ToolPresentationSnapshot | None = None,
        *,
        message: str | None = None,
    ) -> Self:
        """Finalize with a status-specific, safe terminal explanation."""
        if self.state in {
            ToolCardState.SUCCESS,
            ToolCardState.FAILED,
            ToolCardState.CANCELLED,
        }:
            return self
        if status is ToolResultStatus.CANCELLED:
            self._failure_message = "operation cancelled"
        elif status is ToolResultStatus.PERMISSION_DENIED:
            self._failure_message = "permission denied"
        elif status is ToolResultStatus.INVALID_ARGUMENTS:
            self._failure_message = "tool arguments are invalid"
        elif status is not ToolResultStatus.SUCCESS:
            self._failure_message = "tool execution failed"
        return super().finish(status, presentation, message=message)

    def _status_text(self) -> str:
        if self.state is ToolCardState.PENDING:
            return "waiting"
        if self.state is ToolCardState.RUNNING:
            return "running…"
        if self.state is ToolCardState.SUCCESS:
            return "completed"
        if self.state is ToolCardState.CANCELLED:
            return "operation cancelled"
        return self._failure_message or "tool execution failed"

    def render(self) -> Text:
        text = Text()
        text.append(self.tool_name, style="bold")
        text.append("\n")
        text.append(self._status_text(), style="#94a3b8")
        return text

    def __repr__(self) -> str:
        return "<GenericToolCard>"


ToolCardFactory = Callable[[ToolPresentationSnapshot], ToolCard]


def copy_tool_card_factories(
    factories: Mapping[str, ToolCardFactory] | None,
) -> dict[str, ToolCardFactory]:
    """Validate and copy injected factories without exposing input details."""
    if factories is None:
        return {}
    if not isinstance(factories, Mapping):
        raise TypeError("tool_card_factories must be a Mapping or None")
    copied: dict[str, ToolCardFactory] = {}
    for name, factory in factories.items():
        if not isinstance(name, str) or not name.strip():
            raise TypeError("tool_card_factories keys must be valid tool names")
        try:
            _validate_tool_name(name)
        except (TypeError, ValueError):
            raise ValueError(
                "tool_card_factories keys must be valid tool names"
            ) from None
        if not callable(factory):
            raise TypeError("tool_card_factories values must be callable")
        copied[name] = factory
    return copied


def create_tool_card(
    snapshot: ToolPresentationSnapshot,
    *,
    tool_card_factories: Mapping[str, ToolCardFactory] | None = None,
    id: str | None = None,
) -> ToolCard | None:
    """Resolve one safe presentation through the shared TUI card boundary.

    Built-in presentation kinds always win. ASK is deliberately left to its
    future interaction panel and therefore never becomes a regular card.
    """
    if not isinstance(snapshot, ToolPresentationSnapshot):
        raise TypeError("snapshot must be a ToolPresentationSnapshot")
    builtin_types = {
        ToolPresentationKind.READ: ("read", ReadToolCard),
        ToolPresentationKind.WRITE: ("write", WriteToolCard),
        ToolPresentationKind.EDIT: ("edit", EditToolCard),
        ToolPresentationKind.BASH: ("bash", BashToolCard),
    }
    builtin = builtin_types.get(snapshot.kind)
    reserved_names = {
        "read": ToolPresentationKind.READ,
        "write": ToolPresentationKind.WRITE,
        "edit": ToolPresentationKind.EDIT,
        "bash": ToolPresentationKind.BASH,
        "ask": ToolPresentationKind.ASK,
    }
    reserved_kind = reserved_names.get(snapshot.tool_name)
    if reserved_kind is not None:
        if snapshot.kind is not reserved_kind:
            return None
        if snapshot.kind is ToolPresentationKind.ASK:
            return None
    if builtin is not None:
        expected_name, card_type = builtin
        if snapshot.tool_name != expected_name:
            return None
        return card_type(snapshot, id=id)
    if snapshot.kind is ToolPresentationKind.ASK:
        return None
    factories = copy_tool_card_factories(tool_card_factories)
    factory = factories.get(snapshot.tool_name)
    if factory is not None:
        try:
            card = factory(snapshot)
            if (
                isinstance(card, ToolCard)
                and card.call_id == snapshot.call_id
                and card.tool_name == snapshot.tool_name
                and card.snapshot == snapshot
            ):
                return card
        except Exception:  # noqa: BLE001, S110
            pass
    return GenericToolCard(snapshot, id=id)


__all__ = [
    "BashToolCard",
    "EditToolCard",
    "GenericToolCard",
    "ReadToolCard",
    "ToolCard",
    "ToolCardFactory",
    "ToolCardState",
    "ToolCardStatus",
    "WriteToolCard",
    "copy_tool_card_factories",
    "create_tool_card",
]

# Stable descriptive alias for callers that prefer the status terminology.
ToolCardStatus = ToolCardState
