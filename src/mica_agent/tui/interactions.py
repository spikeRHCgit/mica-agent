"""Textual ask interaction panels and their front-end-neutral callback seam."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.widgets import Input, Static

from mica_agent.interactions import AskPrompt, UserInputResponse
from mica_agent.tool_contracts import ToolResultStatus
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)

from .theme import BRAND_LIGHT, ERROR, MUTED, SUCCESS, TEXT

UserInputResponder = Callable[[UserInputResponse], bool]
RunCancelRequester = Callable[[str], bool]


def _is_async_callable(callback: object) -> bool:
    call = callback.__call__ if callable(callback) else None  # type: ignore[attr-defined]
    return any(
        inspect.iscoroutinefunction(candidate) or inspect.isasyncgenfunction(candidate)
        for candidate in (callback, call)
        if candidate is not None
    )


def _discard_awaitable(value: object) -> None:
    if isinstance(value, asyncio.Future):
        value.cancel()
        return
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - awaitable cleanup is a UI boundary.
            return


def _call_sync(callback: Callable[..., object] | None, *args: object) -> bool:
    if callback is None:
        return False
    try:
        result = callback(*args)
    except Exception:  # noqa: BLE001 - callback is a UI boundary.
        return False
    if inspect.isawaitable(result):
        _discard_awaitable(result)
        return False
    return result is True


class AskInteractionView(Static):
    """A single ask panel that remains the same widget through its lifecycle.

    The view deliberately receives only the public prompt and two synchronous
    front-end callbacks.  It never owns a broker, tool request/result, store,
    provider, or run task.
    """

    can_focus = True
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+o", "toggle_expanded", show=False),
    ]

    DEFAULT_CSS = """
    AskInteractionView {
        width: 100%;
        height: auto;
        min-height: 1;
        padding: 1 1 0 1;
        margin-bottom: 1;
        background: $surface;
        border-top: solid $mica-brand-light;
        border-bottom: solid $mica-brand-light;
        color: $foreground;
    }
    AskInteractionView.ask-success {
        background: #052e16;
        border-top: solid $success;
        border-bottom: solid $success;
    }
    AskInteractionView.ask-failed,
    AskInteractionView.ask-cancelled {
        background: #450a0a;
        border-top: solid $error;
        border-bottom: solid $error;
    }
    AskInteractionView > .ask-input {
        width: 100%;
        height: 1;
        margin: 0;
        padding: 0 1;
        background: $surface;
        border: none;
    }
    AskInteractionView.ask-success > Input,
    AskInteractionView.ask-failed > Input,
    AskInteractionView.ask-cancelled > Input {
        display: none;
    }
    """

    def __init__(
        self,
        *,
        run_id: str,
        interaction_id: str,
        prompt: AskPrompt,
        user_input_responder: UserInputResponder | None = None,
        run_cancel_requester: RunCancelRequester | None = None,
        id: str | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise TypeError("run_id must be a non-blank str")
        if not isinstance(interaction_id, str) or not interaction_id.strip():
            raise TypeError("interaction_id must be a non-blank str")
        if not isinstance(prompt, AskPrompt):
            raise TypeError("prompt must be an AskPrompt")
        if user_input_responder is not None and (
            not callable(user_input_responder)
            or _is_async_callable(user_input_responder)
        ):
            raise TypeError(
                "user_input_responder must be a synchronous callable or None"
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
        self._prompt = prompt
        self._user_input_responder = user_input_responder
        self._run_cancel_requester = run_cancel_requester
        self._selected_index = 0
        self._notes: str | None = None
        self._answer: str | None = None
        self._mode = "free" if not prompt.options else "options"
        self._expanded = False
        self._locked = False
        self._terminal: ToolResultStatus | None = None
        self._input: Input | None = None
        super().__init__(id=id)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def interaction_id(self) -> str:
        return self._interaction_id

    @property
    def selected_option_index(self) -> int | None:
        return self._selected_index if self._prompt.options else None

    @property
    def notes(self) -> str | None:
        return self._notes

    @property
    def answer(self) -> str | None:
        return self._answer

    @property
    def is_active(self) -> bool:
        return self._terminal is None

    @property
    def terminal_status(self) -> ToolResultStatus | None:
        return self._terminal

    @property
    def expanded(self) -> bool:
        return self._expanded

    def compose(self) -> ComposeResult:
        self._input = Input(classes="ask-input")
        yield self._input

    def on_mount(self) -> None:
        self._sync_input()
        self.focus_input()

    def focus_input(self) -> None:
        if self._input is not None and self._input.display and not self._input.disabled:
            self._input.focus()
        else:
            self.focus()

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self._request_cancel()
            event.stop()
            return
        if self._locked or self._terminal is not None:
            return
        if self._mode != "options":
            return
        if event.key == "up":
            self._selected_index = max(0, self._selected_index - 1)
            self.refresh()
            event.stop()
        elif event.key == "down":
            self._selected_index = min(
                self._option_count() - 1, self._selected_index + 1
            )
            self.refresh()
            event.stop()
        elif event.key in "12345":
            index = ord(event.key) - ord("1")
            if index < self._option_count():
                self._selected_index = index
                self.refresh()
            event.stop()
        elif event.key == "n":
            self._mode = "notes"
            if self._input is not None:
                self._input.value = self._notes or ""
            self._sync_input()
            self._input.focus() if self._input is not None else self.focus()
            event.stop()
        elif event.key == "enter":
            self._submit_selection()
            event.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._locked or self._terminal is not None:
            return
        if self._mode == "notes":
            self._notes = event.value
            self._mode = "options"
            self._sync_input()
            self.focus()
        elif self._mode in {"custom", "free"}:
            self._submit_answer(event.value)
        event.stop()

    def action_toggle_expanded(self) -> None:
        if self._terminal is None:
            return
        self._expanded = not self._expanded
        self.refresh(layout=True)
        self._notify_transcript_resize()

    def finish(
        self,
        status: ToolResultStatus,
        presentation: ToolPresentationSnapshot | None = None,
    ) -> AskInteractionView:
        """Close this panel once, using only safe final presentation data."""
        if self._terminal is not None:
            return self
        if not isinstance(status, ToolResultStatus):
            raise TypeError("status must be a ToolResultStatus")
        if presentation is not None:
            if presentation.kind is not ToolPresentationKind.ASK:
                raise ValueError("ask presentation kind is required")
            expected = {
                ToolResultStatus.SUCCESS: ToolPresentationStatus.SUCCESS,
                ToolResultStatus.CANCELLED: ToolPresentationStatus.CANCELLED,
            }.get(status, ToolPresentationStatus.FAILED)
            if presentation.status is not expected:
                raise ValueError("ask presentation status must match tool result")
            question = presentation.payload.get("question")
            if not isinstance(question, str) or not question.strip():
                return self
            answer = presentation.payload.get("answer")
            if status is ToolResultStatus.SUCCESS and isinstance(answer, str):
                self._answer = answer
        if status is ToolResultStatus.SUCCESS and self._answer is None:
            self._answer = ""
        self._terminal = status
        self._locked = True
        self._mode = "terminal"
        self._sync_input()
        self._apply_state_class()
        self.refresh(layout=True)
        self._notify_transcript_resize()
        return self

    def _option_count(self) -> int:
        return len(self._prompt.options) + (1 if self._prompt.allow_custom else 0)

    def _submit_selection(self) -> None:
        if self._selected_index == len(self._prompt.options):
            self._mode = "custom"
            if self._input is not None:
                self._input.value = ""
            self._sync_input()
            self._input.focus() if self._input is not None else self.focus()
            self.refresh(layout=True)
            return
        option = self._prompt.options[self._selected_index]
        self._submit_answer(
            option.label,
            selected_option_index=self._selected_index,
        )

    def _submit_answer(
        self,
        answer: str,
        *,
        selected_option_index: int | None = None,
    ) -> None:
        if self._locked or self._terminal is not None:
            return
        response = UserInputResponse(
            interaction_id=self._interaction_id,
            answer=answer,
            selected_option_index=selected_option_index,
            notes=self._notes if self._prompt.options else None,
        )
        accepted = _call_sync(self._user_input_responder, response)
        if accepted:
            self._answer = answer
            self._locked = True
            self._mode = "waiting"
            self._sync_input()
            self.refresh(layout=True)

    def _request_cancel(self) -> None:
        if self._locked or self._terminal is not None:
            return
        accepted = _call_sync(self._run_cancel_requester, self._run_id)
        if accepted:
            self._locked = True
            self._mode = "waiting"
            self._sync_input()
            self.refresh(layout=True)

    def _sync_input(self) -> None:
        if self._input is None:
            return
        visible = self._mode in {"free", "custom", "notes"} and not self._locked
        self._input.display = visible
        self._input.disabled = not visible
        if visible:
            self._input.placeholder = (
                "Notes" if self._mode == "notes" else "Type something."
            )

    def _apply_state_class(self) -> None:
        self.set_class(False, "ask-success", "ask-failed", "ask-cancelled")
        if self._terminal is not None:
            suffix = (
                "success"
                if self._terminal is ToolResultStatus.SUCCESS
                else "cancelled"
                if self._terminal is ToolResultStatus.CANCELLED
                else "failed"
            )
            self.add_class(f"ask-{suffix}")

    def _notify_transcript_resize(self) -> None:
        from .transcript import ConversationTranscript

        parent = self.parent
        while parent is not None:
            if isinstance(parent, ConversationTranscript):
                parent.content_resized()
                return
            parent = parent.parent

    def _append_prompt(self, text: Text) -> None:
        if self._prompt.title is not None:
            text.append(self._prompt.title, style=MUTED)
            text.append("\n\n")
        text.append(self._prompt.question, style=TEXT)

    def render(self) -> Text:
        text = Text()
        if self._terminal is not None:
            text.append(
                "ask  ",
                style=SUCCESS if self._terminal is ToolResultStatus.SUCCESS else ERROR,
            )
            text.append(self._prompt.question)
            text.append("\n→ ")
            if self._terminal is ToolResultStatus.SUCCESS:
                text.append(self._answer if self._answer else "(empty answer)")
                if self._notes:
                    text.append("\n")
                    text.append(self._notes, style=MUTED)
            elif self._terminal is ToolResultStatus.CANCELLED:
                text.append("operation cancelled")
            else:
                text.append("tool execution failed")
            if self._expanded:
                self._append_details(text)
            return text
        self._append_prompt(text)
        if self._mode == "options":
            text.append("\n\n")
            for index, option in enumerate(self._prompt.options):
                if index:
                    text.append("\n\n")
                marker = "› " if index == self._selected_index else "  "
                text.append(
                    marker, style=BRAND_LIGHT if index == self._selected_index else TEXT
                )
                text.append(f"{index + 1}. {option.label}")
                if option.recommended:
                    text.append(" (recommended)", style=MUTED)
                if option.description is not None:
                    text.append("\n     ")
                    text.append(option.description, style=MUTED)
            if self._prompt.allow_custom:
                text.append("\n\n")
                marker = (
                    "› " if self._selected_index == len(self._prompt.options) else "  "
                )
                text.append(marker, style=BRAND_LIGHT if marker.strip() else TEXT)
                text.append(f"{len(self._prompt.options) + 1}. Type something.")
            text.append(
                "\n\nEnter select · ↑/↓ navigate · n add notes · Esc cancel",
                style=MUTED,
            )
        elif self._mode == "notes":
            text.append("\n\nNotes", style=MUTED)
        elif self._mode in {"custom", "free"}:
            text.append("\n\nAnswer", style=MUTED)
        else:
            text.append("\n\nwaiting for tool completion", style=MUTED)
        return text

    def _append_details(self, text: Text) -> None:
        text.append("\n\nPrompt details", style=MUTED)
        if self._prompt.title is not None:
            text.append("\n")
            text.append(self._prompt.title, style=MUTED)
        text.append("\n")
        text.append(self._prompt.question)
        if self._prompt.options:
            for option in self._prompt.options:
                text.append("\n· ")
                text.append(option.label)
                if option.recommended:
                    text.append(" (recommended)", style=MUTED)
                if option.description is not None:
                    text.append("\n  ")
                    text.append(option.description, style=MUTED)

    def __repr__(self) -> str:
        return "<AskInteractionView>"


class HistoricalAskRecordView(Static):
    """A read-only v3 ask record restored from a safe presentation snapshot."""

    can_focus = True
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+o", "toggle_expanded", show=False),
    ]
    DEFAULT_CSS = """
    HistoricalAskRecordView {
        width: 100%;
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
        text-wrap: wrap;
    }
    HistoricalAskRecordView.ask-success { color: $success; }
    HistoricalAskRecordView.ask-failed,
    HistoricalAskRecordView.ask-cancelled { color: $error; }
    """

    def __init__(
        self, snapshot: ToolPresentationSnapshot, *, id: str | None = None
    ) -> None:
        if snapshot.kind is not ToolPresentationKind.ASK:
            raise ValueError("HistoricalAskRecordView requires an ask presentation")
        if snapshot.status not in {
            ToolPresentationStatus.SUCCESS,
            ToolPresentationStatus.FAILED,
        }:
            raise ValueError("historical ask must be successful or failed")
        self._snapshot = snapshot
        self._expanded = False
        super().__init__(id=id)
        self._apply_state_class()

    @property
    def snapshot(self) -> ToolPresentationSnapshot:
        return self._snapshot

    @property
    def expanded(self) -> bool:
        return self._expanded

    def action_toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        self.refresh(layout=True)
        self._notify_transcript_resize()

    def render(self) -> Text:
        question = self._snapshot.payload.get("question", "")
        answer = self._snapshot.payload.get("answer", "")
        text = Text(
            "ask  ",
            style=SUCCESS
            if self._snapshot.status is ToolPresentationStatus.SUCCESS
            else ERROR,
        )
        text.append(question if isinstance(question, str) else "question unavailable")
        if self._snapshot.status is ToolPresentationStatus.SUCCESS:
            text.append("\n→ ")
            text.append(
                answer if isinstance(answer, str) and answer else "(empty answer)"
            )
        else:
            text.append("\n→ ")
            if self._snapshot.status is ToolPresentationStatus.CANCELLED:
                text.append("operation cancelled")
            else:
                text.append("tool execution failed")
        if self._expanded:
            text.append("\n\nPrompt details", style=MUTED)
            text.append("\n")
            text.append(
                question if isinstance(question, str) else "question unavailable"
            )
        return text

    def _apply_state_class(self) -> None:
        self.set_class(False, "ask-success", "ask-failed", "ask-cancelled")
        suffix = (
            "success"
            if self._snapshot.status is ToolPresentationStatus.SUCCESS
            else self._snapshot.status.value
        )
        self.add_class(f"ask-{suffix}")

    def _notify_transcript_resize(self) -> None:
        from .transcript import ConversationTranscript

        parent = self.parent
        while parent is not None:
            if isinstance(parent, ConversationTranscript):
                parent.content_resized()
                return
            parent = parent.parent

    def __repr__(self) -> str:
        return "<HistoricalAskRecordView>"


__all__ = [
    "AskInteractionView",
    "HistoricalAskRecordView",
    "RunCancelRequester",
    "UserInputResponder",
]
