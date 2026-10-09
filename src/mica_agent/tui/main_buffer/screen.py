"""Main-buffer frontend orchestration and mode machine.

The active modes are ``IDLE``, ``RUNNING``, ``ASK``, ``PERMISSION``, ``MENU``, and
``SWITCHING``, plus terminal ``CLOSED``. Terminal focus does not change mode;
explicit mode alone owns input, so missing focus events cannot change how the next
key is handled.

The orchestration layer owns the terminal, renderer, editor, and single-run controller,
but not business state. Existing services retain sessions, tool results, permissions,
and pending interactions. Public events may mutate records only after run identity,
monotonic ``seq``, and ``(run_id, call_id)`` checks.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from mica_agent.app import MicaApp, ModelSwitchResult

from mica_agent.checkpoint_contracts import (
    RunCheckpoint,
    RunCheckpointToolProgress,
    RunCheckpointToolStatus,
)
from mica_agent.config_contracts import ConfigConflictError
from mica_agent.events import (
    ErrorCategory,
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
from mica_agent.session_contracts import TurnEntry
from mica_agent.tool_contracts import ToolResultStatus
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)

from ..models import ReasoningDisplayMode, TuiSessionSnapshot
from .commands import Command, CommandCandidates, CommandRegistry
from .components import (
    IDLE_HINT,
    ReasoningBlock,
    assistant_block,
    composer_body_width,
    composer_frame,
    facts_lines,
    read_git_branch,
    startup_blocks,
    status_line,
    user_block,
)
from .editor import ComposerEditor, Key, normalize_key, windowed_lines
from .interactions import (
    AskInteraction,
    InteractionOutcome,
    PermissionInteraction,
)
from .markdown import MarkdownBlock, StreamingMarkdownBlock
from .menus import ModelMenu
from .renderer import Block, BlockLike, MainBufferRenderer, Palette, truncate_head
from .runtime import InterruptedMarker, MainBufferRuntime
from .sanitize import sanitize_scalar, sanitize_text
from .terminal import Terminal
from .tool_views import GenericToolView, ToolView, ToolViewFactory

Mode = Literal["idle", "running", "ask", "permission", "menu", "switching", "closed"]
Status = Literal["idle", "working", "done", "stop", "error"]

CTRL_C_WINDOW = 2.0
DONE_VISIBLE_SECONDS = 2.0
REFRESH_INTERVAL = 0.25
COMMAND_NOTICE_SECONDS = 2.0
BUSY_COMMAND_NOTICE = "Commands are available after the current task finishes."
MAX_TOOL_DETAIL = 72
MAX_OUTPUT_LINES = 8
# A lone Esc finishes only after this bounded window without another byte, preventing
# complete ESC[A or ESC[B sequences from splitting into Escape plus text.
ESCAPE_FLUSH_DELAY = 0.1

# Internal interruption reasons map to status; real public terminal events take precedence.
_STOP_REASONS = frozenset({"cancelled"})
_RECORDED = "recorded"
CHECKPOINT_INCOMPLETE_MARKER = "incomplete"

# RunFailed uses fixed safe text mapped only from public ErrorCategory, never raw messages.
_FAILURE_MESSAGES: dict[ErrorCategory, str] = {
    ErrorCategory.AUTHENTICATION: "Provider authentication failed.",
    ErrorCategory.RATE_LIMIT: "Provider rate limit reached.",
    ErrorCategory.TIMEOUT: "The provider request timed out.",
    ErrorCategory.TEMPORARY: "The provider is temporarily unavailable.",
    ErrorCategory.MALFORMED: "The provider returned an unusable response.",
    ErrorCategory.STORAGE: "The session could not be stored.",
    ErrorCategory.LIMIT: "The run stopped at a configured limit.",
    ErrorCategory.UNKNOWN: "The run stopped because of an error.",
}

# Public contract: ToolFinished.status must match final snapshot status; others are FAILED.
_FINAL_PRESENTATION_STATUS = {
    ToolResultStatus.SUCCESS: ToolPresentationStatus.SUCCESS,
    ToolResultStatus.CANCELLED: ToolPresentationStatus.CANCELLED,
}


class MainBufferScreen:
    """Main-buffer frontend for one MicaApp, one terminal, and one active run."""

    def __init__(
        self,
        app: MicaApp,
        *,
        terminal: Terminal | None = None,
        startup_resources: object | None = None,
        session_snapshot: object | None = None,
        stored_session: object | None = None,
        workspace_path: object | None = None,
        clock: Any = time.monotonic,
        sleep: Any = asyncio.sleep,
        branch_reader: Any = read_git_branch,
        reasoning_mode: ReasoningDisplayMode = ReasoningDisplayMode.COLLAPSED,
        tool_views: Mapping[str, Any] | None = None,
        escape_flush_delay: float = ESCAPE_FLUSH_DELAY,
    ) -> None:
        self._app = app
        self._terminal = Terminal() if terminal is None else terminal
        self._startup_resources = startup_resources
        self._session_snapshot = session_snapshot
        self._stored_session = stored_session
        self._workspace_path = workspace_path
        self._clock = clock
        self._sleep = sleep
        self._read_branch = branch_reader
        self._reasoning_mode = (
            reasoning_mode
            if isinstance(reasoning_mode, ReasoningDisplayMode)
            else ReasoningDisplayMode.COLLAPSED
        )
        # The factory copies mappings at construction; external views cannot shadow built-ins.
        self._tool_views = ToolViewFactory(tool_views)
        self._assistant_block: StreamingMarkdownBlock | None = None
        self._pending_tool: ToolView | None = None
        self._palette = Palette(False)
        self._renderer: MainBufferRenderer | None = None
        self._editor = ComposerEditor()
        self._runtime = MainBufferRuntime(app, event_sink=self._enqueue_event)
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._mode: Mode = "idle"
        self._status: Status = "idle"
        self._run_started: float | None = None
        self._elapsed: float | None = None
        self._done_deadline: float | None = None
        self._ask: AskInteraction | None = None
        self._permission: PermissionInteraction | None = None
        self._pending_kind: str | None = None
        self._pending_text = ""
        self._pending_presentation: ToolPresentationSnapshot | None = None
        self._pending_call_id: str | None = None
        self._tool_output: list[str] = []
        self._tool_output_total = 0
        self._tool_partial = ""
        self._ctrl_c_at: float | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._escape_flush_delay = max(0.0, float(escape_flush_delay))
        self._flush_handle: asyncio.TimerHandle | None = None
        self._branch: str | None = None
        self._run_id: str | None = None
        self._last_seq = -1
        self._terminal_run_id: str | None = None
        self._closed = False
        self._shutdown_done = False
        self._facts_dirty = False
        self.diagnostics: list[str] = []
        self.cleanup_errors: list[str] = []
        self._configuration_notice: str | None = app.connection_diagnostic
        self._commands = CommandRegistry(
            (
                Command(
                    "/model",
                    "Switch model or set global default",
                    self._open_model_menu,
                ),
            )
        )
        self._candidates = CommandCandidates(self._commands)
        self._menu: ModelMenu | None = None
        self._command_notice: str | None = None
        self._command_notice_deadline: float | None = None
        self._switch_tasks: set[asyncio.Task[ModelSwitchResult]] = set()
        self._switch_request: object | None = None

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def status(self) -> Status:
        return self._status

    @property
    def renderer(self) -> MainBufferRenderer | None:
        return self._renderer

    @property
    def editor(self) -> ComposerEditor:
        return self._editor

    @property
    def composer_text(self) -> str:
        """Displayed draft text, with long pastes represented by summaries."""
        return self._editor.text

    @property
    def submit_text(self) -> str:
        """Original draft text that would be submitted to MicaApp."""
        return self._editor.submit_text()

    @property
    def branch(self) -> str | None:
        return self._branch

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def refresh_task(self) -> asyncio.Task[None] | None:
        return self._refresh_task

    @property
    def closed(self) -> bool:
        """Whether shutdown has converged both terminal and business resources."""
        return self._shutdown_done

    @property
    def interaction(self) -> AskInteraction | PermissionInteraction | None:
        """Current ask or permission interaction with exclusive input ownership."""
        return self._ask or self._permission

    def update_session_snapshot(self, snapshot: object | None) -> None:
        """Update the public snapshot after model or session changes."""
        self._session_snapshot = snapshot
        self.render()

    def handle_resize(self) -> None:
        """Read terminal size and reflow without changing draft, mode, or event order."""
        self.render()

    async def run(self) -> None:
        """Enter raw mode, render the first frame, and run the mode machine until exit."""
        capabilities = self._terminal.capabilities()
        if not capabilities.interactive:
            raise RuntimeError("main buffer frontend requires an interactive terminal")
        self._palette = Palette(capabilities.ansi)
        self._renderer = MainBufferRenderer(
            self._terminal,
            width=capabilities.width,
            height=capabilities.height,
            palette=self._palette,
            interactive=True,
        )
        loop = asyncio.get_running_loop()
        try:
            await self._refresh_branch()
            self._startup_document()
            self._terminal.open_input()
            self._terminal.attach_input(lambda: self._on_input_ready(loop))
            self.render()
            await self._loop()
        finally:
            await self._shutdown()

    async def _loop(self) -> None:
        while True:
            kind, payload = await self._queue.get()
            if kind == "key":
                if self._handle_key(payload):
                    break
            elif kind == "model_switch":
                self._finish_model_switch(*payload)
            else:
                await self.handle_event(payload)
            self.render()
            if self._facts_dirty:
                # Emit status and terminal rows before refreshing public facts.
                self._facts_dirty = False
                await self._refresh_branch()
                self.render()

    async def _shutdown(self) -> None:
        """Shut down idempotently: stop refresh and flush, restore terminal, close runtime."""
        if self._closed:
            return
        self._closed = True
        self._cancel_escape_flush()
        try:
            await self._stop_refresh()
        except Exception as error:  # noqa: BLE001 - record cleanup failure without raising
            self.cleanup_errors.append(f"stop refresh: {type(error).__name__}")
        finally:
            self._finalize_terminal()
            try:
                await self._close_model_switches()
            except Exception:  # noqa: BLE001 - pending model work must not expose errors
                self.cleanup_errors.append("model switch cleanup failed")
            try:
                await self._runtime.aclose()
                if self._runtime.cleanup_error is not None:
                    self.cleanup_errors.append(self._runtime.cleanup_error)
            except Exception as error:  # noqa: BLE001 - record cleanup failure without raising
                self.cleanup_errors.append(f"close runtime: {type(error).__name__}")
            finally:
                self._mode = "closed"
                self._shutdown_done = True

    async def _close_model_switches(self) -> None:
        self._switch_request = None
        self._menu = None
        tasks = tuple(self._switch_tasks)
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if any(isinstance(result, Exception) for result in results):
            self.cleanup_errors.append("model switch cleanup failed")

    def _finalize_terminal(self) -> None:
        """Restore terminal state synchronously with each cleanup step isolated."""
        renderer = self._renderer
        if renderer is not None:
            self._attempt("commit pending", renderer.commit_pending)
            self._attempt("clear chrome", renderer.clear_chrome)
        self._attempt("close input", self._terminal.close_input)

    def _attempt(self, label: str, action: Callable[[], Any]) -> None:
        """Run one cleanup step, recording only a fixed label on failure."""
        try:
            action()
        except Exception as error:  # noqa: BLE001 - record cleanup failure without raising
            self.cleanup_errors.append(f"{label}: {type(error).__name__}")

    def _startup_document(self) -> None:
        renderer = self._renderer
        assert renderer is not None
        if self._startup_resources is not None:
            for block in startup_blocks(
                self._startup_resources,
                renderer.width,
                self._palette,
                ready=self._app.connection_status == "ready",
            ):
                renderer.append(block)
        if self._app.connection_summary is not None:
            renderer.append(Block("muted", self._app.connection_summary))
        self._append_history()

    def _append_history(self) -> None:
        renderer = self._renderer
        assert renderer is not None
        stored = self._stored_session
        if stored is None:
            return
        entries = getattr(stored, "entries", None)
        if entries is None:
            entries = getattr(stored, "turns", ())
        for entry in entries:
            if isinstance(entry, RunCheckpoint):
                self._append_checkpoint_history(entry)
                continue
            if isinstance(entry, TurnEntry) or hasattr(entry, "assistant_message"):
                self._append_turn_history(entry)

    def _append_turn_history(self, turn: object) -> None:
        renderer = self._renderer
        assert renderer is not None
        renderer.append(user_block(turn.user_message.content))
        presentations = {
            presentation.call_id: presentation
            for presentation in getattr(turn, "tool_presentations", ())
        }
        for message in getattr(turn, "intermediate_messages", ()):
            for call in getattr(message, "tool_calls", ()):
                renderer.append(
                    self._history_tool_block(call, presentations.get(call.call_id))
                )
        assistant = getattr(turn, "assistant_message", None)
        content = getattr(assistant, "content", "")
        if content:
            renderer.append(MarkdownBlock(content))

    def _append_checkpoint_history(self, checkpoint: RunCheckpoint) -> None:
        renderer = self._renderer
        assert renderer is not None
        renderer.append(user_block(checkpoint.user_message.content))
        for step in checkpoint.progress:
            if step.assistant_message is not None and step.assistant_message.content:
                renderer.append(MarkdownBlock(step.assistant_message.content))
            for item in step.tools:
                renderer.append(self._checkpoint_tool_block(item))
        if checkpoint.partial_assistant_message is not None:
            renderer.append(MarkdownBlock(checkpoint.partial_assistant_message.content))
        renderer.append(Block("muted", CHECKPOINT_INCOMPLETE_MARKER))

    def _checkpoint_tool_block(self, item: RunCheckpointToolProgress) -> BlockLike:
        if item.status in {
            RunCheckpointToolStatus.SUCCESS,
            RunCheckpointToolStatus.FAILED,
        }:
            return self._history_tool_block(item.call, item.presentation)
        state = {
            RunCheckpointToolStatus.IN_PROGRESS: "running",
            RunCheckpointToolStatus.NOT_STARTED: "pending",
        }[item.status]
        name = sanitize_scalar(item.call.name, fallback="tool")
        return Block("muted", f"tool {name} · {state}")

    def _history_tool_block(
        self, call: object, presentation: ToolPresentationSnapshot | None
    ) -> BlockLike:
        """Use a specialized view for valid historical snapshots, otherwise a neutral record."""
        name = getattr(call, "name", None)
        if isinstance(
            presentation, ToolPresentationSnapshot
        ) and self._accept_presentation(presentation):
            view = self._tool_views.create(
                presentation, state=presentation.status.value
            )
            if view is not None:
                return view
        return Block(
            "muted", f"tool {sanitize_scalar(name, fallback='tool')} · {_RECORDED}"
        )

    def _on_input_ready(self, loop: asyncio.AbstractEventLoop) -> None:
        """Enqueue ready keys and manage the sole pending flush.

        The callback may run on an input thread, so every event-loop operation uses its
        thread-safe entry point in call order.
        """
        try:
            keys = self._terminal.drain_keys()
        except EOFError:
            # Treat an ended input stream as EOF under the same idle-exit rule.
            loop.call_soon_threadsafe(self._queue.put_nowait, ("key", Key("ctrl_d")))
            return
        except Exception as error:  # noqa: BLE001 - input failure must not terminate the frontend
            self.diagnostics.append(f"input read failed: {type(error).__name__}")
            return
        if not keys:
            # An empty drain may be lone Esc: schedule one bounded flush, coalescing an
            # existing handle without rescheduling or polling.
            loop.call_soon_threadsafe(self._schedule_escape_flush, loop)
            return
        # A complete sequence cancels pending flush so ESC[A remains intact.
        loop.call_soon_threadsafe(self._cancel_escape_flush)
        self._enqueue_keys(loop, keys)

    def _enqueue_keys(self, loop: asyncio.AbstractEventLoop, keys: list[Any]) -> None:
        """Enqueue keys in order through the sole path shared by drain and flush."""
        for keypress in keys:
            loop.call_soon_threadsafe(
                self._queue.put_nowait, ("key", normalize_key(keypress))
            )

    def _schedule_escape_flush(self, loop: asyncio.AbstractEventLoop) -> None:
        """Schedule one bounded flush unless closed or one is already pending."""
        if self._closed or self._flush_handle is not None:
            return
        self._flush_handle = loop.call_later(
            self._escape_flush_delay, self._flush_pending_input, loop
        )

    def _flush_pending_input(self, loop: asyncio.AbstractEventLoop) -> None:
        """Finish incomplete input and enqueue resulting keys through the drain path."""
        self._flush_handle = None
        if self._closed:
            return
        try:
            keys = self._terminal.flush_keys()
        except Exception as error:  # noqa: BLE001 - flush failure must not terminate the frontend
            self.diagnostics.append(f"input flush failed: {type(error).__name__}")
            return
        self._enqueue_keys(loop, keys)

    def _cancel_escape_flush(self) -> None:
        handle, self._flush_handle = self._flush_handle, None
        if handle is not None:
            handle.cancel()

    def _handle_key(self, key: Key) -> bool:
        """Handle one key and return whether the frontend should exit."""
        if key.name == "ctrl_c":
            return self._handle_interrupt()
        if key.name == "ctrl_o":
            # Global tool details affect only tool records, not reasoning, ask, composer, or facts.
            renderer = self._renderer
            if renderer is not None:
                renderer.toggle_expanded()
            return False
        if self._mode in {"ask", "permission"}:
            self._handle_interaction_key(key)
            return False
        if self._mode == "menu":
            self._handle_menu_key(key)
            return False
        if key.name == "escape":
            if self._mode == "running":
                self._runtime.cancel(self._runtime.run_id)
            elif self._mode == "idle" and self._candidates.visible:
                self._candidates.dismiss()
            return False
        if self._mode in {"running", "switching"}:
            if key.name == "enter":
                command = self._commands.exact(self._editor.submit_text())
                if command is not None:
                    self._execute_command(command)
            elif key.name != "ctrl_d":
                self._handle_composer_key(key)
            return False
        if key.name == "ctrl_d":
            # Exit only when idle with a blank draft; otherwise ignore Ctrl-D without
            # deleting text or inventing a run.
            return self._editor.is_blank
        self._handle_composer_key(key)
        return False

    def _handle_interrupt(self) -> bool:
        now = self._clock()
        armed = self._ctrl_c_at
        if armed is not None and (now - armed) <= CTRL_C_WINDOW:
            self._ctrl_c_at = None
            return True
        self._ctrl_c_at = now
        if self._mode == "running":
            self._runtime.cancel(self._runtime.run_id)
        return False

    def _handle_composer_key(self, key: Key) -> None:
        self._candidates.sync(self._editor.submit_text())
        if key.name == "enter":
            command = self._candidates.selected
            if command is not None:
                self._execute_command(command)
            else:
                self._submit()
        elif (
            self._mode == "idle"
            and self._candidates.visible
            and key.name in {"up", "down"}
        ):
            self._candidates.move(-1 if key.name == "up" else 1)
        elif key.name == "newline":
            self._editor.insert("\n")
        elif key.name in {"text", "paste"}:
            self._editor.insert(key.data)
        elif key.name in {"backspace", "delete"}:
            getattr(self._editor, key.name)()
        elif key.name in {"left", "right", "up", "down", "home", "end"}:
            self._editor.move(key.name)
        self._candidates.sync(self._editor.submit_text())

    def _set_command_notice(self, text: str) -> None:
        self._command_notice = text
        self._command_notice_deadline = self._clock() + COMMAND_NOTICE_SECONDS
        self._start_refresh()

    def _execute_command(self, command: Command) -> None:
        if self._mode != "idle":
            self._set_command_notice(BUSY_COMMAND_NOTICE)
            return
        self._editor.clear()
        self._candidates.sync("")
        self._command_notice = None
        self._command_notice_deadline = None
        command.handler()

    def _open_model_menu(self) -> None:
        try:
            choices = self._app.model_choices()
        except Exception:  # noqa: BLE001 - configuration exceptions stay private
            self._set_command_notice("Unable to load models.")
            return
        self._menu = ModelMenu(choices)
        self._mode = "menu"

    def _close_model_menu(self) -> None:
        self._menu = None
        # Visibility does not end a request: input stays gated until its queue result.
        self._mode = "switching" if self._switch_request is not None else "idle"

    def _handle_menu_key(self, key: Key) -> None:
        menu = self._menu
        if menu is None:
            return
        outcome = menu.handle(key)
        if outcome.action == "close":
            self._close_model_menu()
        elif outcome.action == "save_default" and outcome.selection is not None:
            try:
                self._app.save_default_model(outcome.selection)
            except ConfigConflictError:
                menu.set_hint(
                    "Default changed elsewhere. Please reopen the menu and try again."
                )
            except Exception:  # noqa: BLE001 - saving errors must never echo private input
                menu.set_hint("Unable to save global default.")
            else:
                try:
                    choices = self._app.model_choices()
                except Exception:  # noqa: BLE001 - committed save and failed refresh differ
                    menu.set_hint(
                        "Default saved, but unable to refresh models. Reopen the menu."
                    )
                else:
                    menu.set_choices(choices)
                    menu.set_hint("Saved as global default.")
        elif outcome.action == "switch" and outcome.selection is not None:
            menu.set_hint("")
            menu.set_busy(True)
            request = object()
            self._switch_request = request
            task = asyncio.create_task(
                self._app.switch_model(outcome.selection), name="mica-menu-model-switch"
            )
            self._switch_tasks.add(task)
            task.add_done_callback(
                lambda finished: self._enqueue_model_switch(finished, menu, request)
            )

    def _enqueue_model_switch(
        self, task: asyncio.Task[ModelSwitchResult], menu: ModelMenu, request: object
    ) -> None:
        from mica_agent.app import AppBusyError, AppClosedError

        self._switch_tasks.discard(task)
        result = None
        hint = None
        try:
            result = task.result()
        except asyncio.CancelledError:
            return
        except AppBusyError:
            hint = "The app is busy. Try again after the current task finishes."
        except AppClosedError:
            hint = "The app is closed."
        except Exception:  # noqa: BLE001 - classify switch failures without retaining errors
            hint = "Unable to switch model."
        if not self._closed:
            self._queue.put_nowait(("model_switch", (menu, request, result, hint)))

    def _finish_model_switch(
        self,
        menu: ModelMenu,
        request: object,
        result: ModelSwitchResult | None,
        hint: str | None,
    ) -> None:
        if self._closed or self._switch_request is not request:
            return
        self._switch_request = None
        visible = self._mode == "menu" and self._menu is menu
        if result is None or not result.switched:
            message = hint or "Unable to switch model."
            if result is not None:
                message = result.message
                if result.cleanup_warning:
                    message += " " + result.cleanup_warning
            if visible:
                menu.set_busy(False)
                menu.set_hint(message)
            elif self._mode == "switching":
                self._mode = "idle"
                self._set_command_notice(message)
            return
        # A committed selection updates global facts even after its menu was dismissed.
        selection = self._app.current_selection
        if selection is not None and isinstance(
            self._session_snapshot, TuiSessionSnapshot
        ):
            self._session_snapshot = replace(
                self._session_snapshot, model=f"{selection.model_id} (session)"
            )
        renderer = self._renderer
        if renderer is not None:
            summary = self._app.connection_summary
            if summary:
                renderer.append(Block("muted", summary))
            if result.cleanup_warning:
                renderer.append(Block("muted", result.cleanup_warning))
        self._configuration_notice = self._app.reasoning_notice
        if visible:
            menu.set_busy(False)
            self._close_model_menu()
        elif self._mode == "switching":
            self._mode = "idle"

    def _submit(self) -> None:
        """Submit a draft; runtime rejection preserves it and creates no user row or run."""
        if self._editor.is_blank:
            self._editor.clear()
            return
        display = self._editor.text
        prompt = self._editor.submit_text()
        if not self._runtime.submit(prompt):
            self.diagnostics.append("submit rejected")
            self._configuration_notice = self._runtime.last_diagnostic
            return
        self._configuration_notice = self._app.reasoning_notice
        if self._runtime.settings is not None:
            self._reasoning_mode = self._runtime.settings.reasoning_display
        self._editor.clear()
        self._begin_run(prompt, display=display)

    def _begin_run(self, prompt: str, *, display: str | None = None) -> None:
        self._status = "working"
        self._run_started = self._clock()
        self._elapsed = 0.0
        self._done_deadline = None
        self._pending_kind = None
        self._pending_text = ""
        self._pending_presentation = None
        self._pending_call_id = None
        self._pending_tool = None
        self._assistant_block = None
        self._tool_output = []
        self._tool_output_total = 0
        self._tool_partial = ""
        self._run_id = None
        self._last_seq = -1
        self._terminal_run_id = None
        renderer = self._renderer
        if renderer is not None:
            renderer.append(user_block(display if display is not None else prompt))
        self._mode = "running"
        self._start_refresh()

    def _handle_interaction_key(self, key: Key) -> None:
        interaction = self._ask or self._permission
        if interaction is None:
            return
        outcome: InteractionOutcome = interaction.handle(key)
        if outcome.action == "cancel":
            self._runtime.cancel(self._runtime.run_id)
            self._close_interaction()
            return
        if outcome.action != "response" or outcome.response is None:
            return
        if self._runtime.respond_interaction(outcome.response) is True:
            self._close_interaction()
            return
        # Business rejection preserves the interaction, selection, custom draft, and note.
        # Show only a fixed panel message so users can retry or cancel without leaked internals.
        self.diagnostics.append("interaction response rejected")
        interaction.fail()

    def _close_interaction(self) -> None:
        self._ask = None
        self._permission = None
        if not self._runtime.is_closed:
            self._mode = "running"
            self._status = "working"
            self._start_refresh()

    async def _enqueue_event(self, event: object) -> None:
        await self._queue.put(("event", event))

    def _accept_event(self, event: object) -> bool:
        """Sole admission check for run identity, monotonic seq, and post-terminal events."""
        run_id = getattr(event, "run_id", None)
        seq = getattr(event, "seq", None)
        if not isinstance(run_id, str) or not isinstance(seq, int):
            return False
        if self._run_id is None:
            if not isinstance(event, RunStarted):
                self.diagnostics.append("event without active run")
                return False
            self._run_id = run_id
            self._last_seq = -1
        elif run_id != self._run_id:
            self.diagnostics.append("event for unknown run")
            return False
        if self._terminal_run_id == run_id:
            self.diagnostics.append("event after terminal state")
            return False
        if seq <= self._last_seq:
            self.diagnostics.append("event out of order")
            return False
        self._last_seq = seq
        return True

    async def handle_event(self, event: object) -> None:
        """Project one public event onto the UI through the runtime and test boundary."""
        if isinstance(event, InterruptedMarker):
            self._finish_run("stop" if event.reason in _STOP_REASONS else "error")
            return
        if not self._accept_event(event):
            return
        if isinstance(event, RunStarted):
            self._status = "working"
            self._mode = "running"
            self._start_refresh()
            return
        if isinstance(event, ReasoningDelta):
            self._start_pending("reasoning")
            self._pending_text += sanitize_text(event.text, newlines=True)
            self._update_pending(
                ReasoningBlock(self._pending_text, self._reasoning_mode)
            )
            return
        if isinstance(event, TextDelta):
            self._start_pending("assistant")
            if self._assistant_block is None:
                self._assistant_block = StreamingMarkdownBlock()
            self._assistant_block.append(sanitize_text(event.text, newlines=True))
            self._update_pending(self._assistant_block)
            return
        if isinstance(event, ToolPrepared):
            if not self._accept_presentation(event.presentation):
                self.diagnostics.append("tool presentation rejected")
                return
            self._start_pending("tool")
            self._pending_presentation = event.presentation
            self._pending_call_id = event.call_id
            self._tool_output = []
            self._tool_output_total = 0
            self._tool_partial = ""
            self._update_pending(self._tool_record_block("pending"))
            return
        if isinstance(event, ToolStarted):
            if self._is_current_tool(event):
                self._update_pending(self._tool_record_block("running"))
            return
        if isinstance(event, ToolOutputDelta):
            if self._is_current_tool(event):
                self._append_tool_output(event.text)
                self._update_pending(self._tool_record_block("running"))
            return
        if isinstance(event, ToolFinished):
            if self._is_current_tool(event):
                self._finish_tool(event)
            return
        if isinstance(event, UserInputRequested):
            self._ask = AskInteraction(event.interaction_id, event.prompt)
            self._permission = None
            self._mode = "ask"
            return
        if isinstance(event, PermissionRequested):
            self._permission = PermissionInteraction(
                event.interaction_id,
                event.tool_name,
                self._permission_detail(event),
            )
            self._ask = None
            self._mode = "permission"
            return
        if isinstance(event, RunCompleted):
            self._finish_run("done")
            self._facts_dirty = True
            return
        if isinstance(event, RunFailed):
            self._commit_pending()
            renderer = self._renderer
            if renderer is not None:
                message = _FAILURE_MESSAGES.get(
                    event.category, _FAILURE_MESSAGES[ErrorCategory.UNKNOWN]
                )
                renderer.append(assistant_block("error: " + message))
            self._finish_run("error")
            self._facts_dirty = True
            return
        if isinstance(event, RunCancelled):
            self._finish_run("stop")
            self._facts_dirty = True
            return

    def _accept_presentation(
        self, presentation: ToolPresentationSnapshot | None
    ) -> bool:
        """Require name and presentation kind to agree before projection."""
        if presentation is None:
            return False
        expected = {
            ToolPresentationKind.READ: "read",
            ToolPresentationKind.WRITE: "write",
            ToolPresentationKind.EDIT: "edit",
            ToolPresentationKind.BASH: "bash",
            ToolPresentationKind.ASK: "ask",
        }.get(presentation.kind)
        return expected is None or presentation.tool_name == expected

    def _is_current_tool(self, event: object) -> bool:
        call_id = getattr(event, "call_id", None)
        tool_name = getattr(event, "tool_name", None)
        if self._pending_kind != "tool" or self._pending_call_id is None:
            self.diagnostics.append("tool event without pending record")
            return False
        if call_id != self._pending_call_id:
            self.diagnostics.append("tool event for wrong call")
            return False
        pending = self._pending_presentation
        if pending is not None and tool_name != pending.tool_name:
            self.diagnostics.append("tool event for wrong tool")
            return False
        return True

    def _accept_final_presentation(self, event: ToolFinished) -> bool:
        """Validate final snapshot identity and state, rejecting the whole event on mismatch."""
        presentation = event.presentation
        if presentation is None:
            return True
        pending = self._pending_presentation
        if (
            presentation.call_id != self._pending_call_id
            or presentation.tool_name != event.tool_name
            or presentation.status
            is not _FINAL_PRESENTATION_STATUS.get(
                event.status, ToolPresentationStatus.FAILED
            )
            or (
                pending is not None
                and (
                    presentation.tool_name != pending.tool_name
                    or presentation.kind is not pending.kind
                )
            )
        ):
            self.diagnostics.append("tool presentation identity mismatch")
            return False
        return True

    def _finish_tool(self, event: ToolFinished) -> None:
        """Finalize the tool record, retaining pending state if final identity mismatches."""
        if not self._accept_final_presentation(event):
            return
        status = {
            ToolResultStatus.SUCCESS: "success",
            ToolResultStatus.CANCELLED: "cancelled",
        }.get(event.status, "failed")
        if event.presentation is not None:
            self._pending_presentation = event.presentation
        self._update_pending(self._tool_record_block(status))
        self._commit_pending()
        self._pending_call_id = None

    def _start_pending(self, kind: str) -> None:
        if self._pending_kind is not None and self._pending_kind != kind:
            self._commit_pending()
        if self._pending_kind != kind:
            self._pending_kind = kind
            self._pending_text = ""

    def _commit_pending(self) -> None:
        renderer = self._renderer
        if renderer is not None:
            if self._assistant_block is not None:
                # Finish streaming with one stable Markdown render from accumulated source.
                self._assistant_block.finish()
                renderer.set_pending(self._assistant_block)
            renderer.commit_pending()
        self._pending_kind = None
        self._pending_text = ""
        self._assistant_block = None

    def _update_pending(self, block: object) -> None:
        renderer = self._renderer
        if renderer is not None:
            renderer.set_pending(block)  # type: ignore[arg-type]

    def _live_output(self) -> str:
        """Bounded live Bash output: eight complete logical rows plus one partial row."""
        lines = [*self._tool_output]
        if self._tool_partial:
            lines.append(self._tool_partial)
        if not lines:
            return ""
        text = "\n".join(lines)
        # Preserve a trailing blank row explicitly so logical-line parsing cannot discard it.
        return f"{text}\n" if lines[-1] == "" else text

    def _live_total(self) -> int:
        """Count received logical rows, including a partial row without newline."""
        return self._tool_output_total + (1 if self._tool_partial else 0)

    def _append_tool_output(self, text: str) -> None:
        """Accumulate live output by logical row, preserving blanks and event fragments.

        Fragments from separate events rejoin the same logical row. A newline terminates
        that row while an incomplete tail waits for the next event, preventing false row
        splits and preserving consecutive blank rows.
        """
        cleaned = sanitize_text(text, newlines=True)
        if not cleaned:
            return
        parts = cleaned.split("\n")
        self._tool_partial += parts[0]
        for part in parts[1:]:
            self._tool_output.append(self._tool_partial)
            self._tool_output_total += 1
            self._tool_partial = part
        # The partial row consumes one preview slot, leaving at most seven complete rows.
        limit = MAX_OUTPUT_LINES - 1 if self._tool_partial else MAX_OUTPUT_LINES
        del self._tool_output[:-limit]

    def _tool_record_block(self, state: str) -> BlockLike:
        view = self._tool_views.create(
            self._pending_presentation,
            state=state,
            live_output=self._live_output(),
            live_total=self._live_total(),
        )
        if view is None:
            # Reject name-kind mismatch rather than silently accepting a generic projection.
            self.diagnostics.append("tool view identity mismatch")
            view = GenericToolView(self._pending_presentation, state=state)
        self._pending_tool = view
        return view

    def _permission_detail(self, event: PermissionRequested) -> str:
        if self._pending_presentation is not None:
            detail = self._presentation_detail(self._pending_presentation)
            if detail:
                return truncate_head(detail, MAX_TOOL_DETAIL * 2)
        return ""

    def _presentation_detail(
        self, presentation: ToolPresentationSnapshot | None
    ) -> str:
        if presentation is None:
            return ""
        payload = presentation.payload
        kind = presentation.kind
        if kind in {
            ToolPresentationKind.READ,
            ToolPresentationKind.WRITE,
            ToolPresentationKind.EDIT,
        }:
            return str(payload.get("path", ""))
        if kind is ToolPresentationKind.BASH:
            command = str(payload.get("command", ""))
            return command.split("\n", 1)[0]
        return ""

    def _finish_run(self, status: Status) -> None:
        self._commit_pending()
        self._pending_presentation = None
        self._pending_call_id = None
        self._pending_tool = None
        self._tool_output = []
        self._tool_output_total = 0
        self._tool_partial = ""
        if self._run_started is not None:
            self._elapsed = max(0.0, self._clock() - self._run_started)
        self._run_started = None
        self._status = status
        self._mode = "idle"
        self._ask = None
        self._permission = None
        if self._run_id is not None:
            self._terminal_run_id = self._run_id
        if status == "done":
            self._done_deadline = self._clock() + DONE_VISIBLE_SECONDS
            self._start_refresh()
        else:
            self._done_deadline = None

    def _start_refresh(self) -> None:
        """Ensure only one status-refresh task exists at a time."""
        task = self._refresh_task
        if task is not None and not task.done():
            return
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="mica-main-buffer-refresh"
        )

    async def _stop_refresh(self) -> None:
        task, self._refresh_task = self._refresh_task, None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _refresh_loop(self) -> None:
        """Sole controlled refresh task for progress animation and done expiry."""
        while not self._closed:
            await self._sleep(REFRESH_INTERVAL)
            if self._closed:
                return
            if self._status == "working":
                self.render()
                continue
            if self._status == "done":
                deadline = self._done_deadline
                if deadline is not None and self._clock() >= deadline:
                    self._status = "idle"
                    self._elapsed = None
                    self._done_deadline = None
                    self.render()
                continue
            deadline = self._command_notice_deadline
            if self._command_notice is not None:
                self.render()
                if deadline is not None and self._clock() < deadline:
                    continue
                self._command_notice = None
                self._command_notice_deadline = None
            return

    async def _refresh_branch(self) -> None:
        """Refresh the public Git branch fact without polling or private business access."""
        if self._workspace_path is None:
            return
        try:
            self._branch = await asyncio.to_thread(
                self._read_branch, self._workspace_path
            )
        except Exception as error:  # noqa: BLE001 - fact refresh failure must not stop the run
            self.diagnostics.append(f"branch refresh failed: {type(error).__name__}")

    def render(self) -> None:
        """Render one frame through the public mode-machine and test entry point."""
        renderer = self._renderer
        if renderer is None or self._closed:
            return
        self._sync_terminal_size(renderer)
        chrome, cursor, anchor = self._build_chrome()
        renderer.set_chrome(chrome, cursor=cursor, anchor=anchor)
        renderer.repaint()

    def _sync_terminal_size(self, renderer: MainBufferRenderer) -> None:
        width, height = self._terminal.size()
        if width == renderer.width and height == renderer.height:
            return
        renderer.set_height(height)
        renderer.set_width(width)

    def _build_chrome(self) -> tuple[list[str], tuple[int, int] | None, int | None]:
        """Build bottom chrome and return rows, cursor position, and selection anchor.

        Interaction panels receive both available height and selected row, allowing tall
        panels to degrade instead of being silently clipped from the top. The renderer's
        anchor is the final safeguard.
        """
        renderer = self._renderer
        assert renderer is not None
        width = renderer.width
        limit = max(1, renderer.height - 1)
        if self._mode == "ask" and self._ask is not None:
            view = self._ask.panel(width, self._palette, limit=limit)
            return view.lines, (len(view.lines) - 1, 0), view.selected_row
        if self._mode == "permission" and self._permission is not None:
            view = self._permission.panel(width, self._palette, limit=limit)
            return view.lines, (len(view.lines) - 1, 0), view.selected_row
        if self._mode == "menu" and self._menu is not None:
            view = self._menu.panel(width, self._palette, limit=limit)
            return view.lines, (len(view.lines) - 1, 0), view.selected_row
        lines: list[str] = []
        if self._configuration_notice:
            from .renderer import wrap_line

            for line in wrap_line(sanitize_text(self._configuration_notice), width):
                lines.append(self._palette.paint(line, "muted"))
        status = status_line(self._status, elapsed=self._live_elapsed())
        if status is not None:
            kind = "status_cancel" if self._status in {"stop", "error"} else "status"
            lines.append(self._palette.paint(status, kind))
        if (
            self._command_notice
            and self._command_notice_deadline is not None
            and self._clock() < self._command_notice_deadline
        ):
            from .renderer import wrap_line

            for line in wrap_line(self._command_notice, width):
                lines.append(self._palette.paint(line, "muted"))
        view = self._editor.wrapped(composer_body_width(width))
        visible, start = windowed_lines(view.lines, view.cursor_row)
        frame = composer_frame(
            visible,
            view.cursor_row - start,
            view.cursor_column,
            width=width,
            palette=self._palette,
            enabled=self._mode == "idle",
            hint=IDLE_HINT,
            hidden_above=start,
            hidden_below=max(0, len(view.lines) - start - len(visible)),
        )
        cursor = (len(lines) + frame.cursor[0], frame.cursor[1])
        lines.extend(frame.lines)
        if self._mode == "idle":
            self._candidates.sync(self._editor.submit_text())
            lines.extend(self._candidates.lines(width, self._palette))
        lines.extend(self._facts_lines())
        return lines, cursor, None

    def _facts_lines(self) -> list[str]:
        snapshot = self._session_snapshot
        renderer = self._renderer
        return facts_lines(
            getattr(snapshot, "workspace", None),
            self._branch,
            getattr(snapshot, "model", None),
            getattr(snapshot, "session_label", None),
            width=renderer.width if renderer is not None else 80,
            palette=self._palette,
        )

    def _live_elapsed(self) -> float | None:
        if self._run_started is not None:
            return max(0.0, self._clock() - self._run_started)
        return self._elapsed


__all__ = [
    "CHECKPOINT_INCOMPLETE_MARKER",
    "MainBufferScreen",
    "Mode",
    "Status",
]
