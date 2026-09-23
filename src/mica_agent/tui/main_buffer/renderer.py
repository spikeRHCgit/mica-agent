"""主缓冲区语义文档与差分帧渲染。

渲染器保存**未换行**的语义块（或块自己生成的物理行），按当前终端宽度在
绘制时排版；因此终端 resize 后可以重新排版。屏上内容分为三部分：

* 永久文档区：已经写出、不再重绘的语义块，会自然进入 scrollback。
* 重绘区：最近一次新写入的块与唯一可重写尾部块，每次绘制重新排版。
* chrome：工作状态、输入框和事实信息，始终位于屏幕最底部。

物理模型是「保留式屏幕映射」：渲染器保存上一帧每个**物理行**的身份
（块索引 + 块内行号，chrome 行为 ``-1``）。重绘时按身份与文本求公共前缀，
只回到第一个真正变化的行，擦除其下方内容再写出新行，因此：

* 普通增量只重绘变化尾部；
* 较早块高度变化（全局工具展开、宽度变化）时从该块起点受控重排；
* 文本不变而只有目标光标移动（方向键/Home/End）时只发送相对光标定位，
  不写正文、不擦除，也不重排任何行；
* 首帧只从当前光标位置向下写出，不向上移动、不擦除启动前内容；
* 屏幕映射保存的行数不超过终端高度，因此映射中的每一行都仍在屏幕上，
  回到其中任意一行所需的上移量都不会越过屏幕顶部。

渲染器从不使用 alternate screen、鼠标捕获、清除 scrollback 或窗口标题。
块的正文一律视为不可信：文本块在排版前过清洗接缝；自带 ANSI 的块只允许
渲染器/组件自己生成的可信样式序列。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .sanitize import sanitize_text

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from .terminal import Terminal

_ERASE_TO_END = "\x1b[J"
_ZERO_WIDTH_CATEGORIES = frozenset({"Mn", "Me", "Cf"})
# 制表符的显示宽度：显示、软换行与光标单元格共用同一个常量。
TAB_WIDTH = 4

# 与 Textual 侧主题令牌保持一致的语义色（真彩色 ANSI）。
BRAND_LIGHT = "38;2;125;211;252"
BRAND = "38;2;59;130;246"
BRAND_DEEP = "38;2;29;78;216"
MUTED = "38;2;148;163;184"
TEXT = "38;2;241;245;249"
SUCCESS = "38;2;34;197;94"
ERROR = "38;2;239;68;68"

BG_PENDING = "48;2;30;58;138"
BG_SUCCESS = "48;2;20;83;45"
BG_ERROR = "48;2;127;29;29"

BG_USER = "48;2;30;41;59"
BG_CODE_BLOCK = "48;2;24;30;43"
INLINE_CODE = "38;2;137;180;250"
REASONING = "2;38;2;148;163;184"

DEFAULT_STYLES: dict[str, str] = {
    "raw": "",
    "logo": BRAND,
    "brand": BRAND_LIGHT,
    "muted": MUTED,
    "text": TEXT,
    "user": BRAND_LIGHT,
    "assistant": TEXT,
    "reasoning": REASONING,
    "tool_pending": BG_PENDING,
    "tool_running": BG_PENDING,
    "tool_success": BG_SUCCESS,
    "tool_failed": BG_ERROR,
    "tool_cancelled": BG_ERROR,
    "status": BRAND_LIGHT,
    "status_cancel": ERROR,
    "composer": TEXT,
    "border": MUTED,
    "facts": BRAND_LIGHT,
    "facts_muted": MUTED,
    "interaction": BRAND_LIGHT,
    # P5-11B 内容层级
    "user_bg": BG_USER,
    "user_marker": BRAND,
    "reasoning_line": MUTED,
    "heading": BRAND_LIGHT,
    "link": BRAND_LIGHT,
    "code_block": BG_CODE_BLOCK,
    "inline_code": INLINE_CODE,
    "tool_read": BG_SUCCESS,
    "tool_write": BG_SUCCESS,
    "tool_edit": BG_SUCCESS,
    "tool_bash": BG_SUCCESS,
    "tool_ask": BG_SUCCESS,
    "tool_generic": BG_SUCCESS,
    "tool_name": TEXT,
    "tool_path": BRAND_LIGHT,
    "tool_diff_add": "38;2;190;242;100",
    "tool_diff_remove": "38;2;252;165;165",
    "tool_output": TEXT,
}


def char_cells(character: str) -> int:
    """返回单个字符占用的终端列数。"""
    if character == "\t":
        return TAB_WIDTH
    if unicodedata.combining(character):
        return 0
    if unicodedata.category(character) in _ZERO_WIDTH_CATEGORIES:
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def cell_len(text: str) -> int:
    """返回文本占用的终端列数。"""
    return sum(char_cells(character) for character in text)


def expand_tabs(text: str) -> str:
    """把制表符确定性展开为空格。

    展开宽度与 :func:`cell_len` 对 ``\\t`` 的度量共用 ``TAB_WIDTH``，
    因此显示层、软换行与光标单元格用同一套计算，不依赖终端自身的制表位。
    """
    if "\t" not in text:
        return text
    return text.replace("\t", " " * TAB_WIDTH)


def truncate_tail(text: str, limit: int) -> str:
    """按终端列宽截断文本，保留尾部并加省略号。"""
    if limit <= 0:
        return ""
    if cell_len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    remaining = limit - 1
    suffix: list[str] = []
    used = 0
    for character in reversed(text):
        width = char_cells(character)
        if used + width > remaining:
            break
        suffix.append(character)
        used += width
    return "…" + "".join(reversed(suffix))


def truncate_head(text: str, limit: int) -> str:
    """按终端列宽从头部截断文本（不保留省略号，用于输入框正文）。"""
    if limit <= 0:
        return ""
    used = 0
    output: list[str] = []
    for character in text:
        width = char_cells(character)
        if used + width > limit:
            break
        output.append(character)
        used += width
    return "".join(output)


def wrap_line(text: str, width: int, *, continuation: str = "") -> list[str]:
    """把文本按终端列宽硬换行。

    显式 ``\\n`` 会拆成多行，每一行再按宽度硬换行；``continuation`` 加在
    除首行以外的行首，用于保持阅读缩进。
    """
    width = max(1, width)
    output: list[str] = []
    for logical in text.split("\n"):
        prefix = continuation if output else ""
        output.extend(_wrap_single(logical, width, prefix))
    return output or [""]


def _wrap_single(text: str, width: int, prefix: str) -> list[str]:
    if not text:
        return [prefix.rstrip()]
    lines: list[str] = []
    current = prefix
    current_width = cell_len(prefix)
    for character in text:
        character_width = char_cells(character)
        if current_width + character_width > width and current.strip():
            lines.append(current)
            current = prefix
            current_width = cell_len(prefix)
        current += character
        current_width += character_width
    lines.append(current)
    return lines


def safe_block_text(block: Block) -> str:
    """返回块内可排版正文。

    ``styled=True`` 是渲染器自有的可信内容（Logo、已排版并着色的面板行）；
    其余语义正文一律视为不可信，在排版前先过唯一清洗接缝。
    """
    return block.text if block.styled else sanitize_text(block.text, newlines=True)


# 高层对话记录：用户、思考、正式回复、工具。启动 Logo/面板/文案不是记录。
_RECORD_KINDS = frozenset(
    {
        "user",
        "reasoning",
        "assistant",
        "tool",
        "read",
        "write",
        "edit",
        "bash",
        "ask",
        "generic",
        "muted",
    }
)
# 间距行挂在后一块流上，行号为 base-1，不与正文行或 chrome（stream=-1）冲突。
_SPACER_LINE = -1


def _is_record(block: BlockLike) -> bool:
    """是否为需要参与语义间距的高层对话记录。"""
    return getattr(block, "kind", "") in _RECORD_KINDS


def wrap_blocks(
    blocks: list[BlockLike],
    width: int,
    *,
    expanded: bool = False,
    palette: Palette | None = None,
) -> list[str]:
    """把语义块序列展开成终端行（未着色的逻辑视图）。

    预着色文本块（例如 Logo）按原样返回；其余块按无色模式渲染并去掉
    ANSI，用于测试、诊断与逻辑文档视图。不含语义间距；间距由渲染器
    按逻辑流身份统一插入。
    """
    lines: list[str] = []
    painter = Palette(False) if palette is None else palette
    for block in blocks:
        if isinstance(block, Block) and block.styled:
            lines.append(block.text)
            continue
        for row in block.render(width, painter, expanded):
            lines.append(
                row if palette is not None and palette.enabled else strip_ansi(row)
            )
    return lines


def strip_ansi(text: str) -> str:
    """去掉物理行中的 ANSI 序列，用于逻辑视图与测试。"""
    output: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == "\x1b":
            index += 1
            if index < length and text[index] == "[":
                index += 1
                while index < length and not ("\x40" <= text[index] <= "\x7e"):
                    index += 1
                index += 1
            elif index < length:
                index += 1
            continue
        output.append(character)
        index += 1
    return "".join(output)


@dataclass(frozen=True, slots=True)
class Block:
    """一个未换行的纯文本语义块。

    ``styled`` 表示正文已经自带 ANSI 序列（例如逐字符着色的 Logo），
    渲染器不会再对它换行、测量或重新着色。
    """

    kind: str
    text: str = field(repr=False)
    continuation: str = ""
    styled: bool = False

    def render(self, width: int, palette: Palette, expanded: bool) -> list[str]:
        """按当前宽度与调色板生成物理行（忽略全局展开状态）。"""
        if self.styled:
            return [self.text]
        return [
            palette.paint(line, self.kind)
            for line in wrap_line(
                safe_block_text(self), width, continuation=self.continuation
            )
        ]

    def split(
        self, keep_tail: int, *, width: int, palette: Palette, expanded: bool
    ) -> tuple[BlockLike, BlockLike] | None:
        """按物理行切分：返回（永久头部, 可重写尾部）。

        纯文本块按**未着色**的换行结果切分，因此头部与尾部仍然保存原始
        安全文本，后续绘制时再按当时宽度重新着色。
        """
        if keep_tail <= 0 or self.styled:
            return None
        rows = wrap_line(safe_block_text(self), width, continuation=self.continuation)
        if len(rows) <= keep_tail:
            return None
        head = Block(self.kind, "\n".join(rows[: len(rows) - keep_tail]))
        tail = Block(self.kind, "\n".join(rows[len(rows) - keep_tail :]))
        return head, tail


@dataclass(frozen=True, slots=True)
class RenderedBlock:
    """已经排版好的物理行块（例：Markdown 渲染结果、冻结的流式前缀）。

    行内 ANSI 只允许由渲染器、组件与 Rich 主题映射生成；正文在构造前
    已经过清洗接缝，因此这里的行被当作可信物理输出。
    """

    kind: str
    lines: tuple[str, ...] = field(repr=False)

    def render(self, width: int, palette: Palette, expanded: bool) -> list[str]:
        return list(self.lines)

    def split(
        self, keep_tail: int, *, width: int, palette: Palette, expanded: bool
    ) -> tuple[BlockLike, BlockLike] | None:
        if keep_tail <= 0 or len(self.lines) <= keep_tail:
            return None
        head = RenderedBlock(self.kind, self.lines[: len(self.lines) - keep_tail])
        tail = RenderedBlock(self.kind, self.lines[len(self.lines) - keep_tail :])
        return head, tail


@runtime_checkable
class BlockLike(Protocol):
    """语义块协议：保存安全源数据与展示状态，按需生成物理行。"""

    kind: str

    def render(self, width: int, palette: Palette, expanded: bool) -> list[str]:
        """按当前宽度、调色板与全局展开状态生成物理显示行。"""
        ...

    def split(
        self, keep_tail: int, *, width: int, palette: Palette, expanded: bool
    ) -> tuple[BlockLike, BlockLike] | None:
        """把块切成「永久头部 + 可重写尾部」；不支持切分时返回 ``None``。"""
        ...


class Palette:
    """把语义样式名映射为 ANSI 序列。"""

    def __init__(self, enabled: bool, styles: dict[str, str] | None = None) -> None:
        self.enabled = enabled
        self.styles = DEFAULT_STYLES if styles is None else styles

    def paint(self, text: str, kind: str) -> str:
        code = self.styles.get(kind, "") if self.enabled else ""
        if not code:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    def fill(self, text: str, width: int, kind: str) -> str:
        """把一行填满到给定列宽并使用背景/前景样式。"""
        padded = text + " " * max(0, width - cell_len(text))
        return self.paint(padded, kind)


def _same_rows(left: list[_Row], right: list[_Row]) -> bool:
    """比较两段物理行是否逐行同身份同内容。"""
    if len(left) != len(right):
        return False
    return all(
        first.key == second.key and first.text == second.text
        for first, second in zip(left, right, strict=True)
    )


@dataclass(frozen=True, slots=True)
class _Row:
    """屏幕映射中的一行：逻辑身份 + 实际写出的文本。"""

    key: tuple[int, int]  # (流标识, 逻辑行号)；chrome 行为 (-1, 行号)
    text: str = field(repr=False)

    def __repr__(self) -> str:
        return f"<_Row key={self.key}>"


@dataclass(slots=True)
class _Slot:
    """一个语义块及其在文档中的逻辑身份。"""

    block: BlockLike
    key: int  # 逻辑流标识（同一条消息/工具记录的切分片段共享）
    base: int = 0  # 块内第 0 行对应的逻辑行号


class MainBufferRenderer:
    """语义文档 + 底部重绘区的差分渲染器。

    渲染器维护一份**保留式屏幕映射** ``_view``：上一帧写出的每个物理行
    及其逻辑身份，按屏幕顺序排列并以光标所在行结尾。重绘时按身份与文本求
    公共前缀，只回到第一个真正变化的行；屏幕映射长度不超过终端高度，因此
    映射内每一行都仍在屏幕上，回到其中任意一行都不会越过屏幕顶部。

    不变式：映射中的行连续且按写出顺序排列；首帧只向下写出；擦除永远从
    可寻址行开始，因此不会覆盖启动前的 shell 内容。
    """

    def __init__(
        self,
        terminal: Terminal,
        *,
        width: int,
        height: int,
        palette: Palette,
        interactive: bool = True,
    ) -> None:
        self._terminal = terminal
        self._width = max(1, width)
        self._height = max(1, height)
        self._palette = palette
        self._interactive = interactive
        self._slots: list[_Slot] = []
        self._next_key = 1
        self._pending: int | None = None
        self._pending_text = ""
        self._released: list[str] = []
        self._repaint_from = 0
        self._chrome: list[str] = []
        self._cursor: tuple[int, int] | None = None
        self._expanded = False
        self._view: list[_Row] = []
        self._cursor_view = 0
        # 已写出的物理光标列；``None`` 表示「未定位/未知」，此时不能宣称
        # 光标已经在目标单元格上，只能重新发送一次定位。
        self._cursor_column: int | None = None
        self._drawn = False

    # -- 只读视图 -----------------------------------------------------------

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def chrome(self) -> list[str]:
        return list(self._chrome)

    @property
    def palette(self) -> Palette:
        """当前生效的调色板（只读视图）。"""
        return self._palette

    @property
    def expanded(self) -> bool:
        """全局工具详情开关（唯一事实源）。"""
        return self._expanded

    @property
    def drawn(self) -> bool:
        """是否已经写出过至少一帧（首帧后不再向下开头）。"""
        return self._drawn

    @property
    def frame_rows(self) -> int:
        """屏幕映射中保存的物理行数。"""
        return len(self._view)

    @property
    def frame_cursor_row(self) -> int:
        """光标在屏幕映射内的行号（0 起，等于其上方行数）。"""
        return self._cursor_view

    def frame(self, *, palette: Palette | None = None) -> list[str]:
        """返回当前完整逻辑屏内容（文档 + chrome），用于测试与证据。

        默认按无色渲染（逻辑视图）；传入调色板可得到与屏幕一致的着色版本。
        """
        painter = Palette(False) if palette is None else palette
        return [row.text for row in self._layout_document(palette=painter)] + list(
            self._chrome
        )

    def physical_frame(self) -> list[str]:
        """返回**已经写出到终端**的物理行（即当前屏上所见的行）。"""
        return [row.text for row in self._view]

    def document_lines(self) -> list[str]:
        """返回文档区行（不含 chrome），反映当前全局展开状态。"""
        return [strip_ansi(row.text) for row in self._layout_document()]

    def tracked_rows(self) -> list[tuple[int, int]]:
        """返回屏幕映射中的逻辑行身份，用于诊断与测试。"""
        return [row.key for row in self._view]

    def _block_list(self) -> list[BlockLike]:
        return [slot.block for slot in self._slots]

    def _layout_document(
        self, *, palette: Palette | None = None, start: int = 0
    ) -> list[_Row]:
        """把语义块排成带唯一间距的物理行。

        相邻且不同逻辑流的高层记录之间恰好插入一条无背景空白行；空白行
        挂在后一块流上，行号为 ``base - 1``，因此同一流的切分片段、原地
        更新与流式增量都不会再插入第二条空行。启动 Logo/面板不参与间距。
        """
        painter = self._palette if palette is None else palette
        rows: list[_Row] = []
        previous_key: int | None = None
        previous_record = False
        for index, slot in enumerate(self._slots):
            body = slot.block.render(self._width, painter, self._expanded)
            if not body:
                continue
            record = _is_record(slot.block)
            if (
                index >= start
                and record
                and previous_record
                and previous_key != slot.key
            ):
                rows.append(_Row((slot.key, _SPACER_LINE), ""))
            if index >= start:
                for offset, text in enumerate(body):
                    rows.append(_Row((slot.key, slot.base + offset), text))
            previous_key = slot.key
            previous_record = record
        return rows

    # -- 文档编辑 -----------------------------------------------------------

    def add_block(
        self,
        kind: str,
        text: str,
        *,
        continuation: str = "",
        styled: bool = False,
    ) -> None:
        """追加一个永久文本块（例如用户消息或已收口的工具记录）。"""
        self.append(Block(kind, text, continuation, styled))

    def append(self, block: BlockLike) -> None:
        """追加一个语义块，并结束当前可重写尾部块。"""
        previous_pending = self._pending
        self._pending = None
        self._pending_text = ""
        self._released = []
        self._slots.append(_Slot(block, self._next_key))
        self._next_key += 1
        if previous_pending is not None:
            self._repaint_from = min(self._repaint_from, previous_pending)

    def update_pending(
        self,
        kind: str,
        text: str,
        *,
        continuation: str = "",
        styled: bool = False,
    ) -> None:
        """替换唯一可重写尾部块的内容（工具记录等原地更新）。"""
        self.set_pending(Block(kind, text, continuation, styled))

    def set_pending(self, block: BlockLike) -> None:
        """创建或替换唯一可重写尾部块。

        当新块是**同一逻辑流的增长版本**（开头与已经永久写出的前缀逐行
        相同）时，只保留尚未写出的尾部并沿用逻辑行号，因此已经写出的行
        不会被重复写出，也不会改变屏幕映射中的身份。
        """
        if self._pending is None or self._slots[self._pending].block is block:
            if self._pending is None:
                self._slots.append(_Slot(block, self._next_key))
                self._next_key += 1
                self._pending = len(self._slots) - 1
                self._pending_text = ""
        else:
            slot = self._slots[self._pending]
            trimmed, base = self._resume_stream(block)
            slot.block = trimmed
            slot.base = base
            if isinstance(trimmed, Block):
                self._pending_text = trimmed.text
        self._repaint_from = min(self._repaint_from, self._pending)

    def _resume_stream(self, block: BlockLike) -> tuple[BlockLike, int]:
        """把新块对齐到已写出的流前缀；对不上时从已写前缀之后重新开始。

        已永久写出的前缀是不可改写的历史，因此即使内容被整体替换，新内容
        也从该前缀之后开始，而不是回到逻辑行 0 去覆盖已在屏幕上的行。
        """
        released = self._released
        rows = block.render(self._width, self._palette, self._expanded)
        if released and len(rows) <= len(released) and rows == released[: len(rows)]:
            # 新内容没有超出已写前缀：不重画任何东西，只保留流位置。
            return RenderedBlock(block.kind, ()), len(released)
        if released and len(rows) > len(released) and rows[: len(released)] == released:
            split = block.split(
                len(rows) - len(released),
                width=self._width,
                palette=self._palette,
                expanded=self._expanded,
            )
            if split is not None:
                return split[1], len(released)
        return block, len(released)

    def append_pending_text(self, kind: str, delta: str) -> None:
        """把流式文本增量追加到可重写尾部块（渲染器维护累计文本）。"""
        text = self._pending_text + delta
        if self._pending is None:
            self.set_pending(Block(kind, text))
            self._pending_text = text
            return
        self._slots[self._pending].block = Block(kind, text)
        self._pending_text = text
        self._repaint_from = min(self._repaint_from, self._pending)

    def commit_pending(self) -> None:
        """把可重写尾部块并入永久文档。"""
        self._pending = None
        self._pending_text = ""
        self._released = []

    def set_width(self, width: int) -> None:
        """更新终端宽度；下一次绘制按新宽度重排可见区域。"""
        self._width = max(1, width)
        self.invalidate_visible()

    def set_height(self, height: int) -> None:
        """更新终端高度；变矮到光标之外时重置物理映射。"""
        updated = max(1, height)
        if self._drawn and self._cursor_view >= updated:
            self._reset_frame_state()
        self._height = updated

    def invalidate_from(self, index: int) -> None:
        """把重绘起点下移到指定块，用于较早块高度变化后的受控重排。"""
        if not self._slots:
            return
        self._repaint_from = min(self._repaint_from, max(0, index))

    def invalidate_visible(self) -> None:
        """重排所有「首行仍在屏幕上」的块。"""
        self.invalidate_from(self.visible_from_index())

    def visible_from_index(self) -> int:
        """最早一个首行仍属于屏幕映射的块索引。"""
        for row in self._view:
            stream, index = row.key
            if stream < 0:
                continue
            slot_index = self._slot_index(stream)
            if slot_index is not None and index == self._slots[slot_index].base:
                return slot_index
        return len(self._slots)

    def toggle_expanded(self) -> bool:
        """切换全局工具详情开关；返回切换后的状态。"""
        self._expanded = not self._expanded
        self.invalidate_visible()
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        """显式设置全局工具详情开关。"""
        if expanded != self._expanded:
            self._expanded = expanded
            self.invalidate_visible()

    def _slot_index(self, stream: int) -> int | None:
        for index, slot in enumerate(self._slots):
            if slot.key == stream:
                return index
        return None

    # -- chrome -------------------------------------------------------------

    def set_chrome(
        self,
        lines: list[str],
        *,
        cursor: tuple[int, int] | None,
        anchor: int | None = None,
    ) -> None:
        """设置底部重绘区与 chrome 内的光标坐标（行号为 chrome 内下标）。

        chrome 高于屏幕预算时按 ``anchor`` 取窗口：锚点行（交互面板当前选中
        行）必须留在屏幕上，绝不无声地从顶部丢掉它。未提供锚点时保持原有的
        「保留尾部」行为。
        """
        rows = list(lines)
        limit = max(1, self._height - 1)
        shift = 0
        if len(rows) > limit:
            focus = len(rows) - 1 if anchor is None else anchor
            focus = min(max(focus, 0), len(rows) - 1)
            shift = min(max(0, focus - limit + 1), len(rows) - limit)
            rows = rows[shift : shift + limit]
        self._chrome = rows
        if cursor is None:
            self._cursor = None
        else:
            row, column = cursor
            self._cursor = (max(0, row - shift), column)

    # -- 绘制 ---------------------------------------------------------------

    def repaint(self) -> None:
        """发送一次差分帧，显式区分三种情况。

        1. 内容变化：回到「第一个真正变化的行」（首帧则只向下开始），擦除其
           下方旧内容，写出新行，最后把光标放回 composer 单元格。
        2. 内容不变但目标光标移动（方向键 / Home / End / 上下移动）：走
           cursor-only 帧，只发送把物理光标移到目标单元格所需的相对定位序列，
           不写正文、不擦除、不重排，因此可见光标立即跟随逻辑编辑位置。
        3. 内容与光标都没变：真正的 no-op，不产生任何字节。

        返回起点永远发生在写出任何正文之前。
        """
        self._trim_pending()
        rows = self._fresh_rows()
        if not self._interactive:
            self._write_stream(rows)
            return
        if not rows:
            return
        start, pending_rows = self._diff(rows)
        row_index = len(rows) - len(pending_rows)
        target = self._cursor_target(rows)
        if not pending_rows and self._drawn:
            self._repaint_cursor_only(start, row_index, target)
            return
        column = max(0, self._cursor[1]) if self._cursor is not None else 0
        buffer = self._move_to(start)
        buffer.append("\r")
        buffer.append(_ERASE_TO_END)
        if pending_rows:
            buffer.append("\r\n".join(row.text for row in pending_rows))
        if target is not None:
            up = len(rows) - 1 - target
            buffer.append("\r")
            if up:
                buffer.append(f"\x1b[{up}A")
            if column:
                buffer.append(f"\x1b[{column}C")
        self._adopt(start, pending_rows, row_index, target)
        self._retire_frame()
        self._cursor_column = column if target is not None else None
        self._terminal.write("".join(buffer))

    def _repaint_cursor_only(
        self, start: int, row_index: int, target: int | None
    ) -> None:
        """文本未变化时只移动物理光标。

        目标单元格用与 :meth:`_adopt` 相同的坐标推导（``start + target -
        row_index``），因此 cursor-only 帧之后渲染器保存的物理光标行与真实
        终端一致，后续内容帧的回程计算不会被污染。
        """
        if target is None or self._cursor is None:
            return
        target_view = start + (target - row_index)
        column = max(0, self._cursor[1])
        if target_view == self._cursor_view and column == self._cursor_column:
            return
        # 先按**移动前**的物理光标行算出相对定位，再更新保存的位置。
        buffer = self._cursor_move(target_view, column)
        self._cursor_view = target_view
        self._cursor_column = column
        self._terminal.write("".join(buffer))

    def _cursor_move(self, target_view: int, column: int) -> list[str]:
        """把物理光标相对移动到目标单元格的控制序列。

        只包含回车、上下相对移动与横向相对移动：没有正文、没有 erase-to-end、
        不重写任何行，也不使用 ``\\r\\n``（在屏幕底部会滚动终端）。
        """
        delta = target_view - self._cursor_view
        buffer: list[str] = ["\r"]
        if delta < 0:
            buffer.append(f"\x1b[{min(-delta, self._height - 1)}A")
        elif delta > 0:
            buffer.append(f"\x1b[{min(delta, self._height - 1)}B")
        if column:
            buffer.append(f"\x1b[{column}C")
        return buffer

    def _fresh_rows(self) -> list[_Row]:
        """生成从重绘起点到 chrome 末尾的全部物理行。"""
        rows = self._layout_document(start=self._repaint_from)
        for offset, text in enumerate(self._chrome):
            rows.append(_Row((-1, offset), text))
        return rows

    def _diff(self, rows: list[_Row]) -> tuple[int, list[_Row]]:
        """返回（写入起点在屏幕映射中的下标, 仍需写出的行）。

        重绘起点所在块仍能在上一帧映射中找到时，从该块的首行开始按
        「身份 + 文本」求公共前缀，只重写变化尾部；若是全新内容，则
        从上一帧 chrome 的起点插入，chrome 顺移到新内容之后。
        """
        view = self._view
        if not view:
            return 0, list(rows)
        anchor = self._view_index(rows[0].key) if rows[0].key[0] >= 0 else None
        if anchor is None:
            chrome_index = self._view_index((-1, 0))
            if chrome_index is None:
                return len(view), list(rows)
            tail = view[chrome_index:]
            if _same_rows(tail, rows):
                return len(view), []
            return chrome_index, list(rows)
        kept = 0
        while (
            anchor + kept < len(view)
            and kept < len(rows)
            and view[anchor + kept].key == rows[kept].key
            and view[anchor + kept].text == rows[kept].text
        ):
            kept += 1
        return anchor + kept, rows[kept:]

    def _view_index(self, key: tuple[int, int]) -> int | None:
        """按逻辑身份查找物理行在屏幕映射中的下标。"""
        for index, row in enumerate(self._view):
            if row.key == key:
                return index
        return None

    def _cursor_target(self, rows: list[_Row]) -> int | None:
        """chrome 光标在待写行序列中的下标（0 起）；无光标时为 ``None``。"""
        chrome_indices = [index for index, row in enumerate(rows) if row.key[0] < 0]
        if self._cursor is None or not chrome_indices:
            return None
        row_index = min(max(self._cursor[0], 0), len(chrome_indices) - 1)
        return chrome_indices[row_index]

    def _move_to(self, start: int) -> list[str]:
        """返回把光标移到屏幕映射第 ``start`` 行的控制序列。"""
        buffer: list[str] = []
        if not self._drawn:
            # 首帧：只从当前光标位置向下写出，绝不向上移动或擦除启动前的内容。
            buffer.append("\r\n")
            return buffer
        up = self._cursor_view - start
        buffer.append("\r")
        if up > 0:
            buffer.append(f"\x1b[{min(up, self._height - 1)}A")
        elif up < 0:
            buffer.append("\r\n" * (-up))
        return buffer

    def _adopt(
        self,
        start: int,
        pending_rows: list[_Row],
        row_index: int,
        target: int | None,
    ) -> None:
        """把本帧写出的行并入屏幕映射，并更新光标位置。"""
        merged = self._view[:start] + list(pending_rows)
        dropped = max(0, len(merged) - self._height)
        self._view = merged[dropped:]
        if target is None:
            self._cursor_view = max(0, len(self._view) - 1)
        else:
            self._cursor_view = start + (target - row_index) - dropped
            self._cursor_view = min(max(0, self._cursor_view), len(self._view) - 1)
        self._drawn = True

    def _retire_frame(self) -> None:
        """一帧写出后缩小下一次的重绘范围。"""
        self._repaint_from = (
            self._pending if self._pending is not None else len(self._slots)
        )

    def _reset_frame_state(self) -> None:
        self._view = []
        self._cursor_view = 0
        self._cursor_column = None
        self._drawn = False

    def clear_chrome(self) -> None:
        """清除当前仍属于 Mica 的下部 chrome，保留正文与 scrollback。"""
        if not self._interactive:
            self._chrome = []
            self._cursor = None
            self._reset_frame_state()
            return
        start = self._view_index((-1, 0))
        if start is None:
            self._chrome = []
            self._cursor = None
            return
        buffer = ["\r"]
        up = self._cursor_view - start
        if self._drawn and up > 0:
            buffer.append(f"\x1b[{min(up, self._height - 1)}A")
        buffer.append("\r")
        buffer.append(_ERASE_TO_END)
        self._view = self._view[:start]
        self._cursor_view = len(self._view)
        self._cursor_column = 0
        self._chrome = []
        self._cursor = None
        self._terminal.write("".join(buffer))

    def _write_stream(self, rows: list[_Row]) -> None:
        """非交互输出：只追加文本，不发光标寻址序列。"""
        if not rows:
            return
        self._terminal.write("\r\n".join(row.text for row in rows) + "\r\n")
        self._adopt(0, list(rows), 0, None)
        self._cursor_column = None
        self._retire_frame()

    def _trim_pending(self) -> None:
        """把高于屏幕预算的可重写尾部切成「永久头部 + 更短的尾部」。

        这样唯一可重写块总是完全落在可寻址区域内：它的头部只写出一次，
        不会在后续帧里被重复重绘。切分后的两个片段共享同一逻辑流标识与
        连续的逻辑行号，因此屏幕映射中的旧行仍然是同一个身份。
        """
        pending = self._pending
        if pending is None:
            return
        slot = self._slots[pending]
        budget = max(1, self._height - len(self._chrome) - 1)
        rows = slot.block.render(self._width, self._palette, self._expanded)
        if len(rows) <= budget:
            return
        split = slot.block.split(
            budget,
            width=self._width,
            palette=self._palette,
            expanded=self._expanded,
        )
        if split is None:
            return
        head, tail = split
        head_rows = head.render(self._width, self._palette, self._expanded)
        self._released = [*self._released, *head_rows]
        self._slots[pending] = _Slot(tail, slot.key, slot.base + len(head_rows))
        self._slots.insert(pending, _Slot(head, slot.key, slot.base))
        self._pending = pending + 1
        if isinstance(tail, Block):
            self._pending_text = tail.text

    def __repr__(self) -> str:
        return f"<MainBufferRenderer rows={len(self._view)} slots={len(self._slots)}>"


__all__ = [
    "TAB_WIDTH",
    "Block",
    "BlockLike",
    "MainBufferRenderer",
    "Palette",
    "RenderedBlock",
    "cell_len",
    "char_cells",
    "expand_tabs",
    "safe_block_text",
    "strip_ansi",
    "truncate_head",
    "truncate_tail",
    "wrap_blocks",
    "wrap_line",
]
