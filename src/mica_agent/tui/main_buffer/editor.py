"""Composer draft, cursor, and key interpretation.

The editor owns only input state: it neither renders nor writes stdout, imports no
Textual code, and knows nothing about runs or sessions. Key interpretation normalizes
prompt_toolkit ``KeyPress`` values into stable ``Key`` values for an independent outer
mode machine.

Draft text has two layers:

* display text in ``Buffer``, where long pastes collapse to
  ``[paste #N +M lines]``;
* submission text from ``submit_text()``, where registered paste fragments expand back
  to their complete source.

A long paste is an indivisible editing unit: cursor movement crosses it as a whole and
Backspace/Delete removes it as a whole on contact. An internal registry determines
fragment identity, so manually typing the same summary remains ordinary text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.keys import Keys

from .renderer import cell_len, expand_tabs, wrap_line
from .sanitize import sanitize_text

MAX_DRAFT_LINES = 8
PASTE_LINE_THRESHOLD = 9


@dataclass(frozen=True, slots=True)
class Key:
    """Normalized key value."""

    name: str
    data: str = ""

    @property
    def is_text(self) -> bool:
        return self.name == "text"


_KEY_NAMES: dict[Any, str] = {
    Keys.ControlM: "enter",
    Keys.ControlJ: "newline",
    Keys.Escape: "escape",
    Keys.Up: "up",
    Keys.Down: "down",
    Keys.Left: "left",
    Keys.Right: "right",
    Keys.Home: "home",
    Keys.End: "end",
    Keys.ControlH: "backspace",
    Keys.ControlI: "tab",
    Keys.Delete: "delete",
    Keys.ControlC: "ctrl_c",
    Keys.ControlO: "ctrl_o",
    Keys.ControlD: "ctrl_d",
    Keys.ControlS: "ctrl_s",
    Keys.BracketedPaste: "paste",
}


def normalize_key(keypress: Any) -> Key:
    """Normalize a prompt_toolkit ``KeyPress`` into a stable key value."""
    key = getattr(keypress, "key", None)
    data = getattr(keypress, "data", "") or ""
    if key in _KEY_NAMES:
        name = _KEY_NAMES[key]
        if name == "paste":
            return Key("paste", data.replace("\r\n", "\n").replace("\r", "\n"))
        return Key(name, data)
    if isinstance(key, str):
        if len(key) == 1 and key.isprintable():
            return Key("text", key)
        if data and data.isprintable():
            return Key("text", data)
    return Key("unknown", data)


def logical_line_count(text: str) -> int:
    """Count content lines at newline boundaries without counting a trailing newline."""
    normalized = text.replace("\r\n", "\n")
    if not normalized:
        return 0
    lines = normalized.split("\n")
    if normalized.endswith("\n"):
        lines = lines[:-1]
    return len(lines)


@dataclass(frozen=True, slots=True)
class PasteFragment:
    """One indivisible long-paste fragment."""

    original: str
    line_count: int
    number: int

    @property
    def summary(self) -> str:
        return f"[paste #{self.number} +{self.line_count} lines]"


@dataclass(slots=True)
class _Span:
    start: int
    end: int
    fragment: PasteFragment

    def shifted(self, at: int, delta: int) -> _Span:
        if self.start >= at:
            return _Span(self.start + delta, self.end + delta, self.fragment)
        return self


@dataclass(frozen=True, slots=True)
class ComposerView:
    """Draft view laid out for the terminal width."""

    lines: list[str]
    cursor_row: int
    cursor_column: int


class ComposerEditor:
    """Draft editor backed by a prompt_toolkit ``Buffer``."""

    def __init__(self) -> None:
        self._buffer = Buffer(multiline=True, enable_history_search=False)
        self._spans: list[_Span] = []
        self._next_number = 1

    @property
    def text(self) -> str:
        """Display text with long pastes represented by summaries."""
        return self._buffer.text

    @property
    def is_blank(self) -> bool:
        return not self._buffer.text.strip()

    @property
    def cursor(self) -> int:
        return self._buffer.document.cursor_position

    @property
    def fragment_summaries(self) -> list[str]:
        return [span.fragment.summary for span in self._spans]

    @property
    def lines(self) -> list[str]:
        return self._buffer.text.split("\n")

    @property
    def cursor_row(self) -> int:
        return self._buffer.document.cursor_position_row

    @property
    def cursor_column(self) -> int:
        """Return the cursor's terminal-cell offset within its logical line."""
        return cell_len(self._buffer.document.current_line_before_cursor)

    def wrapped(self, width: int) -> ComposerView:
        """Soft-wrap to the terminal width and locate the cursor in display coordinates.

        Wrapping affects display only; ``submit_text()`` still returns the unwrapped
        source. Tabs are expanded deterministically through :func:`expand_tabs`, so
        wrapping, cursor columns, and display use the same width calculation.
        """
        text = expand_tabs(self._buffer.text)
        lines = wrap_line(text, width)
        before = expand_tabs(self._buffer.text[: self._buffer.document.cursor_position])
        before_lines = wrap_line(before, width)
        return ComposerView(
            lines=lines,
            cursor_row=len(before_lines) - 1,
            cursor_column=cell_len(before_lines[-1]),
        )

    def submit_text(self) -> str:
        """Return the complete source for ``MicaApp``, expanding paste fragments."""
        if not self._spans:
            return self._buffer.text
        parts: list[str] = []
        last = 0
        for span in sorted(self._spans, key=lambda item: item.start):
            parts.append(self._buffer.text[last : span.start])
            parts.append(span.fragment.original)
            last = span.end
        parts.append(self._buffer.text[last:])
        return "".join(parts)

    def insert(self, text: str) -> None:
        """Insert text, folding a long paste into an indivisible summary fragment."""
        cleaned = sanitize_text(text, newlines=True)
        if not cleaned:
            return
        at = self._buffer.document.cursor_position
        fragment = self._fold(cleaned)
        payload = fragment.summary if fragment is not None else cleaned
        self._apply(
            self._buffer.text[:at] + payload + self._buffer.text[at:],
            at + len(payload),
            at,
            len(payload),
            fragment,
        )

    def backspace(self) -> None:
        at = self._buffer.document.cursor_position
        span = self._span_before(at)
        if span is not None:
            self._delete_span(span)
            return
        if at == 0:
            return
        text = self._buffer.text
        self._apply(text[: at - 1] + text[at:], at - 1, at - 1, -1, None)

    def delete(self) -> None:
        at = self._buffer.document.cursor_position
        span = self._span_after(at)
        if span is not None:
            self._delete_span(span)
            return
        text = self._buffer.text
        if at >= len(text):
            return
        self._apply(text[:at] + text[at + 1 :], at, at, -1, None)

    def move(self, name: str) -> None:
        document = self._buffer.document
        text = document.text
        position = document.cursor_position
        if name == "left":
            span = self._span_before(position)
            position = span.start if span is not None else max(0, position - 1)
        elif name == "right":
            span = self._span_after(position)
            position = span.end if span is not None else min(len(text), position + 1)
        elif name == "up":
            position = self._vertical(document, -1)
            position = self._snap(position, direction=-1)
        elif name == "down":
            position = self._vertical(document, 1)
            position = self._snap(position, direction=1)
        elif name == "home":
            position = position - document.cursor_position_col
        elif name == "end":
            position += len(document.current_line_after_cursor)
        self._set(text, position)

    def clear(self) -> None:
        """Clear the draft, fragments, and numbering."""
        self._spans = []
        self._next_number = 1
        self._buffer.reset()

    def _fold(self, text: str) -> PasteFragment | None:
        count = logical_line_count(text)
        if count < PASTE_LINE_THRESHOLD:
            return None
        fragment = PasteFragment(text, count, self._next_number)
        self._next_number += 1
        return fragment

    def _span_before(self, position: int) -> _Span | None:
        for span in self._spans:
            if span.start < position <= span.end:
                return span
        return None

    def _span_after(self, position: int) -> _Span | None:
        for span in self._spans:
            if span.start <= position < span.end:
                return span
        return None

    def _inside_span(self, position: int) -> _Span | None:
        for span in self._spans:
            if span.start < position < span.end:
                return span
        return None

    def _snap(self, position: int, *, direction: int) -> int:
        span = self._inside_span(position)
        if span is None:
            return position
        return span.end if direction > 0 else span.start

    def _delete_span(self, span: _Span) -> None:
        current = self._buffer.text
        new_text = current[: span.start] + current[span.end :]
        removed = span.end - span.start
        self._spans = [item for item in self._spans if item is not span]
        self._apply(new_text, span.start, span.start, -removed, None)

    def _apply(
        self,
        text: str,
        cursor: int,
        at: int,
        delta: int,
        fragment: PasteFragment | None,
    ) -> None:
        if delta:
            self._spans = [span.shifted(at, delta) for span in self._spans]
        if fragment is not None:
            self._spans.append(_Span(at, at + len(fragment.summary), fragment))
        self._set(text, cursor)

    def _set(self, text: str, position: int) -> None:
        clamped = min(max(position, 0), len(text))
        self._buffer.set_document(
            Document(text, cursor_position=clamped), bypass_readonly=True
        )

    def _vertical(self, document: Document, direction: int) -> int:
        row = min(
            max(document.cursor_position_row + direction, 0), document.line_count - 1
        )
        line = document.lines[row]
        column = min(document.cursor_position_col, len(line))
        return document.translate_row_col_to_index(row, column)


def windowed_lines(
    lines: list[str],
    cursor_row: int,
    *,
    limit: int = MAX_DRAFT_LINES,
) -> tuple[list[str], int]:
    """Return the visible draft window around the cursor and its starting row."""
    if limit <= 0 or len(lines) <= limit:
        return list(lines), 0
    cursor = min(max(cursor_row, 0), len(lines) - 1)
    start = min(max(0, cursor - limit + 1), len(lines) - limit)
    return lines[start : start + limit], start


__all__ = [
    "MAX_DRAFT_LINES",
    "PASTE_LINE_THRESHOLD",
    "ComposerEditor",
    "ComposerView",
    "Key",
    "PasteFragment",
    "logical_line_count",
    "normalize_key",
    "windowed_lines",
]
