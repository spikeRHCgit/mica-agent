"""Mica Textual TUI 外壳与 Engine 事件投影。"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic

from textual.app import App, ComposeResult

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
from mica_agent.jsonl_store import StoredSession
from mica_agent.tool_contracts import ToolResultStatus
from mica_agent.tool_presentation import ToolPresentationKind

from .composer import MicaComposer
from .history import iter_history_views
from .logo import MicaLogo
from .models import StartupResourceSnapshot
from .output import AssistantResponseView, ReasoningDisplayMode, ReasoningView
from .startup import StartupResourcesView
from .status import WorkPhase, WorkStatus, WorkStatusView
from .theme import MICA_THEME, SHELL_CSS
from .tool_cards import (
    EditToolCard,
    ReadToolCard,
    ToolCard,
    ToolCardState,
    WriteToolCard,
)
from .transcript import (
    ConversationTranscript,
    UserMessageCard,
)


class MicaTuiApp(App[None]):
    """Textual 外壳及现有 Engine 公共事件的前端投影边界。"""

    CSS = SHELL_CSS

    def __init__(
        self,
        *,
        ansi_color: bool | None = None,
        startup_resources: StartupResourceSnapshot | None = None,
        reasoning_display_mode: ReasoningDisplayMode = ReasoningDisplayMode.COLLAPSED,
        clock: Callable[[], float] = monotonic,
        stored_session: StoredSession | None = None,
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
        if not callable(clock):
            raise TypeError("clock must be callable")
        if stored_session is not None and not isinstance(stored_session, StoredSession):
            raise TypeError("stored_session must be a StoredSession or None")
        self._clock = clock
        self.stored_session = stored_session
        self._last_seq: dict[str, int] = {}
        self._terminated_run_ids: set[str] = set()
        self._response_views: dict[str, AssistantResponseView] = {}
        self._reasoning_views: dict[str, ReasoningView] = {}
        self._tool_cards: dict[tuple[str, str], ToolCard] = {}
        self._active_status_run_id: str | None = None
        super().__init__(ansi_color=ansi_color)
        self.register_theme(MICA_THEME)
        self.theme = MICA_THEME.name

    def compose(self) -> ComposeResult:
        with ConversationTranscript(id="transcript"):
            yield MicaLogo(id="mica-logo")
            yield StartupResourcesView(
                self.startup_resources,
                id="startup-resources",
            )
            yield from iter_history_views(self.stored_session)
        yield WorkStatusView(
            clock=self._clock,
            auto_animate=self._clock is monotonic,
            id="work-status",
        )
        yield MicaComposer(id="composer")

    async def project_engine_event(self, event: EngineEvent) -> bool:
        """Project one public Engine event into this TUI."""
        if isinstance(event, RunStarted):
            if event.run_id in self._last_seq:
                return False
            self._last_seq[event.run_id] = event.seq
            self._active_status_run_id = event.run_id
            self.query_one(WorkStatusView).start_working()
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
        if event.run_id == self._active_status_run_id:
            if isinstance(event, ReasoningDelta):
                self.query_one(WorkStatusView).observe_progress(WorkPhase.THINKING)
            elif isinstance(event, TextDelta):
                self.query_one(WorkStatusView).observe_progress(WorkPhase.RESPONDING)
            elif isinstance(
                event,
                (
                    ToolPrepared,
                    ToolStarted,
                    ToolOutputDelta,
                    ToolFinished,
                    PermissionRequested,
                    UserInputRequested,
                ),
            ):
                self.query_one(WorkStatusView).observe_progress(WorkPhase.WAITING)
        if terminal:
            self._terminated_run_ids.add(event.run_id)
            if isinstance(event, (RunFailed, RunCancelled)):
                terminal_status = (
                    ToolResultStatus.CANCELLED
                    if isinstance(event, RunCancelled)
                    else ToolResultStatus.EXECUTION_FAILED
                )
                closed_card = False
                for (run_id, _call_id), card in tuple(self._tool_cards.items()):
                    if run_id == event.run_id and card.state in {
                        ToolCardState.PENDING,
                        ToolCardState.RUNNING,
                    }:
                        card.finish(terminal_status)
                        closed_card = True
                if closed_card:
                    self.query_one(ConversationTranscript).content_added()
            if event.run_id == self._active_status_run_id:
                status = (
                    WorkStatus.DONE
                    if isinstance(event, RunCompleted)
                    else WorkStatus.CANCEL
                    if isinstance(event, RunCancelled)
                    and not self.query_one(WorkStatusView).had_progress
                    else WorkStatus.INTERRUPT
                )
                self.query_one(WorkStatusView).finish(status)
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
            self.query_one(ConversationTranscript).content_added()
            return True
        if isinstance(event, ToolPrepared):
            card_type = {
                ToolPresentationKind.READ: ReadToolCard,
                ToolPresentationKind.WRITE: WriteToolCard,
                ToolPresentationKind.EDIT: EditToolCard,
            }.get(event.presentation.kind)
            if card_type is None or event.tool_name != event.presentation.tool_name:
                return False
            key = (event.run_id, event.call_id)
            if key in self._tool_cards:
                return False
            card = card_type(event.presentation)
            self._tool_cards[key] = card
            await self.query_one("#transcript").mount(card)
            await card.wait_for_refresh()
            self.query_one(ConversationTranscript).content_added()
            return True
        if isinstance(event, ToolStarted):
            card = self._tool_cards.get((event.run_id, event.call_id))
            if card is None or card.tool_name != event.tool_name:
                return False
            card.start()
            return True
        if isinstance(event, ToolFinished):
            card = self._tool_cards.get((event.run_id, event.call_id))
            if (
                card is None
                or card.tool_name != event.tool_name
                or (
                    event.presentation is not None
                    and event.presentation.kind is not card.snapshot.kind
                )
            ):
                return False
            card.finish(event.status, event.presentation)
            self.query_one(ConversationTranscript).content_added()
            return True
        if not isinstance(event, TextDelta):
            return False
        view = self._response_views.get(event.run_id)
        if view is None:
            view = AssistantResponseView()
            await self.query_one("#transcript").mount(view)
            self._response_views[event.run_id] = view
        await view.append_text(event.text)
        self.query_one(ConversationTranscript).content_added()
        return True

    def on_mica_composer_submitted(self, message: MicaComposer.Submitted) -> None:
        """收到有效提交后，在 transcript 中追加用户消息卡片。"""
        transcript = self.query_one(ConversationTranscript)
        transcript.mount(UserMessageCard(message))
        transcript.content_added()


__all__ = ["MicaTuiApp"]
