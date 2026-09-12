"""ask 与权限的独占输入模式。

两种交互都把输入控制器切到唯一模式（``ASK`` / ``PERMISSION``），独占
``↑``/``↓``、数字键、``Enter``、``Esc`` 和自己的文本输入。它们只产生
公共响应对象或取消意图，不直接调用 Broker、Engine、Provider 或 Store。
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


@dataclass(frozen=True, slots=True)
class InteractionOutcome:
    """一次按键处理的结果。"""

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
    """结构化 ask 面板的活动状态。"""

    interaction_id: str
    prompt: AskPrompt
    selected: int = 0
    editing: str = "options"
    note: str = ""
    custom: str = ""
    _text: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        self.selected = 0
        if not self.prompt.options:
            self.editing = "free"

    def _custom_enabled(self) -> bool:
        return bool(self.prompt.allow_custom and self.prompt.options)

    @property
    def row_count(self) -> int:
        """可选行总数：选项行加上可选的 ``Type something.`` 行。"""
        return len(self.prompt.options) + (1 if self._custom_enabled() else 0)

    @property
    def is_custom_row(self) -> bool:
        return self._custom_enabled() and self.selected >= len(self.prompt.options)

    @property
    def is_text_entry(self) -> bool:
        return self.editing in {"custom", "free"}

    def buffer_text(self) -> str:
        if self.editing == "notes":
            return self.note
        if self.editing == "custom":
            return self.custom
        if self.editing == "free":
            return self._text
        return ""

    def handle(self, key: Key) -> InteractionOutcome:
        if self.is_text_entry or self.editing == "notes":
            return self._handle_text(key)
        return self._handle_options(key)

    # -- 选项模式 -----------------------------------------------------------

    def _handle_options(self, key: Key) -> InteractionOutcome:
        total = self.row_count
        if key.name in {"up", "down"}:
            if total:
                step = -1 if key.name == "up" else 1
                self.selected = (self.selected + step) % total
            return InteractionOutcome.none()
        if key.name == "text" and key.data.isdigit():
            # 数字键与面板的公开编号一致：只定位已编号的选项行。
            index = int(key.data) - 1
            if 0 <= index < len(self.prompt.options):
                self.selected = index
            return InteractionOutcome.none()
        if key.name == "n" or (key.name == "text" and key.data.lower() == "n"):
            self.editing = "notes"
            return InteractionOutcome.none()
        if key.name == "escape":
            return InteractionOutcome.cancel()
        if key.name == "enter":
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

    # -- 文本模式 -----------------------------------------------------------

    def _handle_text(self, key: Key) -> InteractionOutcome:
        if key.name == "escape":
            return InteractionOutcome.cancel()
        if self.editing == "notes" and key.name == "enter":
            self.editing = "options"
            return InteractionOutcome.none()
        if key.name == "enter":
            answer = self.buffer_text().strip()
            if not answer:
                return InteractionOutcome.none()
            if self.editing == "custom":
                return InteractionOutcome(
                    "response",
                    UserInputResponse(
                        interaction_id=self.interaction_id,
                        answer=answer,
                        selected_option_index=self.selected,
                        notes=self.note or None,
                    ),
                )
            return InteractionOutcome(
                "response",
                UserInputResponse(interaction_id=self.interaction_id, answer=answer),
            )
        if key.name == "backspace":
            self._edit_text(-1)
            return InteractionOutcome.none()
        if key.name == "delete":
            self._edit_text(0)
            return InteractionOutcome.none()
        if key.name in {"text", "paste"}:
            self._edit_text(None, key.data)
        return InteractionOutcome.none()

    def _edit_text(self, remove: int | None, insert: str = "") -> None:
        current = self.buffer_text()
        if remove == -1:
            updated = current[:-1]
        elif remove == 0:
            updated = current[1:]
        else:
            updated = current + sanitize_text(insert, newlines=False, tabs=False)
        if self.editing == "notes":
            self.note = updated
        elif self.editing == "custom":
            self.custom = updated
        else:
            self._text = updated

    # -- 绘制 ---------------------------------------------------------------

    def panel_lines(self, width: int, palette: Palette) -> list[str]:
        inner = max(8, width)
        lines = [palette.paint(_fit(SEPARATOR, inner), "interaction")]
        title = sanitize_scalar(self.prompt.title, fallback="")
        if title:
            lines.append(palette.paint(f" {_fit(title, inner - 2)} ", "facts_muted"))
            lines.append("")
        question = sanitize_text(self.prompt.question, newlines=True)
        for line in wrap_line(question, inner):
            lines.append(palette.paint(line, "text"))
        lines.append("")
        if self.prompt.options:
            for index, option in enumerate(self.prompt.options, start=1):
                marker = "›" if index - 1 == self.selected else " "
                label = (
                    f"{marker} {index}. {sanitize_scalar(option.label, fallback='')}"
                )
                if option.recommended:
                    label += " (推荐)"
                lines.append(palette.paint(label, "interaction"))
                if option.description:
                    description = sanitize_scalar(option.description, fallback="")
                    for line in wrap_line(description, inner - 6):
                        lines.append("  " + palette.paint(line, "muted"))
            if self._custom_enabled():
                marker = "›" if self.is_custom_row else " "
                lines.append(palette.paint(f"{marker} {CUSTOM_LABEL}", "interaction"))
        if self.editing == "notes":
            lines.append("")
            lines.append(
                palette.paint(
                    f" 备注: {sanitize_scalar(self.note, fallback='')}", "interaction"
                )
            )
        elif self.is_text_entry:
            lines.append("")
            lines.append(
                palette.paint(
                    f" › {sanitize_scalar(self.buffer_text(), fallback='')}_", "text"
                )
            )
        lines.append("")
        lines.append(
            palette.paint(
                FOOTER_ASK_TEXT if self.is_text_entry else FOOTER_ASK_OPTIONS,
                "muted",
            )
        )
        lines.append(palette.paint(_fit(SEPARATOR, inner), "interaction"))
        return lines


@dataclass
class PermissionInteraction:
    """权限确认面板的活动状态；只提供一次允许和拒绝。"""

    interaction_id: str
    tool_name: str
    detail: str = ""
    selected: int = 0

    def handle(self, key: Key) -> InteractionOutcome:
        if key.name in {"up", "down"}:
            self.selected = 1 - self.selected
            return InteractionOutcome.none()
        if key.name == "text" and key.data in {"1", "2"}:
            self.selected = int(key.data) - 1
            return InteractionOutcome.none()
        if key.name == "escape":
            return InteractionOutcome.cancel()
        if key.name == "enter":
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

    def panel_lines(self, width: int, palette: Palette) -> list[str]:
        inner = max(8, width)
        lines = [palette.paint(_fit(SEPARATOR, inner), "interaction")]
        title = f" {_fit(sanitize_scalar(self.tool_name, fallback='tool'), inner - 2)} "
        lines.append(palette.paint(title, "facts_muted"))
        if self.detail:
            for line in wrap_line(sanitize_text(self.detail, newlines=True), inner):
                lines.append(palette.paint(line, "text"))
        lines.append("")
        lines.append(palette.paint(QUESTION_PERMISSION, "text"))
        for index, label in enumerate((OPTION_YES, OPTION_NO), start=1):
            marker = "›" if index - 1 == self.selected else " "
            lines.append(palette.paint(f"{marker} {index}. {label}", "interaction"))
        lines.append("")
        lines.append(palette.paint(FOOTER_PERMISSION, "muted"))
        lines.append(palette.paint(_fit(SEPARATOR, inner), "interaction"))
        return lines


def _fit(text: str, width: int) -> str:
    """把分隔线或标签裁剪到给定显示宽度。"""
    if cell_len(text) <= width:
        return text
    return truncate_tail(text, width)


__all__ = [
    "AskInteraction",
    "InteractionOutcome",
    "PermissionInteraction",
]
