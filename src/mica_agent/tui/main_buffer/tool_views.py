"""Main-buffer tool views from safe snapshots to full-width status blocks.

One tool call produces one semantic record updated in place as state changes. Views
consume only public ``ToolPresentationSnapshot`` values, which are sanitized and contain
no runtime resources. They never inspect tool arguments, ``ToolResult``, workspace,
provider, or executor internals.

Pending and running use dark blue, success dark green, and failure or cancellation dark
red across the available width. In NO_COLOR mode, ``○ ● ✓ × ■`` and safe fixed reasons
carry state. Card titles omit lifecycle words, long content folds by tool type, and the
global expansion toggle affects only terminal records.

``ToolViewFactory`` is the sole resolution seam. Built-ins have fixed precedence;
external mappings are copied at construction and cannot shadow built-in names. Ordinary
handler exceptions, invalid objects, or snapshot mismatch fall back safely to a generic
view without exposing exception text or tracebacks.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from mica_agent.tool_presentation import ToolPresentationKind

from .renderer import Palette, cell_len
from .sanitize import sanitize_scalar, sanitize_text

# Tool state maps to background style; color is not the sole lifecycle cue.
STATE_STYLES = {
    "pending": "tool_pending",
    "running": "tool_running",
    "success": "tool_success",
    "failed": "tool_failed",
    "cancelled": "tool_cancelled",
}
STATE_SYMBOLS = {
    "pending": "○",
    "running": "●",
    "success": "✓",
    "failed": "×",
    "cancelled": "■",
}
BUILTIN_TOOL_NAMES = ("read", "write", "edit", "bash", "ask")

WRITE_VISIBLE_LINES = 10
EDIT_VISIBLE_CHANGES = 20
EDIT_RETAINED_CHANGES = 10
BASH_COMMAND_ROWS = 3
BASH_OUTPUT_LINES = 8

_FAILED_STATES = frozenset({"failed", "cancelled"})


def _payload_text(snapshot: object, key: str) -> str:
    """Read and resanitize text from a snapshot payload, including extension snapshots."""
    payload = getattr(snapshot, "payload", None)
    if not isinstance(payload, Mapping):
        return ""
    value = payload.get(key)
    return sanitize_text(value, newlines=True) if isinstance(value, str) else ""


def _logical_lines(value: str) -> list[str]:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.removesuffix("\n").split("\n")


@dataclass(frozen=True, slots=True)
class _Part:
    """Text paired with its semantic style name."""

    text: str = field(repr=False)
    style: str = "text"


class ToolView:
    """Base tool view for state background, safe reason, and NO_COLOR symbol."""

    kind = "tool"

    def __init__(
        self,
        snapshot: object,
        *,
        state: str,
        live_output: str = "",
        live_total: int = 0,
    ) -> None:
        self._snapshot = snapshot
        self._state = state if state in STATE_STYLES else "pending"
        self._live_output = sanitize_text(live_output, newlines=True)
        self._live_total = max(0, live_total)

    @property
    def snapshot(self) -> object:
        return self._snapshot

    @property
    def state(self) -> str:
        return self._state

    @property
    def tool_name(self) -> str:
        return sanitize_scalar(
            getattr(self._snapshot, "tool_name", None), fallback="tool"
        )

    @property
    def can_expand(self) -> bool:
        """Whether this record has additional safe content to expand."""
        return False

    def reason(self) -> str:
        """Return a safe fixed reason using only the snapshot message."""
        message = getattr(self._snapshot, "message", None)
        return sanitize_scalar(message, fallback="") if message else ""

    def rows(
        self, width: int, palette: Palette, *, expanded: bool
    ) -> list[list[_Part]]:
        """Return logical rows before background fill."""
        raise NotImplementedError

    def render(self, width: int, palette: Palette, expanded: bool) -> list[str]:
        """Return full-width status rows, using symbols in NO_COLOR mode."""
        width = max(1, width)
        rows = self.rows(width, palette, expanded=expanded)
        background = STATE_STYLES[self._state]
        lines = [_compose(row, width, palette, background) for row in rows]
        if not palette.enabled and lines:
            symbol = STATE_SYMBOLS[self._state]
            lines[0] = _truncate(f"{symbol} {lines[0]}", width)
        return lines

    def split(self, *args: object, **kwargs: object) -> None:
        """Tool records are bounded and never participate in stream splitting."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} state={self._state}>"


