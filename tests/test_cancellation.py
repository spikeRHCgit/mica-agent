"""统一取消信号测试。"""

from __future__ import annotations

import asyncio

import pytest

from mica_agent.cancellation import CancellationRequested, CancellationToken


def test_token_can_be_created_without_a_running_event_loop() -> None:
    token = CancellationToken()

    assert token.is_cancelled is False
    token.raise_if_cancelled()


def test_wait_remains_pending_until_cancelled() -> None:
    async def scenario() -> None:
        token = CancellationToken()
        waiter = asyncio.create_task(token.wait())
        await asyncio.sleep(0)

        assert waiter.done() is False
        token.cancel()
        await asyncio.wait_for(waiter, timeout=0.1)

    asyncio.run(scenario())


def test_cancel_is_idempotent_and_wait_after_cancel_is_immediate() -> None:
    async def scenario() -> None:
        token = CancellationToken()
        assert token.cancel() is None
        assert token.cancel() is None

        assert token.is_cancelled is True
        await asyncio.wait_for(token.wait(), timeout=0.1)

    asyncio.run(scenario())


def test_cancel_wakes_all_waiters() -> None:
    async def scenario() -> None:
        token = CancellationToken()
        waiters = [asyncio.create_task(token.wait()) for _ in range(3)]
        await asyncio.sleep(0)

        assert not any(waiter.done() for waiter in waiters)
        token.cancel()
        await asyncio.wait_for(asyncio.gather(*waiters), timeout=0.1)
        assert all(waiter.done() for waiter in waiters)

    asyncio.run(scenario())


def test_raise_if_cancelled_uses_fixed_safe_exception() -> None:
    token = CancellationToken()
    token.raise_if_cancelled()
    token.cancel()

    with pytest.raises(CancellationRequested, match=r"^operation cancelled$"):
        token.raise_if_cancelled()
