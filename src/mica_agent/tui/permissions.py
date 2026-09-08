"""Textual permission confirmation panel for one pending tool call."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.events import Key, Resize
from textual.widgets import Static

from mica_agent.interactions import PermissionAnswer, PermissionResponse
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)

from .interactions import (
    RunCancelRequester,
    _call_sync,
    _is_async_callable,
)
from .theme import BRAND_LIGHT, ERROR, MUTED, TEXT

PermissionResponder = Callable[[PermissionResponse], bool]
_PermissionAccepted = Callable[["PermissionInteractionView"], object]

_PERMISSION_FAILURE = "Permission response was not accepted; try again."
_CANCEL_FAILURE = "Cancel request was not accepted; try again."
_COMMAND_UNAVAILABLE = "command details unavailable"
_PATH_UNAVAILABLE = "file path unavailable"
_TOOL_DETAILS_UNAVAILABLE = "tool details unavailable"


class _PermissionContent(Static):
    """Render proxy for the scrollable permission panel."""

    def __init__(self, owner: PermissionInteractionView) -> None:
        self._owner = owner
        super().__init__()

    def render(self) -> Text:
        return self._owner._render_text()


class PermissionInteractionView(VerticalScroll):
    """A temporary bottom panel for one public permission interaction.

    Only a safe pending presentation is rendered. The view never receives a
    broker, executor, request, result, workspace, or run task.
    """

    can_focus = True
    BINDINGS: ClassVar[list[Binding]] = []

    DEFAULT_CSS = """
    PermissionInteractionView {
        width: 100%;
        height: auto;
        min-height: 1;
        max-height: 12;
        padding: 1 1 0 1;
        background: $surface;
        color: $foreground;
        border-top: solid $mica-brand-light;
        overflow-y: auto;
    }
    PermissionInteractionView > _PermissionContent {
        width: 100%;
        height: auto;
        color: $foreground;
        text-wrap: wrap;
    }
    """

    def __init__(
        self,
        *,
        run_id: str,
        interaction_id: str,
        presentation: ToolPresentationSnapshot,
        permission_responder: PermissionResponder | None = None,
        run_cancel_requester: RunCancelRequester | None = None,
        on_accepted: _PermissionAccepted | None = None,
        id: str | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise TypeError("run_id must be a non-blank str")
        if not isinstance(interaction_id, str) or not interaction_id.strip():
            raise TypeError("interaction_id must be a non-blank str")
        if not isinstance(presentation, ToolPresentationSnapshot):
            raise TypeError("presentation must be a ToolPresentationSnapshot")
        if presentation.status is not ToolPresentationStatus.PENDING:
            raise ValueError("permission presentation must be pending")
        if permission_responder is not None and (
            not callable(permission_responder)
            or _is_async_callable(permission_responder)
        ):
            raise TypeError(
                "permission_responder must be a synchronous callable or None"
            )
        if run_cancel_requester is not None and (
            not callable(run_cancel_requester)
            or _is_async_callable(run_cancel_requester)
        ):
            raise TypeError(
                "run_cancel_requester must be a synchronous callable or None"
            )
        self._run_id = run_id
        self._interaction_id = interaction_id
        self._presentation = presentation
        self._permission_responder = permission_responder
        self._run_cancel_requester = run_cancel_requester
        self._on_accepted = on_accepted
        self._selected_index = 0
        self._locked = False
        self._error_message: str | None = None
        super().__init__(id=id)

    def compose(self):
        yield _PermissionContent(self)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def interaction_id(self) -> str:
        return self._interaction_id

    @property
    def presentation(self) -> ToolPresentationSnapshot:
        return self._presentation

    @property
    def selected_index(self) -> int:
        return self._selected_index

    @property
    def is_locked(self) -> bool:
        return self._locked

    @property
    def is_active(self) -> bool:
        return not self._locked

    @property
    def error_message(self) -> str | None:
        return self._error_message

    def deactivate(self) -> None:
        """Make a detached view inert without synthesizing a response."""
        self._locked = True
        self._on_accepted = None
        self._permission_responder = None
        self._run_cancel_requester = None

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            if not self._locked:
                self._request_cancel()
            event.stop()
            return
        if event.key in {"pageup", "pagedown", "home", "end"}:
            if event.key == "pageup":
                self.scroll_page_up(animate=False)
            elif event.key == "pagedown":
                self.scroll_page_down(animate=False)
            elif event.key == "home":
                self.scroll_home(animate=False, immediate=True)
            else:
                self.scroll_end(animate=False, immediate=True)
            event.stop()
            return
        if not self._locked:
            if event.key == "up":
                self._selected_index = max(0, self._selected_index - 1)
                self._refresh_panel()
            elif event.key == "down":
                self._selected_index = min(1, self._selected_index + 1)
                self._refresh_panel()
            elif event.key == "1":
                self._selected_index = 0
                self._refresh_panel()
            elif event.key == "2":
                self._selected_index = 1
                self._refresh_panel()
            elif event.key == "enter":
                self._submit_selection()
        # Permission interaction owns all keys while it is focused. In
        # particular, arbitrary text must never reach the composer.
        event.stop()

    def on_mount(self) -> None:
        self._sync_max_height()

    def on_resize(self, _event: Resize) -> None:
        self._sync_max_height()

    def _sync_max_height(self) -> None:
        if self.app is None:
            return
        # Keep one transcript row, the one-line status, the composer
        # minimum and the two-row facts region visible even when short.
        # Wide terminals also keep a one-row gap so the muted hint does
        # not collide with "Mica · working...".
        if self.app.size.height >= 16:
            self.styles.padding = (1, 1, 1, 1)
            self.styles.margin = (0, 0, 1, 0)
            reserved = 8
        else:
            # Extreme terminals cannot afford the extra top pad;
            # border-top already separates the panel from transcript.
            self.styles.padding = (0, 1, 0, 1)
            self.styles.margin = (0, 0, 0, 0)
            reserved = 7
        available = self.app.size.height - reserved
        self.styles.max_height = max(1, min(12, available))

    def _refresh_panel(self) -> None:
        self.refresh(layout=True)
        for child in self.children:
            child.refresh(layout=True)

    def _submit_selection(self) -> None:
        if self._locked:
            return
        answer = (
            PermissionAnswer.ALLOW_ONCE
            if self._selected_index == 0
            else PermissionAnswer.DENY
        )
        response = PermissionResponse(self._interaction_id, answer)
        accepted = _call_sync(self._permission_responder, response)
        if not accepted:
            self._error_message = _PERMISSION_FAILURE
            self._refresh_panel()
            return
        self._locked = True
        self._error_message = None
        self._refresh_panel()
        _call_sync(self._on_accepted, self)

    def _request_cancel(self) -> None:
        if self._locked:
            return
        accepted = _call_sync(self._run_cancel_requester, self._run_id)
        if not accepted:
            self._error_message = _CANCEL_FAILURE
            self._refresh_panel()
            return
        self._locked = True
        self._error_message = None
        self._refresh_panel()
        _call_sync(self._on_accepted, self)

    def _append_operation(self, text: Text) -> None:
        kind = self._presentation.kind
        payload = self._presentation.payload
        if kind is ToolPresentationKind.BASH:
            text.append("Bash command", style=BRAND_LIGHT)
            text.append("\n$ ")
            command = payload.get("command")
            if isinstance(command, str) and command.strip():
                text.append(command)
            else:
                text.append(_COMMAND_UNAVAILABLE, style=MUTED)
            return
        if kind in {
            ToolPresentationKind.READ,
            ToolPresentationKind.WRITE,
            ToolPresentationKind.EDIT,
        }:
            text.append(f"{self._presentation.tool_name} file", style=BRAND_LIGHT)
            text.append("\n")
            path = payload.get("path")
            text.append(
                path if isinstance(path, str) and path.strip() else _PATH_UNAVAILABLE,
                style=TEXT if isinstance(path, str) and path.strip() else MUTED,
            )
            return
        text.append("Tool ", style=BRAND_LIGHT)
        text.append(self._presentation.tool_name)
        text.append("\n")
        text.append(_TOOL_DETAILS_UNAVAILABLE, style=MUTED)

    def render(self) -> Text:
        return self._render_text()

    def _render_text(self) -> Text:
        text = Text()
        self._append_operation(text)
        text.append("\n\nDo you want to proceed?", style=TEXT)
        text.append("\n")
        for index, label in enumerate(("Yes", "No")):
            if index:
                text.append("\n")
            selected = index == self._selected_index
            text.append(
                "› " if selected else "  ", style=BRAND_LIGHT if selected else TEXT
            )
            text.append(f"{index + 1}. {label}")
        if self._error_message is not None:
            text.append("\n\n")
            text.append(self._error_message, style=ERROR)
        elif self._locked:
            text.append("\n\nwaiting for tool completion", style=MUTED)
        else:
            text.append(
                "\n\n↑/↓ to navigate · Enter to confirm · Esc to cancel · PageUp/PageDown to scroll",
                style=MUTED,
            )
        return text

    def __repr__(self) -> str:
        return "<PermissionInteractionView>"


__all__ = ["PermissionInteractionView", "PermissionResponder"]
