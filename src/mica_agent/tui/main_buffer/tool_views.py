"""主缓冲区专用工具视图：安全展示快照 → 宽状态色块。

一次工具调用只产生一条语义记录，状态变化时原地更新。视图只消费公共
``ToolPresentationSnapshot``（已经清洗、没有运行时资源），不读取工具参数、
ToolResult、工作区、Provider 或 Executor 私有状态。

视觉规则：

* 待执行/执行为深蓝、成功为深绿、失败或取消为深红，背景填满可用内容宽度；
* 彩色模式由背景表达状态，NO_COLOR 使用 ``○ ● ✓ × ■`` 与固定安全原因；
* 卡片标题不出现 ``running``/``success``/``completed`` 等状态词；
* 长内容按工具类型折叠，全局展开只影响终态记录。

``ToolViewFactory`` 是唯一的解析接缝：内置工具固定优先，外部映射在构造时
复制且不能覆盖内置名；处理器抛普通异常、返回非法对象或身份不一致时安全
回退通用视图，异常文本与 traceback 不进入卡片或诊断。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from mica_agent.tool_presentation import ToolPresentationKind

from .renderer import Palette, cell_len
from .sanitize import sanitize_scalar, sanitize_text

# 工具状态 → 背景样式名（颜色只表达生命周期，不是唯一线索）。
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
    """读取快照 payload 中的文本；即使快照来自扩展也再清洗一次。"""
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
    """一段文本及其样式名。"""

    text: str = field(repr=False)
    style: str = "text"


class ToolView:
    """工具视图基类：负责状态背景、安全原因与 NO_COLOR 状态符号。"""

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
        """该记录是否存在可展开的额外安全内容。"""
        return False

    def reason(self) -> str:
        """固定安全原因：只使用快照携带的安全消息。"""
        message = getattr(self._snapshot, "message", None)
        return sanitize_scalar(message, fallback="") if message else ""

    # -- 渲染 ---------------------------------------------------------------

    def rows(
        self, width: int, palette: Palette, *, expanded: bool
    ) -> list[list[_Part]]:
        """返回未填色的逻辑行（子类实现）。"""
        raise NotImplementedError

    def render(self, width: int, palette: Palette, expanded: bool) -> list[str]:
        """返回物理行：填满可用宽度的状态色块（NO_COLOR 下带状态符号）。"""
        width = max(1, width)
        rows = self.rows(width, palette, expanded=expanded)
        background = STATE_STYLES[self._state]
        lines = [_compose(row, width, palette, background) for row in rows]
        if not palette.enabled and lines:
            symbol = STATE_SYMBOLS[self._state]
            lines[0] = _truncate(f"{symbol} {lines[0]}", width)
        return lines

    def split(self, *args: object, **kwargs: object) -> None:
        """工具记录有界，永远不参与流式切分。"""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} state={self._state}>"


def _compose(parts: list[_Part], width: int, palette: Palette, background: str) -> str:
    """把逻辑行拼成一条填满背景宽度的物理行。"""
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
    """从长到短选择能放进可用宽度的折叠提示（窄终端保留核心信息）。"""
    for text in candidates:
        if cell_len(text) <= width:
            return text
    return candidates[-1]


def _wrap(text: str, width: int, indent: str = "") -> list[str]:
    from .renderer import wrap_line

    return wrap_line(text, max(1, width), continuation=indent)


class ReadToolView(ToolView):
    """单行紧凑卡片：只显示工具名与安全相对路径。"""

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
    """文件内容预览卡片：纯文本正文与十行折叠。"""

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
    """逐行差异卡片：真实行号、上下文、二十条变化折叠。"""

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
    """命令 + 动态输出卡片：命令行三行折叠、输出八行有界预览。"""

    kind = "bash"

    @property
    def command_lines(self) -> list[str]:
        command = _payload_text(self._snapshot, "command")
        return _logical_lines(command) if command else []

    @property
    def output_lines(self) -> list[str]:
        """终态用快照全文；活动工具用有界实时缓冲。"""
        if self.state in {"running", "pending"} and self._live_output:
            return _logical_lines(self._live_output)
        output = _payload_text(self._snapshot, "output")
        return _logical_lines(output) if output else []

    @property
    def output_hidden(self) -> int:
        """被折叠的更早逻辑行数（活动工具按已收到的总数计算）。"""
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
    """ask 完成记录：问题与真实答案；备注随答案一起显示。"""

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
    """通用兜底：只显示工具名与固定安全原因，不伪造可展开内容。"""

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

# 内置工具名 → presentation kind；两者不一致时拒绝投影，不回退通用视图。
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
    """完整快照值比较：字段等值的副本接受，任一字段不同即拒绝。

    与 ``create_tool_card`` 的既有工厂契约一致：身份（call_id、
    tool_name）、kind、status、message 与 payload 等全部字段按值比较，
    因此两次独立构造但字段完全相同的快照会被接受。
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
    """把安全展示快照解析成专用工具视图的单一接缝。

    外部只能按工具名提供额外构建器，且不能覆盖内置名；构建器只接收安全
    快照，普通异常、非法返回或身份不一致时安全回退通用视图，异常文本、
    ``repr`` 与 traceback 不进入卡片或诊断（只累加一个固定计数）。
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
        """安全回退到通用视图的次数（不含异常或身份细节）。"""
        return self._fallbacks

    @property
    def overrides(self) -> tuple[str, ...]:
        """已接受的外部工具名（构造时复制，不暴露映射本身）。"""
        return tuple(sorted(self._overrides))

    def create(
        self,
        snapshot: object,
        *,
        state: str,
        live_output: str = "",
        live_total: int = 0,
    ) -> ToolView | None:
        """按内置 kind 优先的统一契约解析展示视图。

        ``None`` 表示身份与 kind 不匹配，调用方应拒绝投影。顺序与既有
        Textual seam 一致：保留工具名只接受对应 kind；内置 kind（含 ASK）
        只给名称正确的内置视图；外部 builder 只能处理 ``GENERIC`` 且非保留名。
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
                # 自定义名称不能截获内置 kind（含 ASK），也不回退通用视图。
                return None
            return GenericToolView(snapshot, state=state)
        builder = self._overrides.get(name) if isinstance(name, str) else None
        if builder is None:
            return GenericToolView(snapshot, state=state)
        view: object = None
        try:
            view = builder(snapshot, state)
            accepted = _same_snapshot(view, snapshot)
        except Exception:  # noqa: BLE001 - 工厂、返回类型与快照比较的普通异常边界
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