def _compose(parts: list[_Part], width: int, palette: Palette, background: str) -> str:
    """Compose a logical row into a physical row that fills the background width."""
    bg = palette.styles.get(background, "")
    output = ""
    used = 0
    for part in parts:
        if used >= width:
            break
        text = _truncate(part.text, width - used)
        used += cell_len(text)
        output += _paint(text, palette, background=bg, style=part.style)
    if palette.enabled and used < width:
        output += _paint(" " * (width - used), palette, background=bg, style="text")
    return output


def _paint(text: str, palette: Palette, *, background: str, style: str) -> str:
    if not palette.enabled or not text:
        return text
    codes = [code for code in (background, palette.styles.get(style, "")) if code]
    if not codes:
        return text
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if cell_len(text) <= limit:
        return text
    from .renderer import truncate_head

    return truncate_head(text, limit)


def _hint(candidates: list[str], width: int) -> str:
    """Choose the longest folding hint that fits, preserving essentials when narrow."""
    for text in candidates:
        if cell_len(text) <= width:
            return text
    return candidates[-1]


def _wrap(text: str, width: int, indent: str = "") -> list[str]:
    from .renderer import wrap_line

    return wrap_line(text, max(1, width), continuation=indent)


class ReadToolView(ToolView):
    """Compact single-row card containing only tool name and safe relative path."""

    kind = "read"

    def rows(
        self, width: int, palette: Palette, *, expanded: bool
    ) -> list[list[_Part]]:
        parts = [_Part(self.tool_name, "tool_name")]
        path = _payload_text(self._snapshot, "path")
        if path:
            parts.append(_Part("  "))
            parts.append(_Part(path, "tool_path"))
        reason = self.reason()
        if reason and self._state in _FAILED_STATES:
            parts.append(_Part(f"  {reason}", "muted"))
        return [parts]


class WriteToolView(ToolView):
    """File-content preview card with plain text and a ten-row fold."""

    kind = "write"

    @property
    def content_lines(self) -> list[str]:
        return _logical_lines(_payload_text(self._snapshot, "content"))

    @property
    def can_expand(self) -> bool:
        return len(self.content_lines) > WRITE_VISIBLE_LINES

    def rows(
        self, width: int, palette: Palette, *, expanded: bool
    ) -> list[list[_Part]]:
        rows = [_header(self)]
        content = self.content_lines
        total = len(content)
        visible = content if expanded else content[:WRITE_VISIBLE_LINES]
        rows.append([])
        for line in visible:
            for physical in _wrap(line, width):
                rows.append([_Part(physical, "tool_output")])
        if total > WRITE_VISIBLE_LINES and not expanded:
            hidden = total - WRITE_VISIBLE_LINES
            rows.append(
                [
                    _Part(
                        _hint(
                            [
                                f"... ({hidden} more lines, {total} total, ctrl+o to expand)",
                                f"... ({hidden} more lines, {total} total)",
                                f"... ({hidden} more lines)",
                            ],
                            width,
                        ),
                        "muted",
                    )
                ]
            )
        elif total > WRITE_VISIBLE_LINES and expanded:
            rows.append(
                [
                    _Part(
                        _hint(
                            [
                                f"... ({total} total, ctrl+o to collapse)",
                                f"... ({total} total)",
                            ],
                            width,
                        ),
                        "muted",
                    )
                ]
            )
        _append_reason(rows, self)
        return rows


