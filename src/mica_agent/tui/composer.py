"""Mica TUI 的可编辑输入与提交边界。"""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding
from textual.events import Resize
from textual.message import Message
from textual.widgets import TextArea


class MicaComposer(TextArea):
    """支持软换行，并以消息提交原始输入快照。"""

    BINDINGS: ClassVar = [
        *TextArea.BINDINGS,
        Binding("enter", "submit", show=False, priority=True),
        Binding("shift+enter", "insert_newline", show=False, priority=True),
    ]

    class Submitted(Message):
        """一次有效 Composer 提交。"""

        __slots__ = ("_text",)

        def __init__(self, text: str) -> None:
            super().__init__()
            self._text = text

        @property
        def text(self) -> str:
            """提交时的原始文本快照。"""
            return self._text

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id, soft_wrap=True, show_line_numbers=False)

    def on_mount(self) -> None:
        super().on_mount()
        self._sync_max_height()
        self.focus()

    def on_resize(self, event: Resize) -> None:
        self._sync_max_height()

    def _sync_max_height(self) -> None:
        """按可用高度限制内容行，保证 transcript 至少一行可见。"""
        if self.app is None:
            return
        # 上部保留 work-status 一行和 transcript 至少一行可见区域。
        max_chrome_height = self.app.size.height - 2
        target = min(10, max(3, max_chrome_height))
        if self.styles.max_height != target:
            self.styles.max_height = target

    def action_submit(self) -> None:
        text = self.text
        self.clear()
        if text.strip():
            self.post_message(self.Submitted(text))

    def action_insert_newline(self) -> None:
        start, end = self.selection
        self.replace("\n", start, end, maintain_selection_offset=False)


__all__ = ["MicaComposer"]
