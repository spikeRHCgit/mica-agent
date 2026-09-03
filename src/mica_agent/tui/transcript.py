"""TUI 对话 transcript、历史记录与滚动跟随。"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import Click
from textual.widgets import Static

from mica_agent.tool_presentation import ToolPresentationSnapshot

from .composer import MicaComposer
from .theme import MUTED


class NewOutputIndicator(Static):
    """固定在 transcript 可视底部附近的新输出提示。"""

    DEFAULT_CSS = """
    NewOutputIndicator {
        position: absolute;
        dock: bottom;
        width: auto;
        height: 1;
        padding: 0 1;
        layer: overlay;
        color: $foreground;
        background: $surface;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("↓ new output", markup=False, id=id)

    def __repr__(self) -> str:
        return "<NewOutputIndicator>"


class ConversationTranscript(VerticalScroll):
    """拥有历史内容并协调用户离底、自动跟随和新输出提示。"""

    def __init__(self, *, id: str | None = None) -> None:
        self._following = True
        self._pending_new_output = False
        self._programmatic_scroll = False
        super().__init__(id=id)

    @property
    def is_following(self) -> bool:
        """用户是否仍在跟随最新输出。"""
        return self._following

    @property
    def has_pending_new_output(self) -> bool:
        """离底后是否有尚未查看的新可见内容。"""
        return self._pending_new_output

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.anchor(True)
        self.call_after_refresh(self._scroll_latest)

    def action_page_up(self) -> None:
        """用即时滚动完成真实 PageUp，并明确释放跟随锚点。"""
        self.scroll_page_up(animate=False)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self._programmatic_scroll:
            return
        at_bottom = new_value >= self.max_scroll_y - 0.01
        if at_bottom:
            self._following = True
            self._pending_new_output = False
            self._sync_indicator()
        elif round(new_value) != round(old_value):
            self._following = False
            self._sync_indicator()

    def content_added(self) -> None:
        """通知可见内容已追加，并按公开跟随状态处理滚动。"""
        if self._following:
            self.call_after_refresh(self._scroll_latest)
        else:
            self._pending_new_output = True
            self._sync_indicator()

    def content_resized(self) -> None:
        """Reconcile scroll position after an existing card changes height."""
        if self._following:
            self.call_after_refresh(self._scroll_latest)

    def return_to_latest(self) -> None:
        """公开动作：返回 transcript 底部并恢复自动跟随。"""
        self._following = True
        self._pending_new_output = False
        self._sync_indicator()
        self.call_after_refresh(self._scroll_latest)

    def _scroll_latest(self) -> None:
        self._programmatic_scroll = True
        try:
            self.scroll_end(animate=False, immediate=True, force=True)
        finally:
            self._programmatic_scroll = False
        self._following = True
        self._pending_new_output = False
        self._sync_indicator()

    def _sync_indicator(self) -> None:
        if not self.is_mounted:
            return
        existing = list(self.query(NewOutputIndicator))
        if self._pending_new_output:
            if not existing:
                self.mount(NewOutputIndicator(id="new-output"))
        else:
            for indicator in existing:
                indicator.remove()

    def on_click(self, event: Click) -> None:
        if isinstance(event.widget, NewOutputIndicator):
            self.return_to_latest()
            event.stop()

    def __repr__(self) -> str:
        return "<ConversationTranscript>"


class HistoricalToolRecordView(Static):
    """历史工具调用的一行安全兼容记录，不是正式工具卡片。"""

    DEFAULT_CSS = """
    HistoricalToolRecordView {
        width: 100%;
        height: 1;
        padding: 0 1 0 3;
        margin-bottom: 1;
        color: $mica-muted;
        background: transparent;
    }
    """

    _STATUSES = frozenset({"success", "failed", "recorded"})

    def __init__(
        self,
        tool_name: str,
        status: str,
        *,
        presentation: ToolPresentationSnapshot | None = None,
        id: str | None = None,
    ) -> None:
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise TypeError("tool_name must be a non-blank str")
        if status not in self._STATUSES:
            raise ValueError("status must be success, failed, or recorded")
        if presentation is not None and not isinstance(
            presentation, ToolPresentationSnapshot
        ):
            raise TypeError("presentation must be a ToolPresentationSnapshot or None")
        self._tool_name = tool_name
        self._status = status
        self._presentation = presentation
        super().__init__(id=id)

    @property
    def tool_name(self) -> str:
        """已经由会话契约验证的工具名称。"""
        return self._tool_name

    @property
    def status(self) -> str:
        """终态展示词，旧会话使用 recorded。"""
        return self._status

    @property
    def presentation(self) -> ToolPresentationSnapshot | None:
        """对应的不可变展示快照（旧会话为 None）。"""
        return self._presentation

    def render(self) -> Text:
        return Text(f"tool {self._tool_name} · {self._status}", style=MUTED)

    def __repr__(self) -> str:
        return "<HistoricalToolRecordView>"


class UserMessageCard(Static):
    """一条已提交用户消息的只读卡片。"""

    DEFAULT_CSS = """
    UserMessageCard {
        width: 100%;
        height: auto;
        padding: 0 1;
        background: $surface;
        color: $foreground;
        border-left: solid $mica-brand;
        margin-bottom: 1;
    }
    """

    def __init__(
        self, submitted: MicaComposer.Submitted, *, id: str | None = None
    ) -> None:
        self._submitted = submitted
        super().__init__(submitted.display_text, markup=False, id=id)

    @classmethod
    def from_text(cls, text: str, *, id: str | None = None) -> UserMessageCard:
        """从持久化用户原文建立只读历史卡片。"""
        if not isinstance(text, str):
            raise TypeError("text must be a str")
        return cls(MicaComposer.Submitted(text), id=id)

    @property
    def submitted(self) -> MicaComposer.Submitted:
        """保存的提交快照。"""
        return self._submitted

    def render(self) -> str:
        """纯文本显示，不解释为 Markdown。"""
        return self._submitted.display_text

    def __repr__(self) -> str:
        return "<UserMessageCard>"


__all__ = [
    "ConversationTranscript",
    "HistoricalToolRecordView",
    "NewOutputIndicator",
    "UserMessageCard",
]