class EditToolView(ToolView):
    """Linewise diff card with real row numbers, context, and a twenty-change fold."""

    kind = "edit"

    @property
    def diff_lines(self) -> tuple[Mapping[str, object], ...]:
        value = getattr(self._snapshot, "payload", {})
        lines = value.get("lines") if isinstance(value, Mapping) else None
        return tuple(line for line in lines or () if isinstance(line, Mapping))

    @property
    def matched(self) -> bool | None:
        value = getattr(self._snapshot, "payload", {})
        matched = value.get("matched") if isinstance(value, Mapping) else None
        return matched if isinstance(matched, bool) else None

    @property
    def change_count(self) -> int:
        return sum(
            1 for line in self.diff_lines if line.get("kind") in {"removed", "added"}
        )

    @property
    def can_expand(self) -> bool:
        return self.change_count > EDIT_VISIBLE_CHANGES

    def _visible_indices(self, expanded: bool) -> tuple[int, ...]:
        lines = self.diff_lines
        if expanded or self.change_count <= EDIT_VISIBLE_CHANGES:
            return tuple(range(len(lines)))
        changes = [
            index
            for index, line in enumerate(lines)
            if line.get("kind") in {"removed", "added"}
        ]
        first_end = changes[EDIT_RETAINED_CHANGES - 1]
        while (
            first_end + 1 < len(lines) and lines[first_end + 1].get("kind") == "context"
        ):
            first_end += 1
        last_start = changes[-EDIT_RETAINED_CHANGES]
        while last_start > 0 and lines[last_start - 1].get("kind") == "context":
            last_start -= 1
        return (*range(first_end + 1), *range(last_start, len(lines)))

    def rows(
        self, width: int, palette: Palette, *, expanded: bool
    ) -> list[list[_Part]]:
        rows = [_header(self)]
        rows.append([])
        indices = self._visible_indices(expanded)
        numbers = self._number_width()
        previous: int | None = None
        for index in indices:
            if previous is not None and index != previous + 1:
                hidden = self.change_count - EDIT_VISIBLE_CHANGES
                rows.append(
                    [
                        _Part(
                            _hint(
                                [
                                    f"... ({hidden} change lines hidden, ctrl+o to expand)",
                                    f"... ({hidden} change lines hidden)",
                                ],
                                width,
                            ),
                            "muted",
                        )
                    ]
                )
            line = self.diff_lines[index]
            rows.extend(self._diff_rows(line, width, numbers))
            previous = index
        if expanded and self.change_count > EDIT_VISIBLE_CHANGES:
            rows.append(
                [
                    _Part(
                        _hint(
                            [
                                f"... ({self.change_count} change lines, ctrl+o to collapse)",
                                f"... ({self.change_count} change lines)",
                            ],
                            width,
                        ),
                        "muted",
                    )
                ]
            )
        _append_reason(rows, self)
        return rows

    def _number_width(self) -> int:
        if self.matched is not True:
            return 0
        numbers = [
            line.get("new_line")
            if line.get("kind") in {"context", "added"}
            else line.get("old_line")
            for line in self.diff_lines
        ]
        return max(
            (len(str(number)) for number in numbers if isinstance(number, int)),
            default=1,
        )

    def _diff_rows(
        self, line: Mapping[str, object], width: int, numbers: int
    ) -> list[list[_Part]]:
        kind = str(line.get("kind", "context"))
        text = str(line.get("text", ""))
        style = {"removed": "tool_diff_remove", "added": "tool_diff_add"}.get(
            kind, "tool_output"
        )
        marker = {"removed": "- ", "added": "+ "}.get(kind, "  ")
        if self.matched is not True:
            prefix = marker
        else:
            number = (
                line.get("new_line")
                if kind in {"context", "added"}
                else line.get("old_line")
            )
            label = (
                str(number).rjust(numbers) if isinstance(number, int) else " " * numbers
            )
            prefix = f"{marker}{label}  "
        rows: list[list[_Part]] = []
        for index, physical in enumerate(_wrap(text, width - cell_len(prefix))):
            head = prefix if index == 0 else " " * cell_len(prefix)
            rows.append([_Part(head + physical, style)])
        return rows or [[_Part(prefix, style)]]


