"""TUI 内用户消息卡片的渲染。"""

from __future__ import annotations

from textual.widgets import Static

from .composer import MicaComposer


class UserMessageCard(Static):
    """一条已提交用户消息的只读卡片。

    卡片展示提交时保存的紧凑显示快照（``display_text``），长粘贴仍以
    ``[paste #N +M lines]`` 摘要显示；卡片不主动把内容解释为 Markdown。
    """

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

    @property
    def submitted(self) -> MicaComposer.Submitted:
        """保存的提交快照。"""
        return self._submitted

    def render(self) -> str:
        """纯文本显示，不解释为 Markdown。"""
        return self._submitted.display_text


__all__ = ["UserMessageCard"]
