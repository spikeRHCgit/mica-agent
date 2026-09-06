"""Owned TUI run consumption and deterministic MicaApp shutdown."""

from __future__ import annotations

import asyncio
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass

from mica_agent.app import MicaApp
from mica_agent.cancellation import CancellationToken
from mica_agent.events import RunCancelled, RunCompleted, RunFailed, RunStarted
from mica_agent.interactions import PermissionResponse, UserInputResponse

from .app import MicaTuiApp


@dataclass
class _ActiveRun:
    token: CancellationToken
    task: asyncio.Task[None] | None = None
    run_id: str | None = None
    terminal: bool = False
    interrupted: bool = True


class MicaTuiRuntime:
    """Own one assembled MicaApp and its TUI for a single UI lifetime.

    Use ``await runtime.tui.run_async()`` or ``async with
    runtime.tui.run_test()``. Both await ``aclose()`` on exit. Direct owners
    may also call ``aclose()``; it stops input and joins the current consumer
    before closing MicaApp. No CLI routing or backend assembly happens here.
    """

    def __init__(self, app: MicaApp, **tui_options) -> None:
        if not isinstance(app, MicaApp):
            raise TypeError("app must be a MicaApp")
        self._app = app
        self._active: _ActiveRun | None = None
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        self.tui = _RuntimeTuiApp(self, **tui_options)

    @property
    def active_task(self) -> asyncio.Task[None] | None:
        """The owned consumer, exposed for joining or external cancellation."""
        return self._active.task if self._active is not None else None

    @property
    def run_id(self) -> str | None:
        """The actual RunStarted identity, never a frontend-generated ID."""
        return self._active.run_id if self._active is not None else None

    @property
    def is_closed(self) -> bool:
        return self._closing

    def submit(self, prompt: str) -> bool:
        """Accept a full prompt synchronously, at most once until cleanup ends."""
        if (
            self._closing
            or self._active is not None
            or not self.tui.is_running
            or not isinstance(prompt, str)
            or not prompt.strip()
        ):
            return False
        state = _ActiveRun(CancellationToken())
        self._active = state
        self.tui.hold_run_input()
        state.task = asyncio.create_task(
            self._consume(prompt, state), name="mica-tui-run"
        )
        state.task.add_done_callback(lambda task: self._release(state, task))
        return True

    def respond_interaction(
        self, response: PermissionResponse | UserInputResponse
    ) -> bool:
        state = self._active
        if (
            self._closing
            or state is None
            or state.terminal
            or state.run_id is None
            or state.token.is_cancelled
        ):
            return False
        return self._app.respond_interaction(response) is True

    def cancel(self, run_id: str) -> bool:
        """Request the first cancellation of the current matching real Run."""
        state = self._active
        if (
            self._closing
            or state is None
            or state.run_id is None
            or state.run_id != run_id
            or state.terminal
            or state.token.is_cancelled
        ):
            return False
        state.token.cancel()
        return True

    async def _consume(self, prompt: str, state: _ActiveRun) -> None:
        try:
            async with aclosing(
                self._app.run(prompt, cancellation=state.token)
            ) as stream:
                async for event in stream:
                    if self._closing:
                        break
                    if isinstance(event, RunStarted):
                        state.run_id = event.run_id
                    state.terminal = isinstance(
                        event, (RunCompleted, RunFailed, RunCancelled)
                    )
                    await self.tui.project_engine_event(event)
                    if state.terminal:
                        state.interrupted = False
                        break
        except asyncio.CancelledError:
            state.token.cancel()
            state.interrupted = True
            raise
        except Exception:  # noqa: BLE001 - no backend or projection exception escapes to Textual.
            state.token.cancel()
            state.interrupted = True
        finally:
            if state.interrupted and not self._closing:
                try:
                    await self.tui.finish_interrupted_output(state.run_id)
                except Exception:  # noqa: BLE001 - best-effort visual freeze.
                    state.interrupted = True

    def _release(self, state: _ActiveRun, task: asyncio.Task[None]) -> None:
        # A done callback also handles cancellation before the coroutine starts.
        if not task.cancelled():
            task.exception()
        if self._active is state:
            self._active = None
            state.task = None
            self.tui.release_run_input(state.run_id, interrupted=state.interrupted)

    async def wait_idle(self) -> None:
        """Join the current consumer without cancelling it when the waiter exits."""
        state = self._active
        if state is not None and state.task is not None:
            task = state.task
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            self._release(state, task)

    def request_shutdown(self) -> None:
        """Stop callbacks now; the lifecycle wrapper subsequently awaits aclose."""
        self._closing = True
        self.tui.stop_runtime_input()
        if self._active is not None:
            self._active.token.cancel()

    async def aclose(self) -> None:
        """Join cleanup even if the closing caller is cancelled; repeat-safe."""
        self.request_shutdown()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="mica-tui-close")
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        state = self._active
        try:
            if state is not None and state.task is not None:
                task = state.task
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=1.0,
                    )
                except TimeoutError:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self._release(state, task)
        finally:
            try:
                self.tui.clear_runtime_interactions()
            finally:
                try:
                    await self._app.aclose()
                except Exception:  # noqa: BLE001 - provider shutdown diagnostics stay private.
                    self.tui.release_run_input(None, interrupted=True)


class _RuntimeTuiApp(MicaTuiApp):
    """Lifecycle wrapper only; all Run state remains in MicaTuiRuntime."""

    def __init__(self, runtime: MicaTuiRuntime, **options) -> None:
        self._runtime = runtime
        super().__init__(
            run_requester=runtime.submit,
            permission_responder=runtime.respond_interaction,
            user_input_responder=runtime.respond_interaction,
            run_cancel_requester=runtime.cancel,
            **options,
        )

    def exit(self, result=None, return_code: int = 0, message=None) -> None:
        self._runtime.request_shutdown()
        super().exit(result, return_code, message)

    async def run_async(self, **kwargs):
        try:
            return await super().run_async(**kwargs)
        finally:
            await self._runtime.aclose()

    @asynccontextmanager
    async def run_test(self, **kwargs):
        try:
            async with super().run_test(**kwargs) as pilot:
                try:
                    yield pilot
                finally:
                    await self._runtime.aclose()
        finally:
            await self._runtime.aclose()


__all__ = ["MicaTuiRuntime"]