class BashToolView(ToolView):
    """Command and live-output card with three command rows and eight output rows."""

    kind = "bash"

    @property
    def command_lines(self) -> list[str]:
        command = _payload_text(self._snapshot, "command")
        return _logical_lines(command) if command else []

    @property
    def output_lines(self) -> list[str]:
        """Use snapshot output when terminal, otherwise the bounded live buffer."""
        if self.state in {"running", "pending"} and self._live_output:
            return _logical_lines(self._live_output)
        output = _payload_text(self._snapshot, "output")
        return _logical_lines(output) if output else []

    @property
    def output_hidden(self) -> int:
        """Number of folded earlier logical rows, based on total received while active."""
        lines = len(self.output_lines)
        if self.state in {"running", "pending"}:
            return max(0, self._live_total - lines)
        return max(0, lines - BASH_OUTPUT_LINES)

    @property
    def exit_code(self) -> int | None:
        payload = getattr(self._snapshot, "payload", {})
        value = payload.get("exit_code") if isinstance(payload, Mapping) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def can_expand(self) -> bool:
        return (
            len(self.command_lines) > BASH_COMMAND_ROWS
            or len(self.output_lines) > BASH_OUTPUT_LINES
        )

    def rows(
        self, width: int, palette: Palette, *, expanded: bool
    ) -> list[list[_Part]]:
        rows: list[list[_Part]] = [[_Part(self.tool_name, "tool_name")], []]
        rows.extend(self._command_rows(width, expanded))
        rows.append([])
        rows.extend(self._output_rows(width, expanded))
        _append_reason(rows, self)
        return rows

    def _command_rows(self, width: int, expanded: bool) -> list[list[_Part]]:
        text = "$ " + _payload_text(self._snapshot, "command")
        wrapped = _wrap(text, width, indent="    ")
        limit = (
            len(wrapped)
            if expanded and self._state not in {"running", "pending"}
            else BASH_COMMAND_ROWS
        )
        visible = wrapped[:limit]
        rows = [[_Part(line, "tool_output")] for line in visible]
        if len(wrapped) > limit and not expanded:
            hidden = len(wrapped) - limit
            rows.append(
                [
                    _Part(
                        _hint(
                            [
                                f"... ({hidden} more display lines, ctrl+o to expand)",
                                f"... ({hidden} more display lines)",
                            ],
                            width,
                        ),
                        "muted",
                    )
                ]
            )
        elif len(wrapped) > BASH_COMMAND_ROWS and expanded:
            hidden = len(wrapped) - BASH_COMMAND_ROWS
            rows.append(
                [
                    _Part(
                        _hint(
                            [
                                f"... ({hidden} display lines, ctrl+o to collapse)",
                                f"... ({hidden} display lines)",
                            ],
                            width,
                        ),
                        "muted",
                    )
                ]
            )
        return rows

    def _output_rows(self, width: int, expanded: bool) -> list[list[_Part]]:
        lines = self.output_lines
        active = self._state in {"running", "pending"}
        if not lines:
            if active:
                return []
            return [[_Part("(no output)", "muted")]]
        if expanded and not active:
            visible, hidden = lines, 0
        else:
            start = max(0, len(lines) - BASH_OUTPUT_LINES)
            visible, hidden = lines[start:], self.output_hidden
        rows: list[list[_Part]] = []
        if hidden:
            rows.append(
                [
                    _Part(
                        _hint(
                            [
                                f"... ({hidden} earlier lines, ctrl+o to expand)"
                                if not active
                                else f"... ({hidden} earlier lines)",
                                f"... ({hidden} earlier lines)",
                            ],
                            width,
                        ),
                        "muted",
                    )
                ]
            )
        for line in visible:
            for physical in _wrap(line, width):
                rows.append([_Part(physical, "tool_output")])
        exit_code = self.exit_code
        if exit_code is not None and exit_code > 0:
            rows.append([_Part(f"exit {exit_code}", "tool_diff_remove")])
        if expanded and not active and len(lines) > BASH_OUTPUT_LINES:
            rows.append(
                [
                    _Part(
                        _hint(
                            [
                                "... (full output, ctrl+o to collapse)",
                                "... (full output)",
                            ],
                            width,
                        ),
                        "muted",
                    )
                ]
            )
        return rows


class AskToolView(ToolView):
    """Completed ask record containing the question, real answer, and answer note."""

    kind = "ask"

    def rows(
        self, width: int, palette: Palette, *, expanded: bool
    ) -> list[list[_Part]]:
        question = _payload_text(self._snapshot, "question")
        answer = _payload_text(self._snapshot, "answer")
        rows: list[list[_Part]] = []
        for physical in _wrap(
            f"{self.tool_name}  {question}".strip(), width, indent="  "
        ):
            rows.append([_Part(physical, "tool_name")])
        if answer:
            for physical in _wrap(f"→ {answer}", width, indent="  "):
                rows.append([_Part(physical, "tool_output")])
        _append_reason(rows, self)
        return rows


class GenericToolView(ToolView):
    """Generic fallback showing only tool name and a fixed safe reason."""

    kind = "generic"

    def rows(
        self, width: int, palette: Palette, *, expanded: bool
    ) -> list[list[_Part]]:
        rows = [[_Part(self.tool_name, "tool_name")]]
        reason = self.reason()
        if reason and self._state in _FAILED_STATES:
            rows.append([_Part(reason, "muted")])
        return rows


def _header(view: ToolView) -> list[_Part]:
    parts = [_Part(view.tool_name, "tool_name")]
    path = _payload_text(view.snapshot, "path")
    if path:
        parts.append(_Part("  "))
        parts.append(_Part(path, "tool_path"))
    return parts


def _append_reason(rows: list[list[_Part]], view: ToolView) -> None:
    reason = view.reason()
    if reason and view.state in _FAILED_STATES:
        rows.append([_Part(reason, "muted")])


ToolViewBuilder = Callable[[object, str], ToolView]

