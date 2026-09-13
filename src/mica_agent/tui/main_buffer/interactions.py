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

# 面板内的固定安全提示：只说明下一步能做什么，不回显响应、ID、Broker 状态、
# 异常文本、路径或工具参数。
HINT_EMPTY_ANSWER = "请输入内容"
HINT_RESPONSE_REJECTED = "提交未被接受，请重试或按 Esc 取消"
HIDDEN_ABOVE = "… 上方还有 {count} 行"
HIDDEN_BELOW = "… 下方还有 {count} 行"

# 面板行的重要性：数值越大越晚删除。空行最先删，其次是选项说明与冗长详情，
# 再次是标题、footer 与分隔线；问题主句、选项标签、自定义行、提示与文本输入
# 行不参与按级删除（空间实在不够时由窗口化保证当前选中行可见）。
RANK_BLANK = 0
RANK_DESCRIPTION = 1
RANK_TITLE = 2
RANK_FOOTER = 3
RANK_SEPARATOR = 4
RANK_CORE = 5


@dataclass(frozen=True, slots=True)
class PanelRow:
    """面板的一行及其降级重要性。"""

    text: str
    rank: int


@dataclass(frozen=True, slots=True)
class PanelView:
    """一次面板排版的结果：物理行与当前选中行。"""

    lines: list[str]
    selected_row: int


def fit_panel_rows(
    rows: list[PanelRow], limit: int | None, anchor: int, also: list[int] | None = None
) -> tuple[list[PanelRow], int]:
    """把面板压到 ``limit`` 行以内，并保证 ``anchor`` 行可见。

    先按重要性从低到高删除可选行（同级里优先删离选中行最远的行）；仍然放不下
    时围绕选中行取窗口，窗口**必须**包含选中行，并用固定提示说明上下方剩余
    行数。``also`` 给出窗口化时优先与选中行同窗的行（面板内的固定提示），
    ``limit`` 为 ``None`` 时不改变任何行。返回值是（可见行, 选中行在可见行中
    的下标）：只要 ``rows`` 非空且 ``limit >= 1``，该下标一定落在可见行范围内，
    且 ``visible[下标]`` 就是原 ``anchor`` 行。
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
    """该级别里离选中行最远的行下标（同距离时取靠后的行）。"""
    candidates = [index for index, row in enumerate(rows) if row.rank == rank]
    return max(candidates, key=lambda index: (abs(index - focus), index))


def _window_rows(
    rows: list[PanelRow], limit: int, focus: int, preferred: list[int]
) -> tuple[list[PanelRow], int]:
    """围绕选中行取窗口：**选中行是硬约束**，其余目标只能次级争取。

    候选窗口一律被限制在选中行的两侧（``start <= focus < end``），因此无论提示
    行、问题主句还是上下隐藏计数怎样竞争，都不可能把选中行挤出结果。评分顺序
    是``（覆盖的提示行数, 是否已交代被隐藏的行, 内容行数, 更靠上）``：

    * 提示行（拒绝/空答案提示）优先，它直接说明面板为何还在；
    * 其次要求窗口有交代：要么不隐藏任何行，要么用上下计数把隐藏行说清楚，
      要么已经显示出那条固定提示（提示比计数更能说明状态）；
    * 内容行数最后比较，用来尽量留住问题主句与选项标签。

    返回的选中行下标始终指向 ``rows[focus]``（原本的 ``anchor`` 行）。
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
    # 计数行只在两条都能放下时才显示：宁可不说，也不留下半个误导性的说明。
    if slots <= budget - (end - start):
        if start > 0:
            prefix = [PanelRow(HIDDEN_ABOVE.format(count=start), RANK_CORE)]
        if end < len(rows):
            suffix = [PanelRow(HIDDEN_BELOW.format(count=len(rows) - end), RANK_CORE)]
    return [*prefix, *rows[start:end], *suffix], len(prefix) + (inside - start)


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
    hint: str = ""

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

    def fail(self) -> None:
        """业务侧拒绝本次响应：只在面板内留下固定安全提示。"""
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
        # 只有真正被本交互消费、并且改变状态的按键才清除上一次的提示：PageUp、
        # PageDown、未知功能键与越界数字不改变交互，也就不该让拒绝提示消失。
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
                self.hint = ""
            return InteractionOutcome.none()
        if key.name == "text" and key.data.isdigit():
            # 数字键与面板的公开编号一致：只定位已编号的选项行。
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

    # -- 文本模式 -----------------------------------------------------------

    def _handle_text(self, key: Key) -> InteractionOutcome:
        if key.name == "escape":
            return InteractionOutcome.cancel()
        if (
            key.name in {"up", "down"}
            and self.editing == "custom"
            and self.prompt.options
        ):
            # 自定义输入里 Up/Down 退出编辑回到固定选项：Up 落到最后一个固定
            # 选项，Down 落到第一个；草稿保留，等再次进入时恢复。
            self.hint = ""
            self.editing = "options"
            self.selected = len(self.prompt.options) - 1 if key.name == "up" else 0
            return InteractionOutcome.none()
        if self.editing == "notes" and key.name == "enter":
            self.hint = ""
            self.editing = "options"
            return InteractionOutcome.none()
        if key.name == "enter":
            # Enter 是重试：先清掉旧提示；空答案再换成「请输入内容」。
            self.hint = ""
            answer = self.buffer_text().strip()
            if not answer:
                self.hint = HINT_EMPTY_ANSWER
                return InteractionOutcome.none()
            if self.editing == "custom":
                # 自定义回答不是编号选项，必须用 ``None`` 下标提交，否则会被
                # 业务侧当作越界选项拒绝。
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
        """写入文本缓冲；返回本次是否真的改变了内容。"""
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

    # -- 绘制 ---------------------------------------------------------------

    def panel(
        self, width: int, palette: Palette, *, limit: int | None = None
    ) -> PanelView:
        """排版面板；``limit`` 给出当前可用行数，超出时按固定优先级降级。"""
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
        """面板的物理行（不含选中行锚点，供只关心行内容的使用者）。"""
        return self.panel(width, palette, limit=limit).lines

    def _question_rows(self, question: str, inner: int, limit: int | None) -> list[str]:
        """问题主句至少保留一行；行数不足时优先把空间让给选项标签。"""
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
        """问题之外无法再删的行数：全部选项标签与自定义行。"""
        return self.row_count


@dataclass
class PermissionInteraction:
    """权限确认面板的活动状态；只提供一次允许和拒绝。"""

    interaction_id: str
    tool_name: str
    detail: str = ""
    selected: int = 0
    hint: str = ""

    def fail(self) -> None:
        """业务侧拒绝本次响应：只在面板内留下固定安全提示。"""
        self.hint = HINT_RESPONSE_REJECTED

    def handle(self, key: Key) -> InteractionOutcome:
        # 与 Ask 一致：只有被本交互消费、并且改变状态的按键才清除拒绝提示。
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
        """排版面板；``limit`` 给出当前可用行数，超出时按固定优先级降级。"""
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
                # 第一行是真正的授权对象，比后续换行更晚删除。
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
        """面板的物理行（不含选中行锚点，供只关心行内容的使用者）。"""
        return self.panel(width, palette, limit=limit).lines


def _fit(text: str, width: int) -> str:
    """把分隔线或标签裁剪到给定显示宽度。"""
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
