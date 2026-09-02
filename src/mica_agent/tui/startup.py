"""只读启动资源视图。"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from mica_agent.context_builder import AgentFileSource

from .models import StartupResourceSnapshot
from .theme import BRAND, MUTED

_SOURCE_LABELS = {
    AgentFileSource.GLOBAL: "global AGENTS.md",
    AgentFileSource.WORKSPACE: "workspace AGENTS.md",
}


class StartupResourcesView(Static):
    """只渲染外部快照中已经确认的启动事实。"""

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

    def render(self) -> Text:
        output = Text("Startup resources", style=f"bold {BRAND}")
        rows = (
            ("System prompt", self.snapshot.system_prompt_name or "none"),
            ("AGENTS.md", self._format_agent_sources()),
            ("Tools", self._format_names(self.snapshot.tool_names)),
            ("Skills", self._format_names(self.snapshot.skill_names)),
            ("Extensions", self._format_names(self.snapshot.extension_names)),
        )
        for label, value in rows:
            output.append("\n")
            output.append(f"{label:<15}", style=MUTED)
            output.append(value)
        return output

    def _format_agent_sources(self) -> str:
        if not self.snapshot.agent_sources:
            return "none"
        return ", ".join(
            _SOURCE_LABELS[source] for source in self.snapshot.agent_sources
        )

    @staticmethod
    def _format_names(names: tuple[str, ...]) -> str:
        return ", ".join(names) if names else "none"


__all__ = ["StartupResourcesView"]
