"""主缓冲区语义文档与差分帧渲染。

渲染器保存**未换行**的语义块，按当前终端宽度在绘制时换行；因此终端
resize 后可以重新排版。屏上内容分为三部分：

* 永久文档区：已经写出、不再重绘的语义块，会自然进入 scrollback。
* 重绘区：最近一次新写入的块与唯一可重写尾部块，每次绘制重新排版。
* chrome：工作状态、输入框和事实信息，始终位于屏幕最底部。

差分重绘只擦除「重绘区 + chrome」，从不擦除永久文档区，也从不使用
alternate screen、鼠标捕获或清除 scrollback 序列。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

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

DEFAULT_STYLES: dict[str, str] = {
    "raw": "",
    "logo": BRAND,
    "brand": BRAND_LIGHT,
    "muted": MUTED,
    "text": TEXT,
    "user": BRAND_LIGHT,
    "assistant": TEXT,
    "reasoning": MUTED,
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

    展开宽度与 :func:`cell_len` 对 ``\t`` 的度量共用 ``TAB_WIDTH``，
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


def wrap_blocks(blocks: list[Block], width: int) -> list[str]:
    """把语义块序列展开成终端行。"""
    lines: list[str] = []
    for block in blocks:
        if block.styled:
            lines.append(block.text)
            continue
        lines.extend(
            wrap_line(safe_block_text(block), width, continuation=block.continuation)
        )
    return lines


@dataclass(frozen=True, slots=True)
class Block:
    """一个未换行的语义块。

    ``styled`` 表示正文已经自带 ANSI 序列（例如逐字符着色的 Logo），
    渲染器不会再对它换行、测量或重新着色。
    """

    kind: str
    text: str
    continuation: str = ""
    styled: bool = False


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


class MainBufferRenderer:
    """语义文档 + 底部重绘区的差分渲染器。

    渲染器维护一份**上一帧的真实物理状态**：

    * ``_frame_rows``：上一帧实际写出的物理行数（重绘区 + chrome）；
    * ``_frame_cursor_row``：上一帧结束后光标在帧内的行号（0 起）；
    * ``_drawn``：是否已经画过至少一帧。

    不变式：**帧高不超过屏幕高**。超出预算的重绘行作为一次性永久输出
    写出（自然进入 scrollback），因此 ``_frame_cursor_row`` 总能精确回到
    上一帧顶部，新旧帧行数增减都不会遗留旧尾巴。
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
        self._blocks: list[Block] = []
        self._pending: int | None = None
        self._repaint_from = 0
        self._chrome: list[str] = []
        self._cursor: tuple[int, int] | None = None
        self._frame_rows = 0
        self._frame_region_rows = 0
        self._frame_offsets: dict[int, int] = {}
        self._frame_owned_from: int | None = None
        self._released_lines: list[str] = []
        self._frame_cursor_row = 0
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
    def drawn(self) -> bool:
        """是否已经画出过至少一帧（首帧后不再向上移动光标）。"""
        return self._drawn

    @property
    def frame_rows(self) -> int:
        """上一帧写出的物理行数。"""
        return self._frame_rows

    @property
    def frame_cursor_row(self) -> int:
        """上一帧结束后光标在帧内的行号（0 起）。"""
        return self._frame_cursor_row

    def frame(self) -> list[str]:
        """返回当前完整逻辑屏内容（文档 + chrome），用于测试与证据。"""
        return wrap_blocks(self._blocks, self._width) + list(self._chrome)

    def document_lines(self) -> list[str]:
        """返回文档区行（不含 chrome）。"""
        return wrap_blocks(self._blocks, self._width)

    # -- 文档编辑 -----------------------------------------------------------

    def add_block(
        self,
        kind: str,
        text: str,
        *,
        continuation: str = "",
        styled: bool = False,
    ) -> None:
        """追加一个永久语义块（例如用户消息或已收口的工具记录）。"""
        previous_pending = self._pending
        self._pending = None
        self._released_lines = []
        self._blocks.append(Block(kind, text, continuation, styled))
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
        """创建或更新唯一的可重写尾部块（例如流式回复）。"""
        if self._pending is None:
            self._blocks.append(Block(kind, text, continuation, styled))
            self._pending = len(self._blocks) - 1
            self._repaint_from = min(self._repaint_from, self._pending)
            self._released_lines = []
            return
        self._blocks[self._pending] = Block(kind, text, continuation, styled)

    def append(self, block: Block) -> None:
        """追加一个语义块对象。"""
        self.add_block(
            block.kind,
            block.text,
            continuation=block.continuation,
            styled=block.styled,
        )

    def set_pending(self, block: Block) -> None:
        """创建或更新唯一可重写尾部块。"""
        self.update_pending(
            block.kind,
            block.text,
            continuation=block.continuation,
            styled=block.styled,
        )

    def commit_pending(self) -> None:
        """把可重写尾部块并入永久文档（下一帧仍会完整重绘它）。"""
        self._pending = None

    def set_width(self, width: int) -> None:
        """更新终端宽度；下一次绘制会按新宽度重新排版重绘区。"""
        self._width = max(1, width)

    def set_height(self, height: int) -> None:
        """更新终端高度；变矮后不再假设还能精确回到上一帧顶部。"""
        updated = max(1, height)
        if self._drawn and self._frame_cursor_row >= updated:
            self._reset_frame_state()
        self._height = updated

    # -- chrome -------------------------------------------------------------

    def set_chrome(self, lines: list[str], *, cursor: tuple[int, int] | None) -> None:
        """设置底部重绘区与 chrome 内的光标坐标（行号为 chrome 内下标）。"""
        rows = list(lines)
        limit = max(1, self._height - 1)
        shift = 0
        if len(rows) > limit:
            shift = len(rows) - limit
            rows = rows[shift:]
        self._chrome = rows
        if cursor is None:
            self._cursor = None
        else:
            row, column = cursor
            self._cursor = (max(0, row - shift), column)

    # -- 绘制 ---------------------------------------------------------------

    def repaint(self) -> None:
        """发送一次差分帧。

        物理顺序是固定的：先依据上一帧的真实状态回到「本次仍需重绘的第一个
        块」的起点并擦除其下的旧尾部，再把超出屏幕预算的行作为永久输出写出，
        接着写出新帧，最后把光标放回 composer 单元格。返回起点永远发生在
        写出任何正文之前，因此正文绝不会从 composer 内部开始写。
        """
        self._trim_pending()
        region, offsets = self._region_layout()
        chrome = list(self._chrome)
        if not region and not chrome:
            return
        if not self._interactive:
            self._write_stream(region, chrome)
            return
        budget = max(0, self._height - len(chrome) - 1)
        overflow: list[str] = []
        if region and len(region) > budget:
            total = len(region)
            overflow = region[: total - budget]
            region = region[total - budget :]
            offsets = self._rebased_offsets(offsets, total, len(overflow))
        rows = region + chrome
        target = self._target_row(region, chrome)
        # 顶部已收口的行留在原位，因此新帧顶部下移：基准是「本次仍需重绘的
        # 第一个块」在上一帧中的起始行；该块在本帧被整体释放时，改用上一帧
        # 可重写部分的起点，否则旧 pending 行会残留在新正文上方。
        base = self._frame_offsets.get(self._repaint_from)
        if base is None:
            if self._frame_owned_from is not None:
                base = self._frame_owned_from
            else:
                base = self._frame_region_rows
        buffer: list[str] = []
        if self._drawn:
            up = min(max(0, self._frame_cursor_row - base), max(0, self._height - 1))
            buffer.append("\r")
            if up:
                buffer.append(f"\x1b[{up}A")
        else:
            # 首帧：只从当前光标位置向下写出，绝不向上移动或擦除启动前的内容。
            buffer.append("\r\n")
        buffer.append("\r")
        buffer.append(_ERASE_TO_END)
        if overflow:
            # 永久行从擦干净的动态区起点开始向下流，多余部分自然进入 scrollback。
            buffer.append("\r\n".join(overflow))
            buffer.append("\r\n")
        buffer.append("\r\n".join(rows))
        if target is None:
            cursor_row = len(rows) - 1
        else:
            cursor_row = target
            down = len(rows) - 1 - target
            buffer.append("\r")
            if down:
                buffer.append(f"\x1b[{down}A")
            column = max(0, self._cursor[1]) if self._cursor is not None else 0
            if column:
                buffer.append(f"\x1b[{column}C")
        self._frame_rows = len(rows)
        self._frame_region_rows = len(region)
        self._frame_offsets = offsets
        self._frame_owned_from = (
            offsets.get(self._pending) if self._pending is not None else None
        )
        self._frame_cursor_row = cursor_row
        self._drawn = True
        self._retire_frame()
        self._terminal.write("".join(buffer))

    def _rebased_offsets(
        self, offsets: dict[int, int], total: int, overflow_rows: int
    ) -> dict[int, int]:
        """永久写出顶部溢出行后，重建仍在重绘区内的块偏移。"""
        ordered = sorted(offsets.items())
        kept: dict[int, int] = {}
        for position, (index, start) in enumerate(ordered):
            end = ordered[position + 1][1] if position + 1 < len(ordered) else total
            if end > overflow_rows:
                kept[index] = max(0, start - overflow_rows)
        return kept

    def clear_chrome(self) -> None:
        """清除当前仍属于 Mica 的下部重绘区，保留正文与 scrollback。

        chrome 位于重绘区之下，因此只需从 chrome 首行擦到屏幕末尾；
        已经写出的对话正文（包括本帧的重绘区行）原样留在主缓冲区，
        光标落在其下方一条干净的新行行首。
        """
        if not self._interactive:
            self._chrome = []
            self._cursor = None
            self._reset_frame_state()
            return
        if not self._chrome and not self._drawn:
            return
        up = self._chrome_cursor_row()
        buffer = ["\r"]
        if self._drawn and self._chrome and up:
            buffer.append(f"\x1b[{up}A")
        buffer.append("\r")
        buffer.append(_ERASE_TO_END)
        self._chrome = []
        self._cursor = None
        self._reset_frame_state()
        self._terminal.write("".join(buffer))

    def _region_layout(self) -> tuple[list[str], dict[int, int]]:
        """返回重绘区行，以及每个块在帧内的起始行号。"""
        rows: list[str] = []
        offsets: dict[int, int] = {}
        for index in range(self._repaint_from, len(self._blocks)):
            block = self._blocks[index]
            offsets[index] = len(rows)
            if block.styled:
                rows.append(block.text)
                continue
            rows.extend(
                self._palette.paint(line, block.kind)
                for line in wrap_line(
                    safe_block_text(block),
                    self._width,
                    continuation=block.continuation,
                )
            )
        return rows, offsets

    def _region_lines(self) -> list[str]:
        return self._region_layout()[0]

    def _target_row(self, region: list[str], chrome: list[str]) -> int | None:
        """chrome 光标在整帧中的行号（0 起）；无光标时为 ``None``。"""
        if self._cursor is None or not chrome:
            return None
        row = min(max(self._cursor[0], 0), len(chrome) - 1)
        return min(len(region) + row, len(region) + len(chrome) - 1)

    def _chrome_cursor_row(self) -> int:
        """光标在 chrome 内的行号；没有显式光标时认为它在 chrome 末行。"""
        if not self._chrome:
            return 0
        if self._cursor is None:
            return len(self._chrome) - 1
        return min(max(self._cursor[0], 0), len(self._chrome) - 1)

    def _retire_frame(self) -> None:
        """一帧写出后缩小下一次的重绘范围。"""
        self._repaint_from = (
            self._pending if self._pending is not None else len(self._blocks)
        )

    def _reset_frame_state(self) -> None:
        self._frame_rows = 0
        self._frame_region_rows = 0
        self._frame_offsets = {}
        self._frame_owned_from = None
        self._frame_cursor_row = 0
        self._drawn = False

    def _write_stream(self, region: list[str], chrome: list[str]) -> None:
        """非交互输出：只追加文本，不发光标寻址序列。"""
        rows = region + chrome
        self._terminal.write("\r\n".join(rows) + "\r\n")
        self._retire_frame()

    def _trim_pending(self) -> None:
        """把超出屏高的旧尾部行转为永久文档，避免重绘区高于整屏。

        流式投影每次都传入累计全文，因此这里记住已经永久写出的前缀行：
        再次收到同一前缀时只处理新增部分，既不重复写出也不重复重绘。
        """
        pending = self._pending
        if pending is None:
            return
        budget = max(1, self._height - len(self._chrome) - 1)
        block = self._blocks[pending]
        if block.styled:
            return
        lines = wrap_line(
            safe_block_text(block), self._width, continuation=block.continuation
        )
        lines = self._drop_released(lines, pending, block)
        if len(lines) <= budget:
            return
        keep = lines[-budget:]
        dropped = lines[: len(lines) - budget]
        self._blocks[pending] = Block(
            block.kind, "\n".join(keep), block.continuation, block.styled
        )
        self._blocks.insert(
            pending,
            Block(block.kind, "\n".join(dropped), block.continuation, block.styled),
        )
        self._pending = pending + 1
        self._released_lines = [*self._released_lines, *dropped]

    def _drop_released(self, lines: list[str], pending: int, block: Block) -> list[str]:
        """去掉已经永久写出的前缀行；调用方换了新文本时重新开始累计。"""
        released = self._released_lines
        if not released:
            return lines
        if len(lines) < len(released) or lines[: len(released)] != released:
            self._released_lines = []
            return lines
        tail = lines[len(released) :]
        self._blocks[pending] = Block(
            block.kind, "\n".join(tail), block.continuation, block.styled
        )
        return tail


__all__ = [
    "TAB_WIDTH",
    "Block",
    "MainBufferRenderer",
    "Palette",
    "cell_len",
    "char_cells",
    "expand_tabs",
    "safe_block_text",
    "truncate_head",
    "truncate_tail",
    "wrap_blocks",
    "wrap_line",
]
