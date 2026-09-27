"""Safe, width-aware Markdown rendering for final model replies.

Mica owns only the safety boundary, theme mapping, and physical-row integration;
locked Rich handles parsing and layout.

* Untrusted source passes through the sanitization seam before Markdown parsing.
* Rendered segments are rebuilt from Rich ``Style`` values, allowing only structural
  attributes and foreground/background colors. This module never emits OSC 8 links or
  passes through control characters.
* Code blocks fill the width over a dark background, or use an equal-width ``│ `` rule
  under NO_COLOR so structure does not depend on color.
* Rendering never loads images, visits URLs, executes code, or changes cursor or
  terminal modes.
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

# Per-block source limit keeps one reply from making rendering unbounded.
MAX_SOURCE_CHARS = 120_000
# The stream cursor appears at the final row only while body text is still arriving.
STREAM_CURSOR = "▌"
# Equal-width code-block boundary used instead of background color under NO_COLOR.
CODE_BAR = "│ "
# Pygments theme name resolved by Rich; an unknown theme falls back to plain text.
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
    """Keep headings left-aligned with Mica's single reading axis."""

    LEVEL_ALIGN: ClassVar[dict[str, str]] = {}


class _MicaMarkdown(Markdown):
    """Markdown renderer using the Mica theme and left-aligned headings."""

    elements: ClassVar[dict[str, type]] = {
        **Markdown.elements,
        "heading_open": _LeftHeading,
    }


def stream_cursor_style() -> Style:
    """Style for the small blue streaming cursor."""
    return Style(color="#7dd3fc")


def render_markdown(source: str, width: int, palette: Palette) -> list[str]:
    """Render Markdown source into physical rows with trusted style sequences."""
    text = markdown_source(source)
    if not text.strip():
        return [""]
    segments = _render_segments(text, max(1, width))
    return [_paint_line(line, palette) for line in segments]


def render_markdown_preview(
    source: str, width: int, palette: Palette, *, streaming: bool
) -> list[str]:
    """Render a body preview, appending the blue cursor while ``streaming``."""
    text = markdown_source(source)
    if not text.strip():
        return [""]
    segments = _render_segments(text, max(1, width), cursor=streaming)
    return [_paint_line(line, palette) for line in segments]


def markdown_source(value: str) -> str:
    """Sanitize and bound untrusted Markdown source text."""
    if not isinstance(value, str):
        return ""
    cleaned = sanitize_text(value, newlines=True)
    if len(cleaned) > MAX_SOURCE_CHARS:
        cleaned = cleaned[:MAX_SOURCE_CHARS]
    return cleaned


def _render_segments(
    text: str, width: int, *, cursor: bool = False
) -> list[list[object]]:
    """Parse and lay out with Rich, returning segments grouped by physical row.

    Parser construction and rendering share one ordinary-exception boundary. Failure at
    either stage falls back to sanitized plain-text rows without exposing exception
    text; ``BaseException`` continues to propagate.
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
    except Exception:  # noqa: BLE001 - parser construction and rendering must degrade safely
        rows = [[Segment(row)] for row in _fallback_rows(text, width)]
    else:
        rows = [list(line) for line in lines]
    if cursor:
        rows = _append_cursor(rows, width)
    return rows


def _append_cursor(rows: list[list[object]], width: int) -> list[list[object]]:
    """Place the stream cursor after the body within the physical width budget.

    Append it to the final row when that row's visible width, measured as
    :func:`_paint_line` displays it, has room. Otherwise use a new physical row. Body
    text is never removed or truncated, the cursor appears once, and every returned
    string is one non-overwide physical row.
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
    """Safely degrade a Rich failure into sanitized plain-text physical rows.

    Split logical lines and hard-wrap to terminal width, so each string is one row no
    wider than ``width``. Tabs become spaces; no C0/C1 control, hyperlink, alternate
    screen, or mouse-capture sequence survives.
    """
    width = max(1, width)
    plain = expand_tabs(sanitize_text(text, newlines=True))
    rows: list[str] = []
    for row in wrap_line(plain, width):
        if cell_len(row) > width:
            # A wide character may exceed an extremely narrow width; honor the width contract.
            row = truncate_head(row, width)
        rows.append(row)
    return rows or [""]


def _paint_line(line: list[object], palette: Palette) -> str:
    """Convert one segment row into a physical row with controlled styling."""
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
        # Under NO_COLOR, replace meaningless background padding with an equal-width rule.
        text = truncate_head(CODE_BAR + _drop_prefix(text.rstrip(), 1), cell_len(text))
    return text


def _strip_trailing_spaces(segments: list[object]) -> list[object]:
    """Drop trailing unbacked spaces while preserving a final stream cursor."""
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
    """Return whether the entire row has code-block background, excluding inline code."""
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
    """Generate ANSI from a segment's parsed style, never a hyperlink sequence."""
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
    """Under NO_COLOR, preserve only structural attributes such as emphasis."""
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
    except Exception:  # noqa: BLE001 - ignore colors that cannot be resolved
        return ""
    return f"{prefix};2;{triplet.red};{triplet.green};{triplet.blue}"


def _drop_prefix(text: str, cells: int) -> str:
    """Drop terminal cells from the start when replacing code-block padding."""
    used = 0
    index = 0
    while index < len(text) and used < cells:
        used += cell_len(text[index])
        index += 1
    return text[index:]


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    """Final assistant reply that reflows safe source for width and palette."""

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
    """Incremental body renderer with an owned count of already-written prefix rows.

    The block owns its frozen prefix, so appending after a renderer split never rewrites
    rows already in scrollback. Incomplete fences and split Unicode chunks affect only
    the latest rendering and do not raise. Accumulated source never exceeds
    ``MAX_SOURCE_CHARS``.
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
        """Append a delta while bounding accumulated source by ``MAX_SOURCE_CHARS``.

        At the limit, source stays fixed at its first ``MAX_SOURCE_CHARS`` characters;
        further appends are deterministic no-ops and preserve rendering, cursor, and
        :meth:`finish` behavior.
        """
        chunk = markdown_source(delta)
        if not chunk or len(self._source) >= MAX_SOURCE_CHARS:
            return
        combined = self._source + chunk
        self._source = combined[:MAX_SOURCE_CHARS]

    def finish(self) -> None:
        """Finish streaming, remove the cursor, and stably render all accumulated source."""
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
