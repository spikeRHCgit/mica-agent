"""Mica Textual TUI 外壳与 Engine 事件投影。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
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
from mica_agent.interactions import PermissionResponse
from mica_agent.jsonl_store import StoredSession
from mica_agent.tool_contracts import ToolResultStatus
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
)

from .composer import MicaComposer
from .history import iter_history_views
from .interactions import (
    AskInteractionView,
    RunCancelRequester,
    UserInputResponder,
    _call_sync,
    _is_async_callable,
)
from .logo import MicaLogo
from .models import StartupResourceSnapshot
from .output import AssistantResponseView, ReasoningDisplayMode, ReasoningView
from .permissions import (
    PermissionInteractionView,
    PermissionResponder,
)
from .startup import StartupResourcesView
from .status import WorkPhase, WorkStatus, WorkStatusView
from .theme import MICA_THEME, SHELL_CSS
from .tool_cards import (
    BashToolCard,
    ToolCard,
    ToolCardFactory,
    ToolCardState,
    copy_tool_card_factories,
    create_tool_card,
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
        tool_card_factories: Mapping[str, ToolCardFactory] | None = None,
        user_input_responder: UserInputResponder | None = None,
        run_cancel_requester: RunCancelRequester | None = None,
        permission_responder: PermissionResponder | None = None,
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
        if user_input_responder is not None and (
            not callable(user_input_responder)
            or _is_async_callable(user_input_responder)
        ):
            raise TypeError(
                "user_input_responder must be a synchronous callable or None"
            )
        if run_cancel_requester is not None and (
            not callable(run_cancel_requester)
            or _is_async_callable(run_cancel_requester)
        ):
            raise TypeError(
                "run_cancel_requester must be a synchronous callable or None"
            )
        if permission_responder is not None and (
            not callable(permission_responder)
            or _is_async_callable(permission_responder)
        ):
            raise TypeError(
                "permission_responder must be a synchronous callable or None"
            )
        self._clock = clock
        self.stored_session = stored_session
        self._tool_card_factories = copy_tool_card_factories(tool_card_factories)
        self._last_seq: dict[str, int] = {}
        self._terminated_run_ids: set[str] = set()
        self._response_views: dict[str, AssistantResponseView] = {}
        self._reasoning_views: dict[str, ReasoningView] = {}
        self._tool_cards: dict[tuple[str, str], ToolCard] = {}
        self._prepared_presentations: dict[
            tuple[str, str], ToolPresentationSnapshot
        ] = {}
        self._permission_views: dict[tuple[str, str], PermissionInteractionView] = {}
        self._permission_interactions: dict[str, tuple[str, str]] = {}
        self._permission_response_ids: set[str] = set()
        self._completed_permission_interactions: set[str] = set()
        self._active_permission_key: tuple[str, str] | None = None
        self._ask_views: dict[tuple[str, str], AskInteractionView] = {}
        self._ask_calls: dict[tuple[str, str], AskInteractionView] = {}
        self._pending_ask_calls: dict[str, str] = {}
        self._user_input_responder = user_input_responder
        self._run_cancel_requester = run_cancel_requester
        self._permission_responder = permission_responder
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
            yield from iter_history_views(
                self.stored_session,
                tool_card_factories=self._tool_card_factories,
            )
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
                closed_ask = False
                for (run_id, _interaction_id), view in tuple(self._ask_views.items()):
                    if run_id == event.run_id and view.is_active:
                        view.finish(terminal_status)
                        closed_ask = True
                if closed_card or closed_ask:
                    self.query_one(ConversationTranscript).content_added()
                self._pending_ask_calls.pop(event.run_id, None)
                self._clear_permission_interactions_for_run(event.run_id)
                self._restore_composer_if_possible()
            else:
                self._clear_permission_interactions_for_run(event.run_id)
                self._restore_composer_if_possible()
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
            if event.tool_name != event.presentation.tool_name:
                return False
            key = (event.run_id, event.call_id)
            if key in self._prepared_presentations:
                return False
            if event.presentation.kind is ToolPresentationKind.ASK:
                if key in self._ask_calls or event.run_id in self._pending_ask_calls:
                    return False
                self._prepared_presentations[key] = event.presentation
                self._pending_ask_calls[event.run_id] = event.call_id
                return True
            if key in self._tool_cards:
                return False
            card = create_tool_card(
                event.presentation,
                tool_card_factories=self._tool_card_factories,
            )
            if card is None:
                return False
            self._prepared_presentations[key] = event.presentation
            self._tool_cards[key] = card
            await self.query_one("#transcript").mount(card)
            await card.wait_for_refresh()
            self.query_one(ConversationTranscript).content_added()
            return True
        if isinstance(event, ToolStarted):
            if event.tool_name == "ask":
                accepted = self._pending_ask_calls.get(
                    event.run_id
                ) == event.call_id or ((event.run_id, event.call_id) in self._ask_calls)
                if accepted:
                    self._clear_permission_for_call(
                        event.run_id, event.call_id, event.tool_name
                    )
                return accepted
            card = self._tool_cards.get((event.run_id, event.call_id))
            if card is None or card.tool_name != event.tool_name:
                return False
            card.start()
            self._clear_permission_for_call(
                event.run_id, event.call_id, event.tool_name
            )
            return True
        if isinstance(event, ToolOutputDelta):
            card = self._tool_cards.get((event.run_id, event.call_id))
            if (
                not isinstance(card, BashToolCard)
                or event.tool_name != "bash"
                or card.state is not ToolCardState.RUNNING
            ):
                return False
            card.append_output(event.text)
            self.query_one(ConversationTranscript).content_added()
            return True
        if isinstance(event, PermissionRequested):
            key = (event.run_id, event.call_id)
            if self._active_permission_key is not None:
                return False
            if self._has_active_ask():
                return False
            if (
                event.interaction_id in self._completed_permission_interactions
                or event.interaction_id in self._permission_interactions
            ):
                return False
            presentation = self._prepared_presentations.get(key)
            if presentation is None or presentation.tool_name != event.tool_name:
                return False
            if presentation.kind is ToolPresentationKind.ASK:
                if self._pending_ask_calls.get(event.run_id) != event.call_id:
                    return False
            else:
                card = self._tool_cards.get(key)
                if (
                    card is None
                    or card.tool_name != event.tool_name
                    or card.state not in {ToolCardState.PENDING, ToolCardState.RUNNING}
                ):
                    return False
            view = PermissionInteractionView(
                run_id=event.run_id,
                interaction_id=event.interaction_id,
                presentation=presentation,
                permission_responder=self._respond_permission,
                run_cancel_requester=self._request_permission_cancel,
                on_accepted=self._on_permission_accepted,
            )
            self._active_permission_key = key
            self._permission_interactions[event.interaction_id] = key
            self._permission_views[key] = view
            await self.screen.mount(view, before="#work-status")
            await view.wait_for_refresh()
            self._set_composer_disabled(True)
            view.focus()
            self.query_one(ConversationTranscript).content_added()
            return False
        if isinstance(event, UserInputRequested):
            if self._active_permission_key is not None or any(
                view.is_active for view in self._ask_views.values()
            ):
                return False
            prompt = event.prompt
            if prompt is None:
                return False
            view = AskInteractionView(
                run_id=event.run_id,
                interaction_id=event.interaction_id,
                prompt=prompt,
                user_input_responder=self._user_input_responder,
                run_cancel_requester=self._run_cancel_requester,
            )
            self._ask_views[(event.run_id, event.interaction_id)] = view
            call_id = self._pending_ask_calls.get(event.run_id)
            if call_id is not None:
                self._ask_calls[(event.run_id, call_id)] = view
                self._pending_ask_calls.pop(event.run_id, None)
            await self.query_one("#transcript").mount(view)
            await view.wait_for_refresh()
            self._set_composer_disabled(True)
            view.focus_input()
            self.query_one(ConversationTranscript).content_added()
            return False
        if isinstance(event, ToolFinished):
            if event.tool_name == "ask":
                if (
                    event.presentation is not None
                    and event.presentation.kind is not ToolPresentationKind.ASK
                ):
                    return False
                view = self._ask_calls.get((event.run_id, event.call_id))
                known_run_ask_calls = {
                    call_id
                    for (run_id, call_id) in self._ask_calls
                    if run_id == event.run_id
                }
                if known_run_ask_calls and event.call_id not in known_run_ask_calls:
                    return False
                expected_call_id = self._pending_ask_calls.get(event.run_id)
                if expected_call_id is not None and expected_call_id != event.call_id:
                    return False
                if view is None:
                    active = [
                        candidate
                        for (
                            run_id,
                            _interaction_id,
                        ), candidate in self._ask_views.items()
                        if run_id == event.run_id and candidate.is_active
                    ]
                    if len(active) == 1:
                        view = active[0]
                if view is None:
                    return False
                view.finish(event.status, event.presentation)
                self._prepared_presentations.pop((event.run_id, event.call_id), None)
                self._clear_permission_for_call(
                    event.run_id, event.call_id, event.tool_name
                )
                self.query_one(ConversationTranscript).content_added()
                self._restore_composer_if_possible()
                return True
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
            self._prepared_presentations.pop((event.run_id, event.call_id), None)
            self._clear_permission_for_call(
                event.run_id, event.call_id, event.tool_name
            )
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

    def _has_active_ask(self) -> bool:
        return bool(self._pending_ask_calls) or any(
            view.is_active for view in self._ask_views.values()
        )

    def _respond_permission(self, response: PermissionResponse) -> bool:
        if not isinstance(response, PermissionResponse):
            return False
        key = self._permission_interactions.get(response.interaction_id)
        if (
            key is None
            or key != self._active_permission_key
            or response.interaction_id in self._permission_response_ids
            or key[0] in self._terminated_run_ids
        ):
            return False
        accepted = _call_sync(self._permission_responder, response)
        if accepted:
            self._permission_response_ids.add(response.interaction_id)
        return accepted

    def _request_permission_cancel(self, run_id: str) -> bool:
        key = self._active_permission_key
        if (
            key is None
            or key[0] != run_id
            or self._permission_views.get(key) is None
            or run_id in self._terminated_run_ids
        ):
            return False
        return _call_sync(self._run_cancel_requester, run_id)

    def _on_permission_accepted(self, view: PermissionInteractionView) -> bool:
        key = (view.run_id, view.presentation.call_id)
        if self._permission_views.get(key) is not view:
            return True
        self._permission_views.pop(key, None)
        interaction_id = next(
            (
                interaction_id
                for interaction_id, interaction_key in self._permission_interactions.items()
                if interaction_key == key
            ),
            None,
        )
        if interaction_id is not None:
            self._permission_interactions.pop(interaction_id, None)
            self._completed_permission_interactions.add(interaction_id)
        self._active_permission_key = None
        view.deactivate()
        if view.parent is not None:
            view.remove()
        self._restore_composer_if_possible()
        return True

    def _clear_permission_for_call(
        self, run_id: str, call_id: str, tool_name: str
    ) -> None:
        key = (run_id, call_id)
        presentation = self._prepared_presentations.get(key)
        if presentation is not None and presentation.tool_name != tool_name:
            return
        interaction_id = next(
            (
                interaction_id
                for interaction_id, interaction_key in self._permission_interactions.items()
                if interaction_key == key
            ),
            None,
        )
        view = self._permission_views.get(key)
        if interaction_id is None and view is None:
            self._prepared_presentations.pop(key, None)
            if self._active_permission_key == key:
                self._active_permission_key = None
            return
        if interaction_id is not None:
            self._permission_interactions.pop(interaction_id, None)
            self._permission_response_ids.discard(interaction_id)
            self._completed_permission_interactions.add(interaction_id)
        view = self._permission_views.pop(key, None)
        if view is not None:
            view.deactivate()
            if view.parent is not None:
                view.remove()
        if self._active_permission_key == key:
            self._active_permission_key = None
        self._prepared_presentations.pop(key, None)
        self._restore_composer_if_possible()

    def _clear_permission_interactions_for_run(self, run_id: str) -> None:
        keys = {
            key for key in self._permission_interactions.values() if key[0] == run_id
        }
        for key in keys:
            self._clear_permission_for_call(
                key[0],
                key[1],
                self._prepared_presentations.get(key).tool_name
                if self._prepared_presentations.get(key) is not None
                else "__unknown__",
            )
        for key in tuple(self._prepared_presentations):
            if key[0] == run_id:
                self._prepared_presentations.pop(key, None)

    def exit(
        self,
        result=None,
        return_code: int = 0,
        message=None,
    ) -> None:
        for view in tuple(self._permission_views.values()):
            view.deactivate()
            if view.parent is not None:
                view.remove()
        self._permission_views.clear()
        self._permission_interactions.clear()
        self._permission_response_ids.clear()
        self._prepared_presentations.clear()
        self._active_permission_key = None
        super().exit(result, return_code, message)

    def _set_composer_disabled(self, disabled: bool) -> None:
        composer = self.query_one(MicaComposer)
        composer.disabled = disabled

    def _restore_composer_if_possible(self) -> None:
        if self._has_active_ask() or any(
            view.is_active for view in self._permission_views.values()
        ):
            self._set_composer_disabled(True)
            return
        composer = self.query_one(MicaComposer)
        composer.disabled = False
        composer.focus()

    def on_mica_composer_submitted(self, message: MicaComposer.Submitted) -> None:
        """收到有效提交后，在 transcript 中追加用户消息卡片。"""
        if self.query_one(MicaComposer).disabled:
            return
        transcript = self.query_one(ConversationTranscript)
        transcript.mount(UserMessageCard(message))
        transcript.content_added()


__all__ = ["MicaTuiApp"]