# Built-in names map to presentation kinds; mismatches reject projection without fallback.
_KIND_BY_NAME = {
    "read": ToolPresentationKind.READ,
    "write": ToolPresentationKind.WRITE,
    "edit": ToolPresentationKind.EDIT,
    "bash": ToolPresentationKind.BASH,
    "ask": ToolPresentationKind.ASK,
}
_BUILTIN_VIEWS: dict[ToolPresentationKind, type[ToolView]] = {
    ToolPresentationKind.READ: ReadToolView,
    ToolPresentationKind.WRITE: WriteToolView,
    ToolPresentationKind.EDIT: EditToolView,
    ToolPresentationKind.BASH: BashToolView,
    ToolPresentationKind.ASK: AskToolView,
    ToolPresentationKind.GENERIC: GenericToolView,
}


def _same_snapshot(view: object, snapshot: object) -> bool:
    """Compare complete snapshot values, accepting equal copies and rejecting any mismatch.

    The factory contract compares identity, ``kind``, ``status``, ``message``, payload,
    and all remaining fields by value, so independently constructed equal snapshots are
    accepted.
    """
    if not isinstance(view, ToolView):
        return False
    bound = view.snapshot
    if bound is snapshot:
        return True
    if getattr(bound, "call_id", None) != getattr(snapshot, "call_id", None):
        return False
    if getattr(bound, "tool_name", None) != getattr(snapshot, "tool_name", None):
        return False
    if getattr(bound, "kind", None) != getattr(snapshot, "kind", None):
        return False
    if getattr(bound, "status", None) != getattr(snapshot, "status", None):
        return False
    if getattr(bound, "message", None) != getattr(snapshot, "message", None):
        return False
    return bool(bound == snapshot)


class ToolViewFactory:
    """Sole seam that resolves a safe presentation snapshot into a tool view.

    External builders are keyed by tool name and cannot shadow built-ins. They receive
    only the safe snapshot. Ordinary exceptions, invalid returns, or identity mismatch
    fall back to the generic view without placing exception text, ``repr``, or traceback
    in cards or diagnostics; only a fixed counter increments.
    """

    def __init__(self, overrides: Mapping[str, ToolViewBuilder] | None = None) -> None:
        copied = dict(overrides or {})
        self._overrides = {
            name: builder
            for name, builder in copied.items()
            if name not in BUILTIN_TOOL_NAMES
        }
        self._fallbacks = 0

    @property
    def fallbacks(self) -> int:
        """Number of safe generic fallbacks, excluding exception or identity details."""
        return self._fallbacks

    @property
    def overrides(self) -> tuple[str, ...]:
        """Accepted external tool names from the copied, private mapping."""
        return tuple(sorted(self._overrides))

    def create(
        self,
        snapshot: object,
        *,
        state: str,
        live_output: str = "",
        live_total: int = 0,
    ) -> ToolView | None:
        """Resolve a view under one built-in-first contract.

        ``None`` means identity and kind mismatch, so the caller must reject projection.
        Reserved names accept only their corresponding kind; built-in kinds, including
        ASK, require the matching built-in name; external builders handle only
        ``GENERIC`` snapshots with non-reserved names.
        """
        name = getattr(snapshot, "tool_name", None)
        kind = getattr(snapshot, "kind", None)
        expected = _KIND_BY_NAME.get(name) if isinstance(name, str) else None
        if expected is not None:
            if expected is not kind:
                return None
            view_type = _BUILTIN_VIEWS.get(kind)
            if view_type is None:
                return None
            if view_type is BashToolView:
                return BashToolView(
                    snapshot,
                    state=state,
                    live_output=live_output,
                    live_total=live_total,
                )
            return view_type(snapshot, state=state)
        if kind is not ToolPresentationKind.GENERIC:
            if kind in _BUILTIN_VIEWS:
                # Custom names cannot intercept built-in kinds, including ASK, or use fallback.
                return None
            return GenericToolView(snapshot, state=state)
        builder = self._overrides.get(name) if isinstance(name, str) else None
        if builder is None:
            return GenericToolView(snapshot, state=state)
        view: object = None
        try:
            view = builder(snapshot, state)
            accepted = _same_snapshot(view, snapshot)
        except Exception:  # noqa: BLE001 - boundary for builder, return, and snapshot errors
            accepted = False
        if not accepted:
            self._fallbacks += 1
            return GenericToolView(snapshot, state=state)
        return view  # type: ignore[return-value]

    def __repr__(self) -> str:
        return f"<ToolViewFactory overrides={len(self._overrides)}>"


__all__ = [
    "BASH_OUTPUT_LINES",
    "BUILTIN_TOOL_NAMES",
    "STATE_STYLES",
    "STATE_SYMBOLS",
    "AskToolView",
    "BashToolView",
    "EditToolView",
    "GenericToolView",
    "ReadToolView",
    "ToolView",
    "ToolViewFactory",
    "WriteToolView",
]
