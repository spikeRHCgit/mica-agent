"""Exclusive input modes for ask and permission interactions.

Each interaction places the input controller in one explicit mode (``ASK`` or
``PERMISSION``) and owns arrows, digits, ``Enter``, ``Esc``, and its text input. It
produces only public response objects or cancellation intent and never calls the
broker, engine, provider, or store directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mica_agent.interactions import (
    AskPrompt,
    PermissionAnswer,
    PermissionResponse,
    UserInputResponse,
)

from .editor import Key
from .renderer import Palette, cell_len, truncate_tail, wrap_line
from .sanitize import sanitize_scalar, sanitize_text

SEPARATOR = "─" * 120
FOOTER_ASK_OPTIONS = "Enter 选择 · ↑/↓ 移动 · n 添加备注 · Esc 取消"
FOOTER_ASK_TEXT = "Enter 提交 · Esc 取消"
FOOTER_PERMISSION = "Enter 确认 · ↑/↓ 移动 · Esc 取消"
CUSTOM_LABEL = "Type something."
QUESTION_PERMISSION = "Do you want to proceed?"
OPTION_YES = "Yes"
OPTION_NO = "No"

# Fixed safe panel hints describe only the next action; they never echo responses,
# identifiers, broker state, exception text, paths, or tool arguments.
HINT_EMPTY_ANSWER = "请输入内容"
HINT_RESPONSE_REJECTED = "提交未被接受，请重试或按 Esc 取消"
HIDDEN_ABOVE = "… 上方还有 {count} 行"
HIDDEN_BELOW = "… 下方还有 {count} 行"

# Higher ranks survive degradation longer. Blank rows go first, then descriptions and
# verbose detail, then titles, footer, and separators. Questions, option labels, custom
# rows, hints, and text input are windowed rather than rank-dropped so selection stays visible.
RANK_BLANK = 0
RANK_DESCRIPTION = 1
RANK_TITLE = 2
RANK_FOOTER = 3
RANK_SEPARATOR = 4
RANK_CORE = 5


@dataclass(frozen=True, slots=True)
class PanelRow:
    """One panel row and its degradation priority."""

    text: str
    rank: int


@dataclass(frozen=True, slots=True)
class PanelView:
    """One panel layout: physical rows and the selected-row index."""

    lines: list[str]
    selected_row: int


def fit_panel_rows(
    rows: list[PanelRow], limit: int | None, anchor: int, also: list[int] | None = None
) -> tuple[list[PanelRow], int]:
    """Fit a panel within ``limit`` rows while keeping ``anchor`` visible.

    Optional rows are removed from lowest to highest priority, choosing the row farthest
    from selection within a rank. If that is insufficient, a window around selection
    must include the selected row and uses fixed hints for hidden rows. ``also`` marks
    rows, such as safe panel hints, that should share the window when possible. A
    ``None`` limit preserves all rows. For non-empty ``rows`` and ``limit >= 1``, the
    returned selection index is valid and identifies the original ``anchor`` row.
    """
    if not rows:
        return [], 0
    if limit is None or limit >= len(rows):
        return list(rows), min(max(anchor, 0), len(rows) - 1)
    kept = list(rows)
    focus = min(max(anchor, 0), len(kept) - 1)
    preferred = [index for index in (also or []) if 0 <= index < len(kept)]
    limit = max(1, limit)
    while len(kept) > limit:
        rank = min((row.rank for row in kept if row.rank < RANK_CORE), default=None)
        if rank is None:
            break
        victim = _farthest_row(kept, rank, focus)
        del kept[victim]
        if victim < focus:
            focus -= 1
        preferred = [
            index - 1 if index > victim else index
            for index in preferred
            if index != victim
        ]
    if len(kept) <= limit:
        return kept, focus
    return _window_rows(kept, limit, focus, preferred)


def _farthest_row(rows: list[PanelRow], rank: int, focus: int) -> int:
    """Return the row in this rank farthest from selection, preferring the later tie."""
    candidates = [index for index, row in enumerate(rows) if row.rank == rank]
    return max(candidates, key=lambda index: (abs(index - focus), index))


def _window_rows(
    rows: list[PanelRow], limit: int, focus: int, preferred: list[int]
) -> tuple[list[PanelRow], int]:
    """Window around selection, treating the selected row as a hard constraint.

    Every candidate satisfies ``start <= focus < end``, so hints, questions, and hidden
    counts cannot displace selection. Candidates are scored by covered hint rows,
    whether hidden content is explained, retained content rows, then earlier position.
    Rejection and empty-answer hints rank first because they explain why the panel
    remains. Next, the window should either hide nothing, show hidden-row counts, or
    include that fixed hint. Content count then preserves as much question and option
    text as possible. The returned index always identifies ``rows[focus]``.
    """
    if not rows:
        return [], 0
    inside = min(max(focus, 0), len(rows) - 1)
    budget = max(1, limit)
    hints = {index for index in preferred if 0 <= index < len(rows)}
    best: tuple[tuple[int, int, int, int], int, int] | None = None
    for content in range(1, budget + 1):
        for start in range(max(0, inside - content + 1), inside + 1):
            end = min(len(rows), start + content)
            if not start <= inside < end:
                continue
            covered = set(range(start, end))
            hint_rows = len(hints & covered)
            slots = int(start > 0) + int(end < len(rows))
            explained = int(slots == 0 or slots <= budget - (end - start) or hint_rows)
            score = (hint_rows, explained, end - start, -start)
            if best is None or score > best[0]:
                best = (score, start, end, slots)
    assert best is not None
    _, start, end, slots = best
    prefix: list[PanelRow] = []
    suffix: list[PanelRow] = []
    # Show hidden-row counts only when every required count fits; a partial account misleads.
    if slots <= budget - (end - start):
        if start > 0:
            prefix = [PanelRow(HIDDEN_ABOVE.format(count=start), RANK_CORE)]
        if end < len(rows):
            suffix = [PanelRow(HIDDEN_BELOW.format(count=len(rows) - end), RANK_CORE)]
    return [*prefix, *rows[start:end], *suffix], len(prefix) + (inside - start)


@dataclass(frozen=True, slots=True)
class InteractionOutcome:
    """Outcome of handling one key."""

    action: str
    """``none``（仅重绘）、``response`` 或 ``cancel``。"""

    response: PermissionResponse | UserInputResponse | None = None

    @staticmethod
    def none() -> InteractionOutcome:
        return InteractionOutcome("none")

    @staticmethod
    def cancel() -> InteractionOutcome:
        return InteractionOutcome("cancel")


@dataclass
class AskInteraction:
    """Active state of a structured ask panel."""

    interaction_id: str
    prompt: AskPrompt
    selected: int = 0
    editing: str = "options"
    note: str = ""
    custom: str = ""
    _text: str = field(default="", repr=False)
    hint: str = ""

    def __post_init__(self) -> None:
        self.selected = 0
        if not self.prompt.options:
            self.editing = "free"

    def _custom_enabled(self) -> bool:
        return bool(self.prompt.allow_custom and self.prompt.options)

    @property
    def row_count(self) -> int:
        """Selectable rows: option rows plus the optional ``Type something.`` row."""
        return len(self.prompt.options) + (1 if self._custom_enabled() else 0)

    @property
    def is_custom_row(self) -> bool:
        return self._custom_enabled() and self.selected >= len(self.prompt.options)

    @property
    def is_text_entry(self) -> bool:
        return self.editing in {"custom", "free"}

    def fail(self) -> None:
        """Record business rejection using only the fixed safe panel hint."""
        self.hint = HINT_RESPONSE_REJECTED

    def buffer_text(self) -> str:
        if self.editing == "notes":
            return self.note
        if self.editing == "custom":
            return self.custom
        if self.editing == "free":
            return self._text
        return ""

    def handle(self, key: Key) -> InteractionOutcome:
        # Clear the prior hint only for a consumed key that changes state. PageUp,
        # PageDown, unknown keys, and out-of-range digits must not hide rejection.
        if self.is_text_entry or self.editing == "notes":
            return self._handle_text(key)
        return self._handle_options(key)

    def _handle_options(self, key: Key) -> InteractionOutcome:
        total = self.row_count
        if key.name in {"up", "down"}:
            if total:
                step = -1 if key.name == "up" else 1
                self.selected = (self.selected + step) % total
                self.hint = ""
            return InteractionOutcome.none()
        if key.name == "text" and key.data.isdigit():
            # Digits match public panel numbering and select only numbered option rows.
            index = int(key.data) - 1
            if 0 <= index < len(self.prompt.options):
                self.selected = index
                self.hint = ""
            return InteractionOutcome.none()
        if key.name == "n" or (key.name == "text" and key.data.lower() == "n"):
            self.hint = ""
            self.editing = "notes"
            return InteractionOutcome.none()
        if key.name == "escape":
            return InteractionOutcome.cancel()
        if key.name == "enter":
            self.hint = ""
            if self.is_custom_row:
                self.editing = "custom"
                return InteractionOutcome.none()
            if self.selected < len(self.prompt.options):
                option = self.prompt.options[self.selected]
                notes = self.note or None
                return InteractionOutcome(
                    "response",
                    UserInputResponse(
                        interaction_id=self.interaction_id,
                        answer=option.label,
                        selected_option_index=self.selected,
                        notes=notes,
                    ),
                )
            if self._custom_enabled():
                self.editing = "custom"
            return InteractionOutcome.none()
        return InteractionOutcome.none()

    def _handle_text(self, key: Key) -> InteractionOutcome:
        if key.name == "escape":
            return InteractionOutcome.cancel()
        if (
            key.name in {"up", "down"}
            and self.editing == "custom"
            and self.prompt.options
        ):
            # In custom input, Up/Down returns to fixed options: Up selects the last and
            # Down the first. Preserve the draft for the next entry into custom input.
            self.hint = ""
            self.editing = "options"
            self.selected = len(self.prompt.options) - 1 if key.name == "up" else 0
            return InteractionOutcome.none()
        if self.editing == "notes" and key.name == "enter":
            self.hint = ""
            self.editing = "options"
            return InteractionOutcome.none()
        if key.name == "enter":
            # Enter retries: clear the old hint, then replace it if the answer is blank.
            self.hint = ""
            answer = self.buffer_text().strip()
            if not answer:
                self.hint = HINT_EMPTY_ANSWER
                return InteractionOutcome.none()
            if self.editing == "custom":
                # A custom answer is not numbered; submit a ``None`` index so business
                # validation does not reject it as an out-of-range option.
                return InteractionOutcome(
                    "response",
                    UserInputResponse(
                        interaction_id=self.interaction_id,
                        answer=answer,
                        selected_option_index=None,
                        notes=self.note or None,
                    ),
                )
            return InteractionOutcome(
                "response",
                UserInputResponse(interaction_id=self.interaction_id, answer=answer),
            )
        if key.name == "backspace":
            self.hint = ""
            self._edit_text(-1)
            return InteractionOutcome.none()
        if key.name == "delete":
            self.hint = ""
            self._edit_text(0)
            return InteractionOutcome.none()
        if key.name in {"text", "paste"} and self._edit_text(None, key.data):
            self.hint = ""
        return InteractionOutcome.none()

    def _edit_text(self, remove: int | None, insert: str = "") -> bool:
        """Edit the text buffer and report whether content changed."""
        current = self.buffer_text()
        if remove == -1:
            updated = current[:-1]
        elif remove == 0:
            updated = current[1:]
        else:
            addition = sanitize_text(insert, newlines=False, tabs=False)
            if not addition:
                return False
            updated = current + addition
        if self.editing == "notes":
            self.note = updated
        elif self.editing == "custom":
            self.custom = updated
        else:
            self._text = updated
        return updated != current

    def panel(
        self, width: int, palette: Palette, *, limit: int | None = None
    ) -> PanelView:
        """Lay out the panel, degrading by fixed priority when ``limit`` is exceeded."""
        inner = max(8, width)
        rows: list[PanelRow] = [
            PanelRow(
                palette.paint(_fit(SEPARATOR, inner), "interaction"), RANK_SEPARATOR
            )
        ]
        title = sanitize_scalar(self.prompt.title, fallback="")
        if title:
            rows.append(
                PanelRow(
                    palette.paint(f" {_fit(title, inner - 2)} ", "facts_muted"),
                    RANK_TITLE,
                )
            )
            rows.append(PanelRow("", RANK_BLANK))
        question = sanitize_text(self.prompt.question, newlines=True)
        for line in self._question_rows(question, inner, limit):
            rows.append(PanelRow(palette.paint(line, "text"), RANK_CORE))
        rows.append(PanelRow("", RANK_BLANK))
        anchor = 0
        hint_rows: list[int] = []
        if self.prompt.options:
            for index, option in enumerate(self.prompt.options, start=1):
                marker = "›" if index - 1 == self.selected else " "
                label = (
                    f"{marker} {index}. {sanitize_scalar(option.label, fallback='')}"
                )
                if option.recommended:
                    label += " (推荐)"
                rows.append(PanelRow(palette.paint(label, "interaction"), RANK_CORE))
                if index - 1 == self.selected:
                    anchor = len(rows) - 1
                if option.description:
                    description = sanitize_scalar(option.description, fallback="")
                    for line in wrap_line(description, inner - 6):
                        rows.append(
                            PanelRow(
                                "  " + palette.paint(line, "muted"), RANK_DESCRIPTION
                            )
                        )
            if self._custom_enabled():
                marker = "›" if self.is_custom_row else " "
                rows.append(
                    PanelRow(
                        palette.paint(f"{marker} {CUSTOM_LABEL}", "interaction"),
                        RANK_CORE,
                    )
                )
                if self.is_custom_row:
                    anchor = len(rows) - 1
        if self.editing == "notes":
            rows.append(PanelRow("", RANK_BLANK))
            rows.append(
                PanelRow(
                    palette.paint(
                        f" 备注: {sanitize_scalar(self.note, fallback='')}",
                        "interaction",
                    ),
                    RANK_CORE,
                )
            )
        elif self.is_text_entry:
            rows.append(PanelRow("", RANK_BLANK))
            rows.append(
                PanelRow(
                    palette.paint(
                        f" › {sanitize_scalar(self.buffer_text(), fallback='')}_",
                        "text",
                    ),
                    RANK_CORE,
                )
            )
            if not self.prompt.options:
                anchor = len(rows) - 1
        if self.hint:
            rows.append(PanelRow("", RANK_BLANK))
            for line in wrap_line(self.hint, inner):
                rows.append(PanelRow(palette.paint(line, "text"), RANK_CORE))
                hint_rows.append(len(rows) - 1)
        rows.append(PanelRow("", RANK_BLANK))
        rows.append(
            PanelRow(
                palette.paint(
                    FOOTER_ASK_TEXT if self.is_text_entry else FOOTER_ASK_OPTIONS,
                    "muted",
                ),
                RANK_FOOTER,
            )
        )
        rows.append(
            PanelRow(
                palette.paint(_fit(SEPARATOR, inner), "interaction"), RANK_SEPARATOR
            )
        )
        visible, selected_row = fit_panel_rows(rows, limit, anchor, hint_rows)
        return PanelView([row.text for row in visible], selected_row)

    def panel_lines(
        self, width: int, palette: Palette, *, limit: int | None = None
    ) -> list[str]:
        """Return physical panel rows without the selection anchor."""
        return self.panel(width, palette, limit=limit).lines

    def _question_rows(self, question: str, inner: int, limit: int | None) -> list[str]:
        """Keep at least one question row, yielding scarce space to option labels."""
        lines = wrap_line(question, inner)
        if limit is None:
            return lines
        budget = max(1, limit - self._mandatory_row_count())
        if len(lines) <= budget:
            return lines
        if budget <= 1:
            return lines[:1]
        return [*lines[: budget - 1], "…"]

    def _mandatory_row_count(self) -> int:
        """Count non-removable rows beyond the question: options and the custom row."""
        return self.row_count


@dataclass
class PermissionInteraction:
    """Active permission panel offering only allow-once and deny."""

    interaction_id: str
    tool_name: str
    detail: str = ""
    selected: int = 0
    hint: str = ""

    def fail(self) -> None:
        """Record business rejection using only the fixed safe panel hint."""
        self.hint = HINT_RESPONSE_REJECTED

    def handle(self, key: Key) -> InteractionOutcome:
        # As in ask mode, only a consumed key that changes state clears rejection.
        if key.name in {"up", "down"}:
            self.selected = 1 - self.selected
            self.hint = ""
            return InteractionOutcome.none()
        if key.name == "text" and key.data in {"1", "2"}:
            self.selected = int(key.data) - 1
            self.hint = ""
            return InteractionOutcome.none()
        if key.name == "escape":
            return InteractionOutcome.cancel()
        if key.name == "enter":
            self.hint = ""
            answer = (
                PermissionAnswer.ALLOW_ONCE
                if self.selected == 0
                else PermissionAnswer.DENY
            )
            return InteractionOutcome(
                "response",
                PermissionResponse(
                    interaction_id=self.interaction_id,
                    answer=answer,
                ),
            )
        return InteractionOutcome.none()

    def panel(
        self, width: int, palette: Palette, *, limit: int | None = None
    ) -> PanelView:
        """Lay out the panel, degrading by fixed priority when ``limit`` is exceeded."""
        inner = max(8, width)
        rows: list[PanelRow] = [
            PanelRow(
                palette.paint(_fit(SEPARATOR, inner), "interaction"), RANK_SEPARATOR
            )
        ]
        title = f" {_fit(sanitize_scalar(self.tool_name, fallback='tool'), inner - 2)} "
        rows.append(PanelRow(palette.paint(title, "facts_muted"), RANK_TITLE))
        if self.detail:
            detail_rows = wrap_line(sanitize_text(self.detail, newlines=True), inner)
            for position, line in enumerate(detail_rows):
                # The first row names the authorization target and outlives wrapped detail.
                rank = RANK_TITLE if position == 0 else RANK_DESCRIPTION
                rows.append(PanelRow(palette.paint(line, "text"), rank))
        rows.append(PanelRow("", RANK_BLANK))
        rows.append(PanelRow(palette.paint(QUESTION_PERMISSION, "text"), RANK_CORE))
        anchor = len(rows)
        hint_rows: list[int] = []
        for index, label in enumerate((OPTION_YES, OPTION_NO), start=1):
            marker = "›" if index - 1 == self.selected else " "
            rows.append(
                PanelRow(
                    palette.paint(f"{marker} {index}. {label}", "interaction"),
                    RANK_CORE,
                )
            )
            if index - 1 == self.selected:
                anchor = len(rows) - 1
        if self.hint:
            rows.append(PanelRow("", RANK_BLANK))
            for line in wrap_line(self.hint, inner):
                rows.append(PanelRow(palette.paint(line, "text"), RANK_CORE))
                hint_rows.append(len(rows) - 1)
        rows.append(PanelRow("", RANK_BLANK))
        rows.append(PanelRow(palette.paint(FOOTER_PERMISSION, "muted"), RANK_FOOTER))
        rows.append(
            PanelRow(
                palette.paint(_fit(SEPARATOR, inner), "interaction"), RANK_SEPARATOR
            )
        )
        visible, selected_row = fit_panel_rows(rows, limit, anchor, hint_rows)
        return PanelView([row.text for row in visible], selected_row)

    def panel_lines(
        self, width: int, palette: Palette, *, limit: int | None = None
    ) -> list[str]:
        """Return physical panel rows without the selection anchor."""
        return self.panel(width, palette, limit=limit).lines


def _fit(text: str, width: int) -> str:
    """Truncate a separator or label to the display width."""
    if cell_len(text) <= width:
        return text
    return truncate_tail(text, width)


__all__ = [
    "HINT_EMPTY_ANSWER",
    "HINT_RESPONSE_REJECTED",
    "AskInteraction",
    "InteractionOutcome",
    "PanelRow",
    "PanelView",
    "PermissionInteraction",
    "fit_panel_rows",
]
