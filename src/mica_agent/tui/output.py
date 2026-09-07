"""TUI 模型输出视图的公共类型。"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar

from rich.cells import cell_len
from textual.app import ComposeResult
from textual.binding import Binding
from textual.content import Content
from textual.events import Resize
from textual.geometry import Region
from textual.widgets import Markdown, Static


class ReasoningDisplayMode(Enum):
    """思考正文的 TUI 显示模式。"""

    FULL = "full"
    COLLAPSED = "collapsed"
    HIDDEN = "hidden"


class AssistantResponseView(Static):
    """模型正式回复视图。"""

    DEFAULT_CSS = """
    AssistantResponseView {
        width: 100%;
        height: auto;
        padding: 0;
        margin-bottom: 1;
        position: relative;
        background: transparent;
        color: $foreground;
    }

    AssistantResponseView > Markdown {
        width: 100%;
        height: auto;
        padding: 0;
        background: transparent;
    }

    AssistantResponseView > .streaming-cursor {
        position: absolute;
        width: 1;
        height: 1;
        color: $mica-brand;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        self._text = ""
        self._is_streaming = True
        super().__init__(id=id, markup=False)

    @classmethod
    def from_history(cls, text: str, *, id: str | None = None) -> AssistantResponseView:
        """建立已完成且不显示流式光标的历史回复。"""
        if not isinstance(text, str):
            raise TypeError("text must be a str")
        view = cls(id=id)
        view._text = text
        view._is_streaming = False
        return view

    @property
    def text(self) -> str:
        """目前收到的完整正式回复原文。"""
        return self._text

    @property
    def is_streaming(self) -> bool:
        """该回复是否仍在接收增量。"""
        return self._is_streaming

    @property
    def markdown(self) -> Markdown:
        """承载正式回复源文本的 Textual Markdown 组件。"""
        return self.query_one(Markdown)

    def compose(self) -> ComposeResult:
        yield Markdown(self._text, classes="assistant-markdown")
        if self._is_streaming:
            yield Static("▌", classes="streaming-cursor", markup=False)

    async def append_text(self, text: str) -> None:
        """追加一个已经通过 Engine 顺序校验的文本增量。"""
        self._text += text
        await self.markdown.update(self._text)
        await self.wait_for_refresh()
        self._position_cursor()

    def on_resize(self, _: Resize) -> None:
        """随 Markdown 重排把光标留在最后一个可见内容单元。"""
        if self.query(".streaming-cursor"):
            self._position_cursor()
            self.call_after_refresh(self._position_cursor)

    async def finish(self, *, stable_markdown: bool) -> None:
        """结束流式状态，并按需执行最终 Markdown 渲染。"""
        if stable_markdown:
            await self.markdown.update(self._text)
        self._is_streaming = False
        cursors = self.query(".streaming-cursor")
        if cursors:
            await cursors.first().remove()

    def _position_cursor(self) -> None:
        cursors = self.query(".streaming-cursor")
        if not cursors:
            return
        cursor = cursors.first()
        best: tuple[int, int] | None = None
        for widget in self.markdown.query("*"):
            lines = widget.render_lines(
                Region(0, 0, widget.region.width, widget.region.height)
            )
            for row, line in enumerate(lines):
                visible = line.text.rstrip()
                if not visible.strip():
                    continue
                position = (
                    widget.region.y - self.region.y + row,
                    widget.region.x - self.region.x + cell_len(visible),
                )
                if best is None or position > best:
                    best = position
        if best is None:
            return
        top, left = best
        cursor.styles.offset = (
            min(max(left, 0), max(self.region.width - 1, 0)),
            min(max(top, 0), max(self.region.height - 1, 0)),
        )

    def __repr__(self) -> str:
        """不把模型正文放入调试表示。"""
        return "<AssistantResponseView>"


class ReasoningView(Static):
    """模型独立思考内容视图。"""

    can_focus = True
    BINDINGS: ClassVar = [Binding("ctrl+o", "toggle_expanded", show=False)]
    PREVIEW_CHAR_LIMIT = 96
    PREVIEW_SUFFIX = " …"

    DEFAULT_CSS = """
    ReasoningView {
        width: 100%;
        height: auto;
        max-height: 2;
        padding: 0 1 0 3;
        margin-bottom: 1;
        border-left: solid $mica-muted;
        background: transparent;
        color: $mica-muted;
        overflow-y: hidden;
    }

    ReasoningView.reasoning-expanded,
    ReasoningView.reasoning-full {
        max-height: 9999;
    }

    ReasoningView.reasoning-hidden {
        display: none;
    }
    """

    def __init__(
        self,
        text: str = "",
        *,
        mode: ReasoningDisplayMode = ReasoningDisplayMode.COLLAPSED,
        id: str | None = None,
    ) -> None:
        if not isinstance(text, str):
            raise TypeError("text must be a str")
        if not isinstance(mode, ReasoningDisplayMode):
            raise TypeError("mode must be a ReasoningDisplayMode")
        self._text = text
        self._mode = mode
        self._expanded = mode is ReasoningDisplayMode.FULL
        self._display_width = 0
        classes = {
            ReasoningDisplayMode.FULL: "reasoning-full",
            ReasoningDisplayMode.COLLAPSED: "",
            ReasoningDisplayMode.HIDDEN: "reasoning-hidden",
        }[mode]
        super().__init__(self._display_text(), markup=False, id=id, classes=classes)

    @property
    def text(self) -> str:
        """目前收到的完整思考原文。"""
        return self._text

    @property
    def mode(self) -> ReasoningDisplayMode:
        """该视图使用的思考显示模式。"""
        return self._mode

    @property
    def expanded(self) -> bool:
        """折叠模式下是否临时展开当前思考组件。"""
        return self._expanded

    def append_text(self, text: str) -> None:
        """追加一个已经通过 Engine 顺序校验的思考增量。"""
        self._text += text
        self._sync_display()

    def on_mount(self) -> None:
        self._refresh_display_width()
        self.call_after_refresh(self._refresh_display_width)

    def on_resize(self, _: Resize) -> None:
        self._refresh_display_width()
        self.call_after_refresh(self._refresh_display_width)

    def _refresh_display_width(self) -> None:
        width = self._available_width()
        if width != self._display_width:
            self._display_width = width
            if self._mode is ReasoningDisplayMode.COLLAPSED:
                self._sync_display()

    def _available_width(self) -> int:
        if self.content_region.width > 0:
            return self.content_region.width
        width = self.region.width - self.styles.padding.left - self.styles.padding.right
        if self.styles.border_left[0] != "none":
            width -= 1
        return max(width, 1)

    def action_toggle_expanded(self) -> None:
        """只切换当前聚焦的折叠思考视图。"""
        if self._mode is not ReasoningDisplayMode.COLLAPSED:
            return
        self._expanded = not self._expanded
        self.set_class(self._expanded, "reasoning-expanded")
        self._sync_display()

    def _sync_display(self) -> None:
        display_text = self._display_text()
        if display_text != self.content:
            self.update(display_text, layout=True)

    def _display_text(self) -> str:
        if self._mode is ReasoningDisplayMode.HIDDEN:
            return ""
        if self._mode is ReasoningDisplayMode.FULL or self._expanded:
            return self._text
        width = max(self._display_width, 1)
        lines = Content(self._text).wrap(width)
        if len(lines) <= 2:
            return self._text
        if len(lines) < 2:
            return self.PREVIEW_SUFFIX
        second_width = width - cell_len(self.PREVIEW_SUFFIX)
        second = Content(lines[1].plain).truncate(max(second_width, 0)).plain.rstrip()
        return f"{lines[0].plain}\n{second}{self.PREVIEW_SUFFIX}"

    def __repr__(self) -> str:
        """不把思考正文放入调试表示。"""
        return "<ReasoningView>"


__all__ = [
    "AssistantResponseView",
    "ReasoningDisplayMode",
    "ReasoningView",
]
