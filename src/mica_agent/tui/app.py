"""Mica Textual TUI 外壳与 Engine 事件投影。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from time import monotonic
from typing import ClassVar

from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.dom import DOMNode, NoScreen
from textual.events import (
    AppFocus,
    Event,
    Key,
    MouseScrollDown,
    MouseScrollUp,
    Resize,
)
from textual.widget import Widget

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

from .bottom import TuiFactsView
from .composer import MicaComposer
from .history import iter_history_views
from .interaction_layer import InteractionOverlay
from .interactions import (
    AskInteractionView,
    RunCancelRequester,
    UserInputResponder,
    _call_sync,
    _is_async_callable,
)
from .logo import MicaLogo
from .models import StartupResourceSnapshot, TuiSessionSnapshot
from .output import AssistantResponseView, ReasoningDisplayMode, ReasoningView
from .permissions import (
    PermissionInteractionView,
    PermissionResponder,
)
from .startup import StartupIntroView, StartupReadyView, StartupResourcesView
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

RunRequester = Callable[[str], bool]


class MicaTuiApp(App[None]):
    """Textual 外壳及现有 Engine 公共事件的前端投影边界。"""

    CSS = SHELL_CSS
    EXIT_CONFIRMATION_SECONDS = 2.0
    # Near-synchronous duplicates of one physical press arrive inside a single
    # input burst; this window merges them without merging deliberate presses.
    EXIT_PRESS_DEDUP_SECONDS = 0.15
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "interrupt_run", show=False, priority=True),
        Binding("ctrl+c", "confirm_exit", show=False, priority=True),
        Binding("up", "interaction_up", show=False, priority=True),
        Binding("down", "interaction_down", show=False, priority=True),
        Binding("enter", "interaction_enter", show=False, priority=True),
        Binding("n", "interaction_n", show=False, priority=True),
        Binding("1", "interaction_digit('1')", show=False, priority=True),
        Binding("2", "interaction_digit('2')", show=False, priority=True),
        Binding("3", "interaction_digit('3')", show=False, priority=True),
        Binding("4", "interaction_digit('4')", show=False, priority=True),
        Binding("5", "interaction_digit('5')", show=False, priority=True),
        Binding("pageup", "interaction_pageup", show=False, priority=True),
        Binding("pagedown", "interaction_pagedown", show=False, priority=True),
        Binding("home", "interaction_home", show=False, priority=True),
        Binding("end", "interaction_end", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        ansi_color: bool | None = None,
        startup_resources: StartupResourceSnapshot | None = None,
        session_snapshot: TuiSessionSnapshot | None = None,
        reasoning_display_mode: ReasoningDisplayMode = ReasoningDisplayMode.COLLAPSED,
        clock: Callable[[], float] = monotonic,
        stored_session: StoredSession | None = None,
        tool_card_factories: Mapping[str, ToolCardFactory] | None = None,
        user_input_responder: UserInputResponder | None = None,
        run_cancel_requester: RunCancelRequester | None = None,
        permission_responder: PermissionResponder | None = None,
        run_requester: RunRequester | None = None,
    ) -> None:
        if startup_resources is not None and not isinstance(
            startup_resources, StartupResourceSnapshot
        ):
            raise TypeError(
                "startup_resources must be a StartupResourceSnapshot or None"
            )
        self.startup_resources = startup_resources or StartupResourceSnapshot()
        if session_snapshot is not None and not isinstance(
            session_snapshot, TuiSessionSnapshot
        ):
            raise TypeError("session_snapshot must be a TuiSessionSnapshot or None")
        self.session_snapshot = session_snapshot or TuiSessionSnapshot()
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
        if run_requester is not None and (
            not callable(run_requester) or _is_async_callable(run_requester)
        ):
            raise TypeError("run_requester must be a synchronous callable or None")
        self._run_requester = run_requester
        self._run_active = False
        self._runtime_closed = False
        self._exit_armed_until: float | None = None
        self._last_exit_press_at: float | None = None
        self._cancel_requested: set[str] = set()
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
        # 业务关联索引：只用于接受/拒绝响应与取消请求的归属校验，
        # 不参与键盘所有权判定（后者只看 overlay 的活动子组件）。
        self._active_permission_key: tuple[str, str] | None = None
        self._ask_views: dict[tuple[str, str], AskInteractionView] = {}
        self._ask_calls: dict[tuple[str, str], AskInteractionView] = {}
        self._pending_ask_calls: dict[str, str] = {}
        self._settled_ask_keys: set[tuple[str, str]] = set()
        self._user_input_responder = user_input_responder
        self._run_cancel_requester = run_cancel_requester
        self._permission_responder = permission_responder
        self._active_status_run_id: str | None = None
        super().__init__(ansi_color=ansi_color)
        self.register_theme(MICA_THEME)
        self.theme = MICA_THEME.name

    def compose(self) -> ComposeResult:
        with ConversationTranscript(
            id="transcript",
            has_history=bool(self.stored_session and self.stored_session.turns),
        ):
            yield MicaLogo(id="mica-logo")
            yield StartupIntroView(id="startup-intro")
            yield StartupResourcesView(
                self.startup_resources,
                id="startup-resources",
            )
            yield StartupReadyView(id="startup-ready")
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
        yield TuiFactsView(self.session_snapshot, id="runtime-facts")

    async def project_engine_event(self, event: EngineEvent) -> bool:
        """Project one public Engine event into this TUI."""
        if self._runtime_closed:
            return False
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
                    if run_id != event.run_id:
                        continue
                    if view.is_active:
                        view.finish(terminal_status)
                        closed_ask = True
                    await self._settle_ask_view(view)
                if closed_card or closed_ask:
                    self.query_one(ConversationTranscript).content_added()
                self._pending_ask_calls.pop(event.run_id, None)
                self._clear_permission_interactions_for_run(event.run_id)
                self._sync_shell_chrome(restore_focus=True)
            else:
                self._clear_permission_interactions_for_run(event.run_id)
                self._sync_shell_chrome(restore_focus=True)
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
                await self._close_active_model_step(event.run_id)
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
            await self._close_active_model_step(event.run_id)
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
            await self._show_overlay(view)
            await view.wait_for_refresh()
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
            await self._show_overlay(view)
            await view.wait_for_refresh()
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
                await self._settle_ask_view(view, event.presentation)
                self._prepared_presentations.pop((event.run_id, event.call_id), None)
                self._clear_permission_for_call(
                    event.run_id, event.call_id, event.tool_name
                )
                self.query_one(ConversationTranscript).content_added()
                self._sync_shell_chrome(restore_focus=True)
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
            await view.wait_for_refresh()
            self._response_views[event.run_id] = view
        await view.append_text(event.text)
        self.query_one(ConversationTranscript).content_added()
        return True

    async def _close_active_model_step(self, run_id: str) -> None:
        """End the current model step without removing its transcript widgets."""
        view = self._response_views.pop(run_id, None)
        self._reasoning_views.pop(run_id, None)
        if view is not None and view.is_mounted and not self._runtime_closed:
            await view.finish(stable_markdown=True)
            self.query_one(ConversationTranscript).content_added()

    def _has_active_ask(self) -> bool:
        return bool(self._pending_ask_calls) or any(
            view.is_active for view in self._ask_views.values()
        )

    def _overlay(self) -> InteractionOverlay | None:
        if not self.is_running:
            return None
        overlays = list(self.query(InteractionOverlay))
        return overlays[0] if overlays else None

    def _blocking_owner(
        self,
    ) -> AskInteractionView | PermissionInteractionView | None:
        """Return the single keyboard owner of the shared overlay.

        The overlay's assembled state（可见、已挂载、仍活动）是所有权唯一事实源。
        业务索引（如权限关联键）只负责映射交互，不再决定键盘交给谁。
        """
        overlay = self._overlay()
        if overlay is None:
            return None
        return overlay.active_view

    async def _ensure_overlay(self) -> InteractionOverlay:
        overlay = self._overlay()
        if overlay is not None:
            return overlay
        overlay = InteractionOverlay(id="interaction-overlay")
        overlay.display = False
        await self.screen.mount(overlay)
        return overlay

    async def _show_overlay(self, view: Widget) -> None:
        overlay = await self._ensure_overlay()
        for child in list(overlay.children):
            if child is view:
                continue
            if isinstance(child, AskInteractionView) and not child.is_active:
                await self._settle_ask_view(child)
            elif child.parent is overlay:
                child.remove()
        if view.parent is not overlay:
            await overlay.mount(view)
        overlay.display = True
        self._sync_shell_chrome(restore_focus=True)

    async def _settle_ask_view(
        self,
        view: AskInteractionView,
        presentation: ToolPresentationSnapshot | None = None,
    ) -> None:
        key = (view.run_id, view.interaction_id)
        if key in self._settled_ask_keys or view.is_active:
            return
        self._settled_ask_keys.add(key)
        record = view.to_transcript_record(presentation)
        if view.parent is not None:
            await view.remove()
        if not self._runtime_closed:
            await self.query_one("#transcript").mount(record)
        self._ask_views[key] = record
        for call_key, mapped in tuple(self._ask_calls.items()):
            if mapped is view:
                self._ask_calls[call_key] = record

    def _set_chrome_visible(self, visible: bool) -> None:
        for widget_id in ("#work-status", "#composer", "#runtime-facts"):
            matches = list(self.query(widget_id))
            if matches:
                matches[0].display = visible

    def _sync_shell_chrome(self, *, restore_focus: bool = False) -> None:
        if not self.is_running:
            return
        owner = self._blocking_owner()
        overlay = self._overlay()
        blocking = owner is not None
        if overlay is not None:
            overlay.display = blocking
        self._set_chrome_visible(not blocking)
        composers = list(self.query(MicaComposer))
        if composers:
            composer = composers[0]
            if (
                blocking
                or self._runtime_closed
                or self._run_active
                or self._has_active_ask()
                or self._permission_views
            ):
                composer.disabled = True
            else:
                composer.disabled = False
        if restore_focus:
            self._restore_interaction_focus()

    def _restore_interaction_focus(self) -> None:
        if self._runtime_closed or not self.is_running:
            return
        owner = self._blocking_owner()
        if isinstance(owner, AskInteractionView):
            owner.focus_input()
            return
        if isinstance(owner, PermissionInteractionView):
            owner.focus(scroll_visible=False)
            return
        run_id = self._active_status_run_id
        if self._run_active or (
            run_id is not None and run_id not in self._terminated_run_ids
        ):
            self.query_one(ConversationTranscript).focus(scroll_visible=False)
            return
        composers = list(self.query(MicaComposer))
        if composers and not composers[0].disabled and composers[0].display:
            composers[0].focus(scroll_visible=False)

    def on_app_focus(self, _event: AppFocus) -> None:
        self.call_after_refresh(self._restore_interaction_focus)

    def _focused_widget(self) -> Widget | None:
        """Return the focused widget, tolerating a missing or closed screen."""
        if not self.is_running:
            return None
        try:
            return self.screen.focused
        except (NoScreen, ScreenStackError):
            return None

    def _focus_is_usable(self, focused: Widget | None) -> bool:
        """True when focus still points at a live widget of the current screen."""
        if focused is None or not focused.is_mounted:
            return False
        try:
            if focused.screen is not self.screen:
                return False
        except (NoScreen, ScreenStackError):
            return False
        return focused.display and not focused.disabled

    def _focus_within(self, container: Widget) -> bool:
        """True when the focused widget is the container or one of its children."""
        focused = self._focused_widget()
        if focused is None or not self._focus_is_usable(focused):
            return False
        node: DOMNode | None = focused
        while node is not None:
            if node is container:
                return True
            node = node.parent
        return False

    def _focus_browses_own_overflow(self) -> bool:
        """True while a focused Bash card owns page keys for its own viewport."""
        node: DOMNode | None = self._focused_widget()
        while node is not None:
            if isinstance(node, BashToolCard):
                return True
            node = node.parent
        return False

    def _reconcile_input_owner(self) -> None:
        """Idempotently repair focus before dispatching one input event.

        Real terminal drivers may never report ``AppBlur``/``AppFocus``, so
        the first key, mouse-wheel or resize event after a blur must repair
        the current owner instead of dropping the user's action.  Only an
        unusable focus or a focus outside the blocking owner is corrected.
        """
        if self._runtime_closed or not self.is_running:
            return
        if not self.app_focus:
            # Mirror Textual's own healing; required because owned keys are
            # consumed at the App entry before the framework updates app_focus.
            self.app_focus = True
        owner = self._blocking_owner()
        if owner is not None:
            if not self._focus_within(owner):
                if isinstance(owner, AskInteractionView):
                    owner.focus_input()
                else:
                    owner.focus(scroll_visible=False)
            return
        focused = self._focused_widget()
        if self._focus_is_usable(focused):
            return
        run_id = self._active_status_run_id
        run_current = self._run_active or (
            run_id is not None and run_id not in self._terminated_run_ids
        )
        if run_current:
            transcripts = list(self.query(ConversationTranscript))
            if transcripts:
                transcripts[0].focus(scroll_visible=False)
            return
        for composer in self.query(MicaComposer):
            if not composer.disabled and composer.display:
                composer.focus(scroll_visible=False)
                return

    async def on_event(self, event: Event) -> None:
        if not event.is_forwarded and isinstance(
            event, (Key, MouseScrollDown, MouseScrollUp, Resize)
        ):
            self._reconcile_input_owner()
        if isinstance(event, Key) and not event.is_forwarded:
            owner = self._blocking_owner()
            if owner is not None and owner.owns_key(event.key):
                self._route_interaction_key(event.key)
                event.stop()
                event.prevent_default()
                return
        await super().on_event(event)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"interrupt_run", "confirm_exit"}:
            return True
        key_by_action = {
            "interaction_up": "up",
            "interaction_down": "down",
            "interaction_enter": "enter",
            "interaction_n": "n",
            "interaction_digit": parameters[0] if parameters else "",
            "interaction_pageup": "pageup",
            "interaction_pagedown": "pagedown",
            "interaction_home": "home",
            "interaction_end": "end",
        }
        key = key_by_action.get(action)
        if key is None:
            return True
        owner = self._blocking_owner()
        if owner is None:
            if key in {"pageup", "pagedown"}:
                # No blocking interaction: page keys browse transcript history
                # unless a focused card owns page keys for its own viewport.
                return not self._focus_browses_own_overflow()
            return False
        return owner.owns_key(str(key))

    def _route_interaction_key(self, key: str) -> None:
        owner = self._blocking_owner()
        if owner is None:
            return
        if key in {"pageup", "pagedown", "home", "end"}:
            # 两种交互视图共用同一个滚动容器；面板本身不再自己滚动。
            self._scroll_interaction_overlay(key)
            return
        owner.apply_key(key)

    def _scroll_interaction_overlay(self, key: str) -> None:
        """Scroll the single shared overlay for any active interaction owner."""
        overlay = self._overlay()
        if overlay is None or not overlay.display:
            return
        if key == "pageup":
            overlay.scroll_page_up(animate=False)
        elif key == "pagedown":
            overlay.scroll_page_down(animate=False)
        elif key == "home":
            overlay.scroll_home(animate=False, immediate=True)
        else:
            overlay.scroll_end(animate=False, immediate=True)

    def action_interaction_up(self) -> None:
        self._route_interaction_key("up")

    def action_interaction_down(self) -> None:
        self._route_interaction_key("down")

    def action_interaction_enter(self) -> None:
        self._route_interaction_key("enter")

    def action_interaction_n(self) -> None:
        self._route_interaction_key("n")

    def action_interaction_digit(self, digit: str) -> None:
        self._route_interaction_key(digit)

    def action_interaction_pageup(self) -> None:
        if self._blocking_owner() is None:
            self._browse_transcript_page(page_up=True)
            return
        self._route_interaction_key("pageup")

    def action_interaction_pagedown(self) -> None:
        if self._blocking_owner() is None:
            self._browse_transcript_page(page_up=False)
            return
        self._route_interaction_key("pagedown")

    def _browse_transcript_page(self, *, page_up: bool) -> None:
        """Browse transcript history when no interaction owns the page keys."""
        transcripts = list(self.query(ConversationTranscript))
        if not transcripts:
            return
        if page_up:
            transcripts[0].scroll_page_up(animate=False)
        else:
            transcripts[0].scroll_page_down(animate=False)

    def action_interaction_home(self) -> None:
        self._route_interaction_key("home")

    def action_interaction_end(self) -> None:
        self._route_interaction_key("end")

    def _respond_permission(self, response: PermissionResponse) -> bool:
        if not isinstance(response, PermissionResponse) or self._runtime_closed:
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
            self._runtime_closed
            or key is None
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
        self._sync_shell_chrome(restore_focus=True)
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
        self._sync_shell_chrome(restore_focus=True)

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
        self.stop_runtime_input()
        self.clear_runtime_interactions()
        super().exit(result, return_code, message)

    def _cancellable_run_id(self) -> str | None:
        """Return the current projected run_id that can still be cancelled."""
        if (
            self._runtime_closed
            or self._active_status_run_id is None
            or self._active_status_run_id in self._terminated_run_ids
            or self._active_status_run_id in self._cancel_requested
        ):
            return None
        return self._active_status_run_id

    def _request_run_cancel(self, run_id: str) -> bool:
        accepted = _call_sync(self._run_cancel_requester, run_id)
        if not accepted:
            return False
        self._cancel_requested.add(run_id)
        self._pending_ask_calls.pop(run_id, None)
        closed_ask = False
        for (owner, _interaction_id), view in tuple(self._ask_views.items()):
            if owner != run_id:
                continue
            if view.is_active:
                view.finish(ToolResultStatus.CANCELLED)
                closed_ask = True
        self._clear_permission_interactions_for_run(run_id)
        if closed_ask:
            self.query_one(ConversationTranscript).content_added()
        self._sync_shell_chrome(restore_focus=True)
        return True

    def action_interrupt_run(self) -> None:
        """Cancel the current Run from any focused widget; idle Esc is a no-op."""
        run_id = self._cancellable_run_id()
        if run_id is None:
            return
        self._request_run_cancel(run_id)

    def _handle_exit_press(self) -> None:
        """Count one independent Ctrl+C press; duplicates never exit."""
        if self._runtime_closed:
            return
        now = self._clock()
        last_press = self._last_exit_press_at
        if (
            last_press is not None
            and 0.0 <= now - last_press < self.EXIT_PRESS_DEDUP_SECONDS
        ):
            # One physical press can be delivered near-synchronously more than
            # once (the key plus a console signal, or a duplicated dispatch);
            # those duplicates must never count as the second press.
            # 平台限制：Textual 8.2.8 的 Windows 控制台输入在转换时丢弃 key-up
            # 记录并忽略 wRepeatCount，``Key`` 事件也不携带按下/重复/释放标记，
            # 因此“延迟到达的自动重复”与本层可见的“新的独立按下”无法区分。
            # 该限制已写入 P6-10F 构建日志，并改由真实终端手工复验，而不是
            # 在这里声称已识别完美物理按键。
            return
        self._last_exit_press_at = now
        if self._exit_armed_until is not None and now <= self._exit_armed_until:
            self._exit_armed_until = None
            self.exit()
            return
        run_id = self._cancellable_run_id()
        if run_id is not None:
            self._request_run_cancel(run_id)
        self._exit_armed_until = now + self.EXIT_CONFIRMATION_SECONDS

    def action_confirm_exit(self) -> None:
        """First Ctrl+C arms a short confirmation window; the second exits."""
        self._handle_exit_press()

    def handle_native_interrupt(self) -> None:
        """Route a native Ctrl+C signal into the same exit state machine."""
        if self._runtime_closed or not self.is_running:
            return
        self._handle_exit_press()

    def stop_runtime_input(self) -> None:
        """Stop input synchronously; resource cleanup belongs to the runtime."""
        self._runtime_closed = True
        self._exit_armed_until = None
        if not self.is_running:
            return
        for composer in self.query(MicaComposer):
            composer.disabled = True

    def clear_runtime_interactions(self) -> None:
        """Detach temporary interaction references without inventing events."""
        for view in self._ask_views.values():
            if view.is_active:
                view.finish(ToolResultStatus.CANCELLED)
        self._ask_views.clear()
        self._ask_calls.clear()
        self._pending_ask_calls.clear()
        self._settled_ask_keys.clear()
        for view in tuple(self._permission_views.values()):
            view.deactivate()
            if view.parent is not None:
                view.remove()
        self._permission_views.clear()
        self._permission_interactions.clear()
        self._permission_response_ids.clear()
        self._prepared_presentations.clear()
        self._active_permission_key = None
        overlay = self._overlay()
        if overlay is not None:
            overlay.display = False

    def hold_run_input(self) -> None:
        """Lock the composer from synchronous acceptance through stream cleanup."""
        self._run_active = True
        self._set_composer_disabled(True)

    def release_run_input(self, run_id: str | None, *, interrupted: bool) -> None:
        """Finish UI-only cleanup even when no terminal event could be projected."""
        if run_id is not None:
            self._terminated_run_ids.add(run_id)
            self._pending_ask_calls.pop(run_id, None)
            self._clear_permission_interactions_for_run(run_id)
            for (owner, _), view in self._ask_views.items():
                if owner == run_id and view.is_active:
                    view.finish(ToolResultStatus.CANCELLED)
        if interrupted:
            for status in self.query(WorkStatusView):
                status.finish(WorkStatus.INTERRUPT)
        self._run_active = False
        if self.query(MicaComposer):
            self._sync_shell_chrome(restore_focus=True)

    async def finish_interrupted_output(self, run_id: str | None) -> None:
        """Freeze partial text without marking it as a successful reply."""
        view = self._response_views.get(run_id) if run_id is not None else None
        if view is not None and view.is_mounted and not self._runtime_closed:
            await view.finish(stable_markdown=False)

    def _set_composer_disabled(self, disabled: bool) -> None:
        composer = self.query_one(MicaComposer)
        composer.disabled = disabled

    def _restore_composer_if_possible(self) -> None:
        self._sync_shell_chrome(restore_focus=True)

    def on_mica_composer_submitted(self, message: MicaComposer.Submitted) -> None:
        """收到有效提交后，在 transcript 中追加用户消息卡片。"""
        if self.query_one(MicaComposer).disabled or self._runtime_closed:
            return
        if self._run_requester is not None:
            if not _call_sync(self._run_requester, message.text):
                return
            self.hold_run_input()
        transcript = self.query_one(ConversationTranscript)
        transcript.mount(UserMessageCard(message))
        transcript.content_added()


__all__ = ["MicaTuiApp", "RunRequester"]
