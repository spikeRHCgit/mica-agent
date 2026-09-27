"""Unified cancellation signal shared by providers, the engine, and tools."""

from __future__ import annotations

import asyncio

_CANCELLED_MESSAGE = "operation cancelled"


class CancellationRequested(Exception):
    """A cooperative cancellation token has raised its cancellation signal."""


class CancellationToken:
    """One-shot cancellation signal that may be signalled repeatedly and awaited by many
    coroutines."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        """Whether the cancellation signal has been raised."""
        return self._event.is_set()

    def cancel(self) -> None:
        """Raise the cancellation signal; repeated calls keep the cancelled state."""
        self._event.set()

    async def wait(self) -> None:
        """Await the cancellation signal."""
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        """Raise a fixed, safe cancellation error when already cancelled."""
        if self.is_cancelled:
            raise CancellationRequested(_CANCELLED_MESSAGE)
