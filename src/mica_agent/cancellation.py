"""Provider、Engine 与工具共享的统一取消信号。"""

from __future__ import annotations

import asyncio

_CANCELLED_MESSAGE = "operation cancelled"


class CancellationRequested(Exception):
    """协作式取消令牌已发出取消信号。"""


class CancellationToken:
    """可重复发出、可供多个协程等待的一次性取消信号。"""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        """取消信号是否已经发出。"""
        return self._event.is_set()

    def cancel(self) -> None:
        """发出取消信号；重复调用保持已取消状态。"""
        self._event.set()

    async def wait(self) -> None:
        """异步等待取消信号。"""
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        """已取消时抛出固定、安全的取消异常。"""
        if self.is_cancelled:
            raise CancellationRequested(_CANCELLED_MESSAGE)
