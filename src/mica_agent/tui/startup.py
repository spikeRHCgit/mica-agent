"""启动首屏的只读头部、资源面板和就绪提示。"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from .models import StartupResourceSnapshot
from .theme import BRAND_LIGHT, MUTED


class StartupIntroView(Static):
    """Logo 下方的启动定位文案。"""

    DEFAULT_CSS = """
    StartupIntroView {
        width: 100%;
        height: auto;
        margin-bottom: 1;
        padding-left: 1;
    }
    """

    def render(self) -> Text:
        if self.app.size.width < 48 or self.app.size.height <= 16:
            return Text("Ready to build.", style=f"bold {BRAND_LIGHT}")
        output = Text("Mica — lightweight AI coding agent")
        output.append("\nReady to build.", style=f"bold {BRAND_LIGHT}")
        return output


class StartupResourcesView(Static):
    """只渲染外部快照中已经确认的启动事实。"""

    DEFAULT_CSS = """
    StartupResourcesView {
        width: 68;
        max-width: 100%;
        min-width: 20;
        height: auto;
        margin-bottom: 0;
        padding: 0 1;
        border: solid $mica-border;
        border-title-color: $mica-muted;
        border-title-background: $background;
        border-title-style: bold;
        color: $foreground;
    }
    """

    def __init__(
        self,
        snapshot: StartupResourceSnapshot,
        *,
        id: str | None = None,
    ) -> None:
        if not isinstance(snapshot, StartupResourceSnapshot):
            raise TypeError("snapshot must be a StartupResourceSnapshot")
        super().__init__(id=id)
        self.snapshot = snapshot
        self.border_title = " Context "

    def render(self) -> Text:
        output = Text()
        compact = self.app.size.height <= 16
        self._line(output, "System prompt", self.snapshot.system_prompt_name or "none")
        self._line(
            output,
            "Global AGENTS.md",
            self.snapshot.global_agent_path or "none",
        )
        self._line(
            output,
            "Workspace AGENTS.md",
            self.snapshot.workspace_agent_path or "none",
        )
        if compact:
            return output
        self._section(output, "Skills")
        output.append("\n" + self._format_names(self.snapshot.skill_names))
        self._section(output, "Extensions")
        output.append("\n" + self._format_names(self.snapshot.extension_names))
        self._section(output, "Tools")
        output.append(
            "\n"
            f"{len(self.snapshot.tool_names)} available: "
            f"{self._format_names(self.snapshot.tool_names)}"
        )
        return output

    @staticmethod
    def _section(output: Text, title: str) -> None:
        if output:
            output.append("\n")
        output.append(title, style=MUTED)
        output.append(" ", style=MUTED)
        output.append("─" * 8, style=MUTED)

    @staticmethod
    def _line(output: Text, label: str, value: str) -> None:
        if output:
            output.append("\n")
        output.append("  ")
        output.append(f"{label}: ", style=MUTED)
        output.append(value)

    @staticmethod
    def _format_names(names: tuple[str, ...]) -> str:
        return ", ".join(names) if names else "none"


class StartupReadyView(Static):
    """不进入模型上下文或会话历史的启动提示。"""

    DEFAULT_CSS = """
    StartupReadyView {
        width: 100%;
        height: auto;
        margin-bottom: 1;
        padding: 0 1;
    }
    """

    def render(self) -> Text:
        output = Text("✓ Mica is ready.", style=f"bold {BRAND_LIGHT}")
        output.append("  Type your request below or run a command.", style=MUTED)
        return output


__all__ = ["StartupIntroView", "StartupReadyView", "StartupResourcesView"]
