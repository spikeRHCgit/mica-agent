"""Mica Textual TUI 外壳与 Engine 事件投影。"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from mica_agent.events import (
    EngineEvent,
    PermissionRequested,
    ReasoningDelta,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
    ToolFinished,
    ToolOutputDelta,
    ToolPrepared,
    ToolStarted,
    UserInputRequested,
)

from .composer import MicaComposer
from .logo import MicaLogo
from .models import StartupResourceSnapshot
from .output import AssistantResponseView, ReasoningDisplayMode, ReasoningView
from .startup import StartupResourcesView
from .theme import MICA_THEME, SHELL_CSS
from .transcript import UserMessageCard


class MicaTuiApp(App[None]):
    """Textual 外壳及现有 Engine 公共事件的前端投影边界。"""

    CSS = SHELL_CSS

    def __init__(
        self,
        *,
        ansi_color: bool | None = None,
        startup_resources: StartupResourceSnapshot | None = None,
        reasoning_display_mode: ReasoningDisplayMode = ReasoningDisplayMode.COLLAPSED,
    ) -> None:
        if startup_resources is not None and not isinstance(
            startup_resources, StartupResourceSnapshot
        ):
            raise TypeError(
                "startup_resources must be a StartupResourceSnapshot or None"
            )
        self.startup_resources = startup_resources or StartupResourceSnapshot()
        if not isinstance(reasoning_display_mode, ReasoningDisplayMode):
            raise TypeError("reasoning_display_mode must be a ReasoningDisplayMode")
        self.reasoning_display_mode = reasoning_display_mode
        self._last_seq: dict[str, int] = {}
        self._terminated_run_ids: set[str] = set()
        self._response_views: dict[str, AssistantResponseView] = {}
        self._reasoning_views: dict[str, ReasoningView] = {}
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
        yield MicaComposer(id="composer")

    async def project_engine_event(self, event: EngineEvent) -> bool:
        """Project one public Engine event into this TUI."""
        if isinstance(event, RunStarted):
            if event.run_id in self._last_seq:
                return False
            self._last_seq[event.run_id] = event.seq
            return True
        terminal = isinstance(event, (RunCompleted, RunFailed, RunCancelled))
        known_event = isinstance(
            event,
            (
                PermissionRequested,
                ReasoningDelta,
                TextDelta,
                ToolFinished,
                ToolOutputDelta,
                ToolPrepared,
                ToolStarted,
                UserInputRequested,
                RunCompleted,
                RunFailed,
                RunCancelled,
            ),
        )
        if not known_event:
            return False
        if isinstance(event, TextDelta) and not isinstance(event.text, str):
            return False
        if event.run_id in self._terminated_run_ids:
            return False
        last_seq = self._last_seq.get(event.run_id)
        if last_seq is None or event.seq <= last_seq:
            return False
        self._last_seq[event.run_id] = event.seq
        if terminal:
            self._terminated_run_ids.add(event.run_id)
            view = self._response_views.get(event.run_id)
            if view is not None:
                await view.finish(stable_markdown=isinstance(event, RunCompleted))
            return True
        if isinstance(event, ReasoningDelta):
            if self.reasoning_display_mode is ReasoningDisplayMode.HIDDEN:
                return True
            view = self._reasoning_views.get(event.run_id)
            if view is None:
                view = ReasoningView(
                    event.text,
                    mode=self.reasoning_display_mode,
                )
                await self.query_one("#transcript").mount(view)
                await view.wait_for_refresh()
                self._reasoning_views[event.run_id] = view
            else:
                view.append_text(event.text)
            return True
        if not isinstance(event, TextDelta):
            return False
        view = self._response_views.get(event.run_id)
        if view is None:
            view = AssistantResponseView()
            await self.query_one("#transcript").mount(view)
            self._response_views[event.run_id] = view
        await view.append_text(event.text)
        return True

    def on_mica_composer_submitted(self, message: MicaComposer.Submitted) -> None:
        """收到有效提交后，在 transcript 中追加用户消息卡片。"""
        transcript = self.query_one("#transcript")
        transcript.mount(UserMessageCard(message))


__all__ = ["MicaTuiApp"]
