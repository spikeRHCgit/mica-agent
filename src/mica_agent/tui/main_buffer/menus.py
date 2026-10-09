"""Local model-menu input and layout using only safe public directory choices."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mica_agent.session_contracts import SessionSelection

from .editor import Key
from .interactions import (
    RANK_BLANK,
    RANK_CORE,
    RANK_FOOTER,
    RANK_SEPARATOR,
    RANK_TITLE,
    PanelRow,
    PanelView,
    fit_panel_rows,
)
from .renderer import Palette, cell_len, truncate_head, wrap_line
from .sanitize import sanitize_scalar, sanitize_text

if TYPE_CHECKING:
    from mica_agent.app import ModelChoice

FOOTER_MODEL = "Enter Switch · Ctrl+S Set default · ↑/↓ Move · Esc Close"
FOOTER_CONFIRM = "Enter Confirm switch · Esc Back"


@dataclass(frozen=True, slots=True)
class MenuOutcome:
    action: Literal["none", "close", "switch", "save_default"]
    selection: SessionSelection | None = None


class ModelMenu:
    def __init__(self, choices: tuple[ModelChoice, ...]) -> None:
        self.choices = choices
        self.filter_text = ""
        self._index = next(
            (index for index, item in enumerate(choices) if item.is_current), 0
        )
        self._hint = ""
        self._busy = False
        self._confirming = False

    @property
    def filtered(self) -> tuple[ModelChoice, ...]:
        query = self.filter_text.casefold()
        return tuple(
            item
            for item in self.choices
            if query in sanitize_scalar(item.model_name).casefold()
            or query in sanitize_scalar(item.provider_name).casefold()
        )

    @property
    def selected(self) -> ModelChoice | None:
        choices = self.filtered
        return choices[self._index] if choices else None

    @property
    def current(self) -> ModelChoice | None:
        return next((item for item in self.choices if item.is_current), None)

    def set_choices(self, choices: tuple[ModelChoice, ...]) -> None:
        selected = self.selected
        self.choices = choices
        filtered = self.filtered
        self._index = next(
            (
                index
                for index, item in enumerate(filtered)
                if selected is not None and item.selection == selected.selection
            ),
            min(self._index, max(0, len(filtered) - 1)),
        )

    def set_hint(self, text: str) -> None:
        self._hint = sanitize_scalar(text, fallback="")

    def set_busy(self, busy: bool) -> None:
        self._busy = busy

    def handle(self, key: Key) -> MenuOutcome:
        if key.name == "escape":
            if self._confirming and not self._busy:
                self._confirming = False
                return MenuOutcome("none")
            return MenuOutcome("close")
        if self._busy or not self.choices:
            return MenuOutcome("none")
        target = self.selected
        if self._confirming:
            if key.name == "enter" and target is not None:
                return MenuOutcome("switch", target.selection)
            return MenuOutcome("none")
        if key.name in {"text", "paste", "backspace"}:
            if key.name == "backspace":
                self.filter_text = self.filter_text[:-1]
            else:
                self.filter_text += sanitize_text(key.data, newlines=False, tabs=False)
            self._index = 0
            self._hint = ""
        elif key.name in {"up", "down"} and target is not None:
            self._index = min(
                max(0, self._index + (-1 if key.name == "up" else 1)),
                len(self.filtered) - 1,
            )
            self._hint = ""
        elif key.name == "ctrl_s" and target is not None:
            return MenuOutcome("save_default", target.selection)
        elif key.name == "enter" and target is not None:
            self._hint = ""
            if target.is_current:
                return MenuOutcome("close")
            current = self.current
            if current is not None and target.context_window < current.context_window:
                self._confirming = True
            else:
                return MenuOutcome("switch", target.selection)
        return MenuOutcome("none")

    @staticmethod
    def _capacity(choice: ModelChoice) -> str:
        suffix = " (default)" if choice.context_window_is_default else ""
        return f"{choice.context_window:,}{suffix}"

    @staticmethod
    def _wrap(text: str, width: int) -> list[str]:
        # A whitespace-only wrap prefix can overflow a one-cell terminal. Trim only
        # overflowing rows here, keeping the shared renderer and other panels unchanged.
        return [
            truncate_head(line.lstrip(), width) if cell_len(line) > width else line
            for line in wrap_line(text, width)
        ]

    def _choice_rows(
        self, choice: ModelChoice, selected: bool, width: int, palette: Palette
    ) -> list[PanelRow]:
        color = "interaction" if choice.is_current else "muted"
        prefix = (
            palette.paint("›" if selected else " ", "interaction")
            + " "
            + palette.paint("✓" if choice.is_current else " ", color)
            + " "
        )
        labels = []
        if choice.is_current:
            labels.append(("Current", "interaction"))
        if choice.is_default:
            labels.append(("Default", "muted"))
        label_width = sum(cell_len(label) for label, _ in labels) + max(
            0, len(labels) - 1
        )
        label_text = " ".join(palette.paint(label, style) for label, style in labels)
        name = f"{sanitize_scalar(choice.model_name)} [{sanitize_scalar(choice.provider_name)}]"
        if width >= 5 + label_width:
            budget = width - 5 - label_width - int(bool(labels))
            name = truncate_head(name, budget)
            gap = " " * (width - 5 - cell_len(name) - label_width) if labels else ""
            return [
                PanelRow(
                    " " + prefix + palette.paint(name, color) + gap + label_text,
                    RANK_CORE,
                )
            ]
        # At widths too small for full markers in one row, labels occupy continuation rows.
        marker = truncate_head(
            f" {'›' if selected else ' '} {'✓' if choice.is_current else ' '}", width
        )
        rows = [PanelRow(palette.paint(marker, "interaction"), RANK_CORE)]
        for label, style in labels:
            rows.extend(
                PanelRow(palette.paint(line, style), RANK_CORE)
                for line in self._wrap(label, width)
            )
        return rows

    def panel(
        self, width: int, palette: Palette, *, limit: int | None = None
    ) -> PanelView:
        inner = max(1, width)
        separator = palette.paint("─" * inner, "interaction")
        rows = [
            PanelRow(separator, RANK_SEPARATOR),
            PanelRow(
                palette.paint(truncate_head(" Select model", inner), "facts_muted"),
                RANK_TITLE,
            ),
        ]
        filter_line = (
            f"Filter: {self.filter_text}" if self.filter_text else "Type to filter..."
        )
        rows.extend(
            PanelRow(palette.paint(line, "muted"), RANK_CORE)
            for line in self._wrap(" " + filter_line, inner)
        )
        rows.append(PanelRow("", RANK_BLANK))
        anchor = len(rows)
        choices = self.filtered
        if not choices:
            message = "No matching models" if self.choices else "No models available"
            rows.extend(
                PanelRow(palette.paint(line, "muted"), RANK_CORE)
                for line in self._wrap(message, inner)
            )
        else:
            for index, choice in enumerate(choices):
                if index == self._index:
                    anchor = len(rows)
                rows.extend(
                    self._choice_rows(choice, index == self._index, inner, palette)
                )
        hints: list[str] = []
        current, target = self.current, self.selected
        if (
            current is not None
            and target is not None
            and current.selection.provider_id != target.selection.provider_id
        ):
            hints.append(
                f"Future requests will send this session's history to {sanitize_scalar(target.provider_name)}."
            )
        if self._confirming and current is not None and target is not None:
            hints.extend(
                (
                    f"Context window will shrink: {self._capacity(current)} → {self._capacity(target)}",
                    "Existing history may exceed the new limit.",
                )
            )
        if self._hint:
            hints.append(self._hint)
        hint_rows = []
        for hint in hints:
            for line in self._wrap(hint, inner):
                hint_rows.append(len(rows))
                rows.append(PanelRow(palette.paint(line, "muted"), RANK_CORE))
        footer = (
            "Switching..."
            if self._busy
            else FOOTER_CONFIRM
            if self._confirming
            else FOOTER_MODEL
            if self.choices
            else "Esc Close"
        )
        rows.append(PanelRow("", RANK_BLANK))
        rows.extend(
            PanelRow(palette.paint(line, "muted"), RANK_FOOTER)
            for line in self._wrap(" " + footer, inner)
        )
        rows.append(PanelRow(separator, RANK_SEPARATOR))
        visible, selected_row = fit_panel_rows(rows, limit, anchor, hint_rows)
        lines = []
        for row in visible:
            hidden = re.fullmatch(r"… (上方|下方)还有 (\d+) 行", row.text)
            if hidden:
                direction, count = hidden.groups()
                text = f"… {count} more {'above' if direction == '上方' else 'below'}"
                lines.append(palette.paint(truncate_head(text, inner), "muted"))
            else:
                lines.append(row.text)
        return PanelView(lines, selected_row)
