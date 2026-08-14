"""AgentEngine 公共行为测试（P2-03）。

最小 Provider 只存在于本测试文件内，用于固定事件序列、
错误映射和精确取消时机；真实跨层链路在 test_app.py 验证。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from mica_agent.engine import AgentEngine
from mica_agent.events import (
    EngineEvent,
    ErrorCategory,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
)
from mica_agent.models import Message, MessageRole
from mica_agent.providers import (
    AuthenticationError,
    Finish,
    MalformedResponseError,
    ProviderError,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderRequest,
    ProviderTimeoutError,
    RateLimitError,
    TemporaryServerError,
)
from mica_agent.providers import (
    TextDelta as ProviderTextDelta,
)


def _request() -> ProviderRequest:
    return ProviderRequest(
        model="test-model",
        messages=(Message(role=MessageRole.USER, content="hi"),),
    )


class _StreamingProvider:
    """按给定列表产出 Provider 事件的最小实现（本文件专用）。"""

    def __init__(self, events: list[ProviderEvent]) -> None:
        self._events = events
        self.consumed = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        for event in self._events:
            self.consumed += 1
            yield event


def _engine(events: list[ProviderEvent], *, run_id: str = "run-test") -> AgentEngine:
    return AgentEngine(
        provider=_StreamingProvider(events),
        run_id_factory=lambda: run_id,
    )


async def _collect(
    engine: AgentEngine,
    request: ProviderRequest,
    collected: list[EngineEvent] | None = None,
) -> list[EngineEvent]:
    if collected is None:
        collected = []
    async for event in engine.run(request):
        collected.append(event)
    return collected


def _run_collect(engine: AgentEngine, request: ProviderRequest) -> list[EngineEvent]:
    return asyncio.run(_collect(engine, request))


# ---------------------------------------------------------------------------
# 正常 Run 事件序列
# ---------------------------------------------------------------------------


def test_normal_text_run_yields_strictly_sequenced_events() -> None:
    engine = _engine(
        [
            ProviderTextDelta(text="hello"),
            ProviderTextDelta(text=" world"),
            Finish(),
        ]
    )

    events = _run_collect(engine, _request())

    assert events == [
        RunStarted(run_id="run-test"),  # seq 固定 0
        TextDelta(run_id="run-test", text="hello", seq=1),
        TextDelta(run_id="run-test", text=" world", seq=2),
        RunCompleted(run_id="run-test", seq=3),
    ]
    assert [event.seq for event in events] == [0, 1, 2, 3]


def test_run_without_text_goes_straight_from_started_to_completed() -> None:
    engine = _engine([Finish()])

    events = _run_collect(engine, _request())

    assert events == [
        RunStarted(run_id="run-test"),
        RunCompleted(run_id="run-test", seq=1),
    ]


def test_engine_stops_consuming_provider_after_terminal_event() -> None:
    provider = _StreamingProvider(
        [
            ProviderTextDelta(text="done"),
            Finish(),
            # 终态之后的事件必须不被消费。
            ProviderTextDelta(text="ghost"),
        ]
    )
    engine = AgentEngine(provider=provider, run_id_factory=lambda: "run-test")

    events = _run_collect(engine, _request())

    assert provider.consumed == 2  # 只消费到 Finish
    assert events == [
        RunStarted(run_id="run-test"),
        TextDelta(run_id="run-test", text="done", seq=1),
        RunCompleted(run_id="run-test", seq=2),
    ]


def test_every_run_has_exactly_one_terminal_event() -> None:
    scenarios: list[list[ProviderEvent]] = [
        [Finish()],
        [ProviderTextDelta(text="a"), Finish()],
        [ProviderErrorEvent(error=AuthenticationError("authentication failed"))],
    ]

    for events in scenarios:
        collected = _run_collect(_engine(events), _request())
        terminal = [
            event
            for event in collected
            if isinstance(event, (RunCompleted, RunFailed, RunCancelled))
        ]
        assert len(terminal) == 1


# ---------------------------------------------------------------------------
# ProviderError 到 ErrorCategory 映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider_error", "category"),
    [
        (AuthenticationError("authentication failed"), ErrorCategory.AUTHENTICATION),
        (RateLimitError("rate limited"), ErrorCategory.RATE_LIMIT),
        (ProviderTimeoutError("timed out"), ErrorCategory.TIMEOUT),
        (TemporaryServerError("server hiccup"), ErrorCategory.TEMPORARY),
        (MalformedResponseError("bad chunk"), ErrorCategory.MALFORMED),
    ],
)
def test_classified_provider_errors_map_to_categories(
    provider_error: ProviderError, category: ErrorCategory
) -> None:
    engine = _engine([ProviderErrorEvent(error=provider_error)])

    events = _run_collect(engine, _request())

    assert [type(event).__name__ for event in events] == [
        "RunStarted",
        "RunFailed",
    ]
    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is category
    assert failed.seq == 1
    assert failed.message == str(provider_error)  # 使用已清洗的公开诊断


def test_unknown_provider_error_maps_to_unknown_category() -> None:
    engine = _engine([ProviderErrorEvent(error=ProviderError("mystery"))])

    events = _run_collect(engine, _request())

    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.UNKNOWN
    assert failed.message == "mystery"


def test_non_provider_error_object_maps_to_safe_unknown() -> None:
    engine = _engine([ProviderErrorEvent(error="not an error")])  # type: ignore[arg-type]

    events = _run_collect(engine, _request())

    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.UNKNOWN
    assert "not an error" not in failed.message


def test_credentials_never_leak_through_run_failed_message() -> None:
    error = AuthenticationError(
        "request rejected: Authorization: Bearer sk-live-secret-token"
    )
    engine = _engine([ProviderErrorEvent(error=error)])

    events = _run_collect(engine, _request())

    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert "sk-live-secret-token" not in failed.message
    assert "Bearer" in failed.message  # 保留清洗后的可读诊断


# ---------------------------------------------------------------------------
# 畸形与未知事件
# ---------------------------------------------------------------------------


def test_unknown_event_object_maps_to_safe_unknown_failure() -> None:
    class WeirdProvider:
        async def stream(self, request: ProviderRequest) -> AsyncIterator[object]:
            yield "not-a-provider-event"

    engine = AgentEngine(provider=WeirdProvider(), run_id_factory=lambda: "run-test")

    events = _run_collect(engine, _request())

    assert [type(event).__name__ for event in events] == [
        "RunStarted",
        "RunFailed",
    ]
    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.UNKNOWN
    assert "not-a-provider-event" not in failed.message


def test_non_string_text_delta_maps_to_malformed() -> None:
    class OddDeltaProvider:
        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            yield ProviderTextDelta(text=123)  # type: ignore[arg-type]
            yield Finish()

    engine = AgentEngine(provider=OddDeltaProvider(), run_id_factory=lambda: "run-test")

    events = _run_collect(engine, _request())

    # 非字符串文本不得构造非法 Engine TextDelta。
    assert [type(event).__name__ for event in events] == [
        "RunStarted",
        "RunFailed",
    ]
    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.MALFORMED
    assert "123" not in failed.message


def test_provider_stream_ending_without_terminal_maps_to_malformed() -> None:
    engine = _engine([ProviderTextDelta(text="partial")])

    events = _run_collect(engine, _request())

    assert [type(event).__name__ for event in events] == [
        "RunStarted",
        "TextDelta",
        "RunFailed",
    ]
    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.MALFORMED
    assert "partial" not in failed.message


# ---------------------------------------------------------------------------
# 未预期异常
# ---------------------------------------------------------------------------


def test_unexpected_provider_exception_maps_to_safe_unknown() -> None:
    class BrokenProvider:
        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            yield ProviderTextDelta(text="partial")
            raise RuntimeError("secret internal detail: sk-abc12345")

    engine = AgentEngine(provider=BrokenProvider(), run_id_factory=lambda: "run-test")

    events = _run_collect(engine, _request())

    assert [type(event).__name__ for event in events] == [
        "RunStarted",
        "TextDelta",
        "RunFailed",
    ]
    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.UNKNOWN
    assert "secret internal detail" not in failed.message
    assert "sk-abc12345" not in failed.message
    assert "RuntimeError" not in failed.message
    assert failed.message  # 固定、安全、简短的公开诊断


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


class _HangingProvider:
    """产出首个事件后挂起，等待外部取消。"""

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        yield ProviderTextDelta(text="first")
        await asyncio.sleep(30)
        yield Finish()


def test_cancellation_maps_to_single_runcancelled() -> None:
    engine = AgentEngine(provider=_HangingProvider(), run_id_factory=lambda: "run-test")
    collected: list[EngineEvent] = []

    async def consume() -> None:
        async for event in engine.run(_request()):
            collected.append(event)

    async def main() -> None:
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    # 取消映射为唯一 RunCancelled；无 RunFailed / RunCompleted，
    # 且取消后不再追加文本事件。
    assert [type(event).__name__ for event in collected] == [
        "RunStarted",
        "TextDelta",
        "RunCancelled",
    ]
    assert not any(isinstance(event, (RunCompleted, RunFailed)) for event in collected)
