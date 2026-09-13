"""主缓冲区前端的单 Run 生命周期控制器。

集中拥有唯一活动 Run 的 ``CancellationToken``、消费任务、真实
``run_id`` 和 ``MicaApp.run()`` 事件流，并转发公共交互响应。它不持有
终端、不绘制。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mica_agent.app import MicaApp

from mica_agent.cancellation import CancellationToken
from mica_agent.events import (
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
)
from mica_agent.interactions import PermissionResponse, UserInputResponse

EventSink = Callable[[object], Awaitable[None]]


@dataclass
class _ActiveRun:
    token: CancellationToken
    task: asyncio.Task[None] | None = None
    run_id: str | None = None
    terminal: bool = False
    interrupted: bool = True


class MainBufferRuntime:
    """拥有一个已装配 ``MicaApp`` 的单 Run 控制器。"""

    def __init__(self, app: MicaApp, *, event_sink: EventSink) -> None:
        from mica_agent.app import MicaApp as App

        if not isinstance(app, App):
            raise TypeError("app must be a MicaApp")
        if not callable(event_sink):
            raise TypeError("event_sink must be callable")
        self._app = app
        self._sink = event_sink
        self._active: _ActiveRun | None = None
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def run_id(self) -> str | None:
        return self._active.run_id if self._active is not None else None

    @property
    def is_closed(self) -> bool:
        return self._closing

    @property
    def active_task(self) -> asyncio.Task[None] | None:
        return self._active.task if self._active is not None else None

    def submit(self, prompt: str) -> bool:
        """接受一次完整提交；关闭、已有活动 Run 或空白输入时拒绝。"""
        if (
            self._closing
            or self._active is not None
            or not isinstance(prompt, str)
            or not prompt.strip()
        ):
            return False
        state = _ActiveRun(CancellationToken())
        self._active = state
        state.task = asyncio.create_task(
            self._consume(prompt, state), name="mica-main-buffer-run"
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

    def cancel(self, run_id: str | None = None) -> bool:
        """请求取消当前活动 Run；``run_id`` 为空时取消当前 Run。"""
        state = self._active
        if self._closing or state is None or state.terminal or state.token.is_cancelled:
            return False
        if run_id is not None and state.run_id is not None and state.run_id != run_id:
            return False
        state.token.cancel()
        return True

    async def _consume(self, prompt: str, state: _ActiveRun) -> None:
        reason = "stream_ended"
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
                    await self._sink(event)
                    if state.terminal:
                        state.interrupted = False
                        break
        except asyncio.CancelledError:
            state.token.cancel()
            state.interrupted = True
            reason = "cancelled"
            raise
        except Exception:  # noqa: BLE001 - 后端异常不得逃逸到前端循环
            state.token.cancel()
            state.interrupted = True
            reason = "runtime_error"
        finally:
            if state.interrupted and not self._closing:
                if reason == "stream_ended" and state.token.is_cancelled:
                    reason = "cancelled"
                await self._sink(InterruptedMarker(reason))

    def _release(self, state: _ActiveRun, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        if self._active is state:
            self._active = None
            state.task = None

    async def wait_idle(self) -> None:
        """等待当前消费任务结束，不取消它。"""
        state = self._active
        if state is not None and state.task is not None:
            task = state.task
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            self._release(state, task)

    def request_shutdown(self) -> None:
        self._closing = True
        if self._active is not None:
            self._active.token.cancel()

    async def aclose(self) -> None:
        """幂等关闭：取消活动 Run 后关闭 MicaApp。"""
        self.request_shutdown()
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name="mica-main-buffer-close"
            )
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
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except TimeoutError:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self._release(state, task)
        finally:
            try:
                await self._app.aclose()
            except Exception:  # noqa: BLE001, S110 - Provider 关闭诊断保持私有
                pass


@dataclass(frozen=True, slots=True)
class InterruptedMarker:
    """内部标记：消费任务在未收到公共终态事件时结束。

    只携带最小终止类别，不包含异常对象、消息、路径或任何后端细节：

    * ``cancelled``——使用者取消（Esc / Ctrl+C）；
    * ``runtime_error``——Provider 或消费链抛异常；
    * ``stream_ended``——事件流在没有终态事件的情况下结束。
    """

    reason: str = "stream_ended"


__all__ = ["InterruptedMarker", "MainBufferRuntime"]
