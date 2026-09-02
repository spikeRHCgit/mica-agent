"""Mica Textual TUI 的最小三段式外壳。"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from .logo import MicaLogo
from .models import StartupResourceSnapshot
from .startup import StartupResourcesView
from .theme import MICA_THEME, SHELL_CSS


class MicaTuiApp(App[None]):
    """不连接业务运行时的 Textual 空外壳。"""

    CSS = SHELL_CSS

    def __init__(
        self,
        *,
        ansi_color: bool | None = None,
        startup_resources: StartupResourceSnapshot | None = None,
    ) -> None:
        if startup_resources is not None and not isinstance(
            startup_resources, StartupResourceSnapshot
        ):
            raise TypeError(
                "startup_resources must be a StartupResourceSnapshot or None"
            )
        self.startup_resources = startup_resources or StartupResourceSnapshot()
        super().__init__(ansi_color=ansi_color)
        self.register_theme(MICA_THEME)
        self.theme = MICA_THEME.name

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="transcript"):
            yield MicaLogo(id="mica-logo")
            yield StartupResourcesView(
                self.startup_resources,
                id="startup-resources",
            )
        yield Static(id="work-status")
        yield Static(id="composer")


__all__ = ["MicaTuiApp"]
