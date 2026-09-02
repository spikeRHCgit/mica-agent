"""Mica Textual TUI 的最小三段式外壳。"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from .logo import MicaLogo
from .theme import MICA_THEME, SHELL_CSS


class MicaTuiApp(App[None]):
    """不连接业务运行时的 Textual 空外壳。"""

    CSS = SHELL_CSS

    def __init__(self, *, ansi_color: bool | None = None) -> None:
        super().__init__(ansi_color=ansi_color)
        self.register_theme(MICA_THEME)
        self.theme = MICA_THEME.name

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="transcript"):
            yield MicaLogo(id="mica-logo")
        yield Static(id="work-status")
        yield Static(id="composer")


__all__ = ["MicaTuiApp"]
