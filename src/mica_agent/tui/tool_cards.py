"""Textual tool-card views for safe, in-transcript tool lifecycle projection."""

from __future__ import annotations

from dataclasses import replace
from enum import Enum
from typing import Self

from rich.text import Text
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
            self._snapshot = replace(
                self._snapshot,
                status=final_status,
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


__all__ = ["ReadToolCard", "ToolCard", "ToolCardState", "ToolCardStatus"]

# Stable descriptive alias for callers that prefer the status terminology.
ToolCardStatus = ToolCardState
