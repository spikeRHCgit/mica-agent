"""安全、宽度感知的模型正式回复 Markdown 渲染。

职责边界很小：Mica 只负责**安全边界、主题映射与物理行集成**，解析与
排版交给锁文件中已有的 Rich（不新增依赖、不升级版本）。

* 不可信源文本先过清洗接缝，再交给 Markdown 解析器；
* 渲染结果按行转成物理行，样式序列由本模块按 Rich 解析出的 Style 重新
  生成——只允许加粗/斜体/下划线/暗淡与前景/背景色，**从不生成 OSC 8
  超链接**，也不透传任何控制字符；
* 代码块使用比终端背景稍亮的深灰背景并填满可用宽度；NO_COLOR 下改为
  等宽的 ``│ `` 边界，结构不依赖颜色；
* 不加载图片、不访问 URL、不执行代码、不改光标与终端模式。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import ClassVar

from rich.console import Console
from rich.markdown import Heading, Markdown
from rich.segment import Segment
from rich.style import Style
from rich.theme import Theme

from .renderer import (
    BlockLike,
    Palette,
    RenderedBlock,
    cell_len,
    expand_tabs,
    truncate_head,
    wrap_line,
)
from .sanitize import sanitize_text

# 单块 Markdown 源上限：避免超长回复让一次渲染无界。
MAX_SOURCE_CHARS = 120_000
# 流式光标：只在模型仍在输出正文时追加在最后一行末尾。
STREAM_CURSOR = "▌"
# NO_COLOR 下代码块的等宽边界（替代背景色）。
CODE_BAR = "│ "
# Pygments 代码主题名称（Rich 内置按名称解析；未知名称回退为纯文本）。
CODE_THEME = "one-dark"

_MARKDOWN_THEME = Theme(
    {
        "markdown.h1": Style(bold=True, color="#7dd3fc"),
        "markdown.h2": Style(bold=True, color="#7dd3fc"),
        "markdown.h3": Style(bold=True, color="#93c5fd"),
        "markdown.h4": Style(bold=True, color="#93c5fd"),
        "markdown.h5": Style(bold=True, color="#93c5fd"),
        "markdown.h6": Style(bold=True, color="#93c5fd"),
        "markdown.em": Style(italic=True, color="#e2e8f0"),
        "markdown.strong": Style(bold=True, color="#f8fafc"),
        "markdown.code": Style(color="#89b4fa"),
        "markdown.code_block": Style(color="#e2e8f0"),
        "markdown.block_quote": Style(color="#94a3b8"),
        "markdown.item": Style(color="#e2e8f0"),
        "markdown.item.bullet": Style(color="#7dd3fc", bold=True),
        "markdown.item.number": Style(color="#7dd3fc", bold=True),
        "markdown.link": Style(color="#7dd3fc", underline=True),
        "markdown.link_url": Style(color="#64748b"),
        "markdown.hr": Style(color="#334155"),
        "markdown.table.header": Style(bold=True, color="#7dd3fc"),
        "markdown.table.border": Style(color="#334155"),
        "markdown.kbd": Style(bold=True, color="#f8fafc"),
    },
    inherit=True,
)

_COLOURED_ATTRIBUTES = ("bold", "dim", "italic", "underline")


class _LeftHeading(Heading):
    """标题保持左对齐：Mica 的正文沿单一阅读主轴左对齐。"""

    LEVEL_ALIGN: ClassVar[dict[str, str]] = {}


class _MicaMarkdown(Markdown):
    """使用 Mica 主题与左对齐标题的 Markdown 渲染器。"""

    elements: ClassVar[dict[str, type]] = {
        **Markdown.elements,
        "heading_open": _LeftHeading,
    }


def stream_cursor_style() -> Style:
    """流式光标的样式（小型蓝色光标）。"""
    return Style(color="#7dd3fc")


def render_markdown(source: str, width: int, palette: Palette) -> list[str]:
    """把 Markdown 源渲染成物理行（自带可信样式序列）。"""
    text = markdown_source(source)
    if not text.strip():
        return [""]
    segments = _render_segments(text, max(1, width))
    return [_paint_line(line, palette) for line in segments]


def render_markdown_preview(
    source: str, width: int, palette: Palette, *, streaming: bool
) -> list[str]:
    """渲染正文预览；``streaming`` 时在末尾追加蓝色光标。"""
    text = markdown_source(source)
    if not text.strip():
        return [""]
    segments = _render_segments(text, max(1, width), cursor=streaming)
    return [_paint_line(line, palette) for line in segments]


def markdown_source(value: str) -> str:
    """清洗并限制不可信 Markdown 源文本。"""
    if not isinstance(value, str):
        return ""
    cleaned = sanitize_text(value, newlines=True)
    if len(cleaned) > MAX_SOURCE_CHARS:
        cleaned = cleaned[:MAX_SOURCE_CHARS]
    return cleaned


def _render_segments(
    text: str, width: int, *, cursor: bool = False
) -> list[list[object]]:
    """用 Rich 解析并排版，返回逐行的 Segment 列表。

    解析器构造与渲染处于同一个普通异常边界内：任一阶段抛普通异常都
    降级为已清洗纯文本物理行（不泄漏异常文本，`BaseException` 继续上抛）。
    """
    try:
        console = Console(
            width=width,
            file=io.StringIO(),
            force_terminal=True,
            color_system="truecolor",
            legacy_windows=False,
            emoji=False,
            highlight=False,
            soft_wrap=False,
            theme=_MARKDOWN_THEME,
        )
        options = console.options.update(width=width, height=None)
        markdown = _MicaMarkdown(
            text,
            code_theme=CODE_THEME,
            justify="left",
            hyperlinks=False,
            inline_code_lexer=None,
        )
        lines = console.render_lines(markdown, options, pad=False)
    except Exception:  # noqa: BLE001 - 解析器构造或渲染失败都必须降级
        rows = [[Segment(row)] for row in _fallback_rows(text, width)]
    else:
        rows = [list(line) for line in lines]
    if cursor:
        rows = _append_cursor(rows, width)
    return rows


def _append_cursor(rows: list[list[object]], width: int) -> list[list[object]]:
    """把流式光标放在正文末尾，并计入物理行宽度预算。

    末行按 :func:`_paint_line` 的实际可见宽度（非代码行不含行尾填充空白）
    还有空余列时直接追加；已占满 ``width`` 时另起一条物理行。正文不删
    不截，光标只出现一次，且无论哪条路径每个字符串都是一条不超宽的物理行。
    """
    cursor = Segment(STREAM_CURSOR, stream_cursor_style())
    if rows:
        last = rows[-1]
        visible = [segment for segment in last if getattr(segment, "text", "")]
        measured = (
            last
            if not visible or _is_code_row(visible)
            else _strip_trailing_spaces(list(last))
        )
        used = cell_len(
            "".join(str(getattr(segment, "text", "")) for segment in measured)
        )
        if used + 1 <= max(1, width):
            rows[-1] = [*last, cursor]
            return rows
    return [*rows, [cursor]]


def _fallback_rows(text: str, width: int) -> list[str]:
    """Rich 渲染失败后的安全降级：已清洗纯文本逐物理行输出。

    先按逻辑换行拆分，再按终端列宽硬换行，因此每个字符串都是一条独立
    物理行且不超过 ``width``；制表符展开为空格，绝不保留任何 C0/C1 控制
    内容，也不引入超链接、备用屏幕或鼠标捕获序列。
    """
    width = max(1, width)
    plain = expand_tabs(sanitize_text(text, newlines=True))
    rows: list[str] = []
    for row in wrap_line(plain, width):
        if cell_len(row) > width:
            # 单个宽字符可能超过极窄宽度：宽度契约优先。
            row = truncate_head(row, width)
        rows.append(row)
    return rows or [""]


def _paint_line(line: list[object], palette: Palette) -> str:
    """把一行 Segment 转成带受控样式序列的物理行。"""
    segments = [segment for segment in line if getattr(segment, "text", "")]
    if not segments:
        return ""
    code_row = _is_code_row(segments)
    if not code_row:
        segments = _strip_trailing_spaces(segments)
        if not segments:
            return ""
    text = "".join(_paint_segment(segment, palette) for segment in segments)
    if code_row and not palette.enabled:
        # NO_COLOR：去掉无意义的背景填充，改用等宽边界保持宽度不变。
        text = truncate_head(CODE_BAR + _drop_prefix(text.rstrip(), 1), cell_len(text))
    return text


def _strip_trailing_spaces(segments: list[object]) -> list[object]:
    """去掉行尾无背景的空白（光标行结尾保留光标本身）。"""
    body = list(segments)
    tail: list[object] = []
    if body and getattr(body[-1], "text", "") == STREAM_CURSOR:
        tail = [body.pop()]
    while body and not str(getattr(body[-1], "text", "")).strip():
        body.pop()
    if body:
        last = body[-1]
        text = str(getattr(last, "text", ""))
        body[-1] = Segment(text.rstrip(), getattr(last, "style", None))
    return [*body, *tail]


def _is_code_row(segments: list[object]) -> bool:
    """判断整行是否属于带背景的代码块（行内代码只覆盖局部，不算）。"""
    body = segments
    if body and getattr(body[-1], "text", "") == STREAM_CURSOR:
        body = body[:-1]
    visible = [segment for segment in body if str(getattr(segment, "text", "")).strip()]
    if not visible:
        return False
    return all(_style_of(segment).bgcolor is not None for segment in visible)


def _style_of(segment: object) -> Style:
    style = getattr(segment, "style", None)
    return style if isinstance(style, Style) else Style()


def _paint_segment(segment: object, palette: Palette) -> str:
    """按 Segment 的解析样式生成 ANSI；从不生成超链接序列。"""
    text = str(getattr(segment, "text", ""))
    if not text:
        return ""
    style = _style_of(segment)
    if not palette.enabled:
        return _wrap_attributes(text, style)
    codes = _sgr_codes(style)
    if not codes:
        return text
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


def _wrap_attributes(text: str, style: Style) -> str:
    """NO_COLOR：只保留加粗/暗淡/斜体/下划线等结构性属性。"""
    codes = [code for code in _attribute_codes(style)]
    if not codes:
        return text
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


def _attribute_codes(style: Style) -> list[str]:
    mapping = {"bold": "1", "dim": "2", "italic": "3", "underline": "4"}
    return [mapping[name] for name in _COLOURED_ATTRIBUTES if getattr(style, name)]


def _sgr_codes(style: Style) -> list[str]:
    codes = _attribute_codes(style)
    if style.color is not None:
        codes.append(_colour_code(style.color, background=False))
    if style.bgcolor is not None:
        codes.append(_colour_code(style.bgcolor, background=True))
    return codes


def _colour_code(colour: object, *, background: bool) -> str:
    prefix = "48" if background else "38"
    try:
        triplet = colour.get_truecolor()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 无法解析的颜色直接忽略
        return ""
    return f"{prefix};2;{triplet.red};{triplet.green};{triplet.blue}"


def _drop_prefix(text: str, cells: int) -> str:
    """按终端列宽从行首丢弃若干列（用于替换代码块的内边距）。"""
    used = 0
    index = 0
    while index < len(text) and used < cells:
        used += cell_len(text[index])
        index += 1
    return text[index:]


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    """已收口的正式回复：保存安全源文本，按宽度/调色板重新排版。"""

    source: str = field(repr=False)
    frozen: int = 0

    kind: str = "assistant"

    def __repr__(self) -> str:
        return f"<MarkdownBlock chars={len(self.source)} frozen={self.frozen}>"

    def render(self, width: int, palette: Palette, expanded: bool) -> list[str]:
        rows = render_markdown(self.source, width, palette)
        return rows[self.frozen :]

    def split(
        self, keep_tail: int, *, width: int, palette: Palette, expanded: bool
    ) -> tuple[BlockLike, BlockLike] | None:
        rows = self.render(width, palette, expanded)
        if keep_tail <= 0 or len(rows) <= keep_tail:
            return None
        head = RenderedBlock(self.kind, tuple(rows[: len(rows) - keep_tail]))
        tail = MarkdownBlock(self.source, self.frozen + len(rows) - keep_tail)
        return head, tail


class StreamingMarkdownBlock:
    """流式正文：按累计源文本渐进渲染，并自带已写出前缀计数。

    冻结前缀由本块自己维护（``frozen``），因此渲染器切分后继续追加增量
    不会重复写出已经进入 scrollback 的行；不完整的围栏与拆开的 Unicode
    增量都只影响最后一次渲染结果，不会抛出异常。累计源文本始终不超过
    ``MAX_SOURCE_CHARS``，因此一次超长回复不会带来无界增长。
    """

    kind = "assistant"

    def __init__(
        self, source: str = "", *, frozen: int = 0, streaming: bool = True
    ) -> None:
        self._source = markdown_source(source)
        self._frozen = frozen
        self._streaming = streaming

    @property
    def source(self) -> str:
        return self._source

    @property
    def frozen(self) -> int:
        return self._frozen

    @property
    def streaming(self) -> bool:
        return self._streaming

    def append(self, delta: str) -> None:
        """追加一段增量，并让完整累计源文本保持在 ``MAX_SOURCE_CHARS`` 内。

        到达上限后累计源固定为前 ``MAX_SOURCE_CHARS`` 个字符：后续追加
        不再改变内容（确定性且幂等），渲染、光标与 :meth:`finish` 行为不变。
        """
        chunk = markdown_source(delta)
        if not chunk or len(self._source) >= MAX_SOURCE_CHARS:
            return
        combined = self._source + chunk
        self._source = combined[:MAX_SOURCE_CHARS]

    def finish(self) -> None:
        """结束流式：去掉光标，用完整累计源文本做一次稳定渲染。"""
        self._streaming = False

    def render(self, width: int, palette: Palette, expanded: bool) -> list[str]:
        rows = render_markdown_preview(
            self._source, width, palette, streaming=self._streaming
        )
        return rows[self._frozen :]

    def split(
        self, keep_tail: int, *, width: int, palette: Palette, expanded: bool
    ) -> tuple[BlockLike, BlockLike] | None:
        rows = self.render(width, palette, expanded)
        if keep_tail <= 0 or len(rows) <= keep_tail:
            return None
        head = RenderedBlock(self.kind, tuple(rows[: len(rows) - keep_tail]))
        tail = StreamingMarkdownBlock(
            self._source,
            frozen=self._frozen + len(rows) - keep_tail,
            streaming=self._streaming,
        )
        return head, tail

    def __repr__(self) -> str:
        return f"<StreamingMarkdownBlock frozen={self._frozen}>"


__all__ = [
    "CODE_BAR",
    "CODE_THEME",
    "MAX_SOURCE_CHARS",
    "STREAM_CURSOR",
    "MarkdownBlock",
    "StreamingMarkdownBlock",
    "markdown_source",
    "render_markdown",
    "render_markdown_preview",
    "stream_cursor_style",
]
