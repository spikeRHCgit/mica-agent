"""MicaApp 公共行为测试（P2-03）。

最小 Provider 与 httpx.MockTransport 均只存在于测试文件内；
跨层链路（MockTransport → OpenAICompatibleProvider → AgentEngine
→ MicaApp → EngineEvent → 进程内 history）在文件末尾验证。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from mica_agent.app import AppBusyError, AppClosedError, MicaApp
from mica_agent.events import (
    EngineEvent,
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
    OpenAICompatibleProvider,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderRequest,
)
from mica_agent.providers import (
    TextDelta as ProviderTextDelta,
)


class _ScriptedProvider:
    """按请求次序产出固定事件序列的最小 Provider（本文件专用）。"""

    def __init__(self, scripts: list[list[ProviderEvent]]) -> None:
        self._scripts = scripts
        self.requests: list[ProviderRequest] = []
        self.aclose_calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        events = self._scripts[min(len(self._scripts) - 1, len(self.requests) - 1)]
        for event in events:
            yield event

    async def aclose(self) -> None:
        self.aclose_calls += 1


def _make_app(
    provider: _ScriptedProvider,
    *,
    model: str = "test-model",
    system_prompt: str | None = None,
) -> MicaApp:
    return MicaApp(
        provider=provider,
        model=model,
        system_prompt=system_prompt,
        run_id_factory=lambda: "run-test",
    )


async def _collect(
    app: MicaApp,
    prompt: str,
    collected: list[EngineEvent] | None = None,
) -> list[EngineEvent]:
    if collected is None:
        collected = []
    async for event in app.run(prompt):
        collected.append(event)
    return collected


def _run_collect(app: MicaApp, prompt: str) -> list[EngineEvent]:
    return asyncio.run(_collect(app, prompt))


def _user(content: str) -> Message:
    return Message(role=MessageRole.USER, content=content)


def _assistant(content: str) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=content)


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["", "   ", "\t\n"])
def test_blank_model_is_rejected(model: str) -> None:
    with pytest.raises(ValueError):
        _make_app(_ScriptedProvider([[]]), model=model)


def test_non_str_model_is_rejected() -> None:
    with pytest.raises(TypeError):
        _make_app(_ScriptedProvider([[]]), model=123)  # type: ignore[arg-type]


def test_non_str_system_prompt_is_rejected() -> None:
    with pytest.raises(TypeError):
        _make_app(_ScriptedProvider([[]]), system_prompt=42)  # type: ignore[arg-type]


@pytest.mark.parametrize("prompt", ["", "   ", "\t\n"])
def test_blank_prompt_is_rejected_before_provider(prompt: str) -> None:
    provider = _ScriptedProvider([[]])
    app = _make_app(provider)

    with pytest.raises(ValueError):
        _run_collect(app, prompt)

    assert provider.requests == []  # 网络请求前拒绝


def test_non_str_prompt_is_rejected_before_provider() -> None:
    provider = _ScriptedProvider([[]])
    app = _make_app(provider)

    with pytest.raises(TypeError):
        _run_collect(app, 123)  # type: ignore[arg-type]

    assert provider.requests == []


# ---------------------------------------------------------------------------
# 成功 Run 与原子提交
# ---------------------------------------------------------------------------


def test_successful_run_commits_user_and_assistant_atomically() -> None:
    provider = _ScriptedProvider(
        [
            [
                ProviderTextDelta(text="hello"),
                ProviderTextDelta(text=" world"),
                Finish(),
            ]
        ]
    )
    app = _make_app(provider)

    events = _run_collect(app, "first prompt")

    assert events == [
        RunStarted(run_id="run-test"),
        TextDelta(run_id="run-test", text="hello", seq=1),
        TextDelta(run_id="run-test", text=" world", seq=2),
        RunCompleted(run_id="run-test", seq=3),
    ]
    assert app.history == (_user("first prompt"), _assistant("hello world"))
    assert provider.requests[0].messages == (_user("first prompt"),)


def test_successful_run_without_text_commits_empty_assistant() -> None:
    provider = _ScriptedProvider([[Finish()]])
    app = _make_app(provider)

    _run_collect(app, "empty reply")

    assert app.history == (_user("empty reply"), _assistant(""))


def test_system_prompt_is_first_and_appears_once() -> None:
    provider = _ScriptedProvider(
        [
            [Finish()],
            [ProviderTextDelta(text="ok"), Finish()],
        ]
    )
    app = _make_app(provider, system_prompt="be brief")

    _run_collect(app, "q1")
    _run_collect(app, "q2")

    assert provider.requests[0].messages == (
        Message(role=MessageRole.SYSTEM, content="be brief"),
        _user("q1"),
    )
    assert provider.requests[1].messages == (
        Message(role=MessageRole.SYSTEM, content="be brief"),
        _user("q1"),
        _assistant(""),
        _user("q2"),
    )
    assert [
        message for message in app.history if message.role is MessageRole.SYSTEM
    ] == [Message(role=MessageRole.SYSTEM, content="be brief")]


# ---------------------------------------------------------------------------
# 失败 / 取消 / 提前关闭时历史回滚
# ---------------------------------------------------------------------------


def test_failed_run_rolls_back_history() -> None:
    provider = _ScriptedProvider(
        [
            [
                ProviderTextDelta(text="partial"),
                ProviderErrorEvent(error=AuthenticationError("auth")),
            ]
        ]
    )
    app = _make_app(provider)

    events = _run_collect(app, "question")

    assert isinstance(events[-1], RunFailed)
    assert app.history == ()  # 用户消息与部分文本都不提交
    assert provider.requests[0].messages == (_user("question"),)


def test_cancelled_run_rolls_back_history() -> None:
    class HangingProvider:
        def __init__(self) -> None:
            self.requests: list[ProviderRequest] = []

        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            self.requests.append(request)
            yield ProviderTextDelta(text="partial")
            await asyncio.sleep(30)
            yield Finish()

        async def aclose(self) -> None:
            return None

    app = _make_app(HangingProvider())  # type: ignore[arg-type]
    collected: list[EngineEvent] = []

    async def main() -> None:
        task = asyncio.create_task(_collect(app, "question", collected))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    assert isinstance(collected[-1], RunCancelled)
    assert app.history == ()  # 取消不提交任何内容


def test_consumer_abandoning_stream_rolls_back_history() -> None:
    provider = _ScriptedProvider(
        [
            [
                ProviderTextDelta(text="partial"),
                ProviderTextDelta(text=" more"),
                Finish(),
            ]
        ]
    )
    app = _make_app(provider)

    async def main() -> None:
        collected: list[EngineEvent] = []
        async for event in app.run("question"):
            collected.append(event)
            break  # 消费者提前关闭事件流

    asyncio.run(main())

    assert app.history == ()  # 未收到 RunCompleted → 全部回滚


# ---------------------------------------------------------------------------
# 多轮上下文
# ---------------------------------------------------------------------------


def test_second_run_carries_full_committed_context() -> None:
    provider = _ScriptedProvider(
        [
            [ProviderTextDelta(text="answer one"), Finish()],
            [ProviderTextDelta(text="answer two"), Finish()],
        ]
    )
    app = _make_app(provider)

    _run_collect(app, "question one")
    _run_collect(app, "question two")

    assert len(provider.requests) == 2
    assert provider.requests[0].messages == (_user("question one"),)
    assert provider.requests[1].messages == (
        _user("question one"),
        _assistant("answer one"),
        _user("question two"),
    )
    assert app.history == (
        _user("question one"),
        _assistant("answer one"),
        _user("question two"),
        _assistant("answer two"),
    )


def test_failed_run_does_not_break_following_successful_run() -> None:
    provider = _ScriptedProvider(
        [
            [ProviderErrorEvent(error=AuthenticationError("auth"))],
            [ProviderTextDelta(text="retry ok"), Finish()],
        ]
    )
    app = _make_app(provider)

    _run_collect(app, "first")
    _run_collect(app, "second")

    # 失败轮未提交，第二轮请求只有自己的用户消息。
    assert provider.requests[1].messages == (_user("second"),)
    assert app.history == (_user("second"), _assistant("retry ok"))


# ---------------------------------------------------------------------------
# history 快照
# ---------------------------------------------------------------------------


def test_history_returns_immutable_snapshot() -> None:
    provider = _ScriptedProvider([[ProviderTextDelta(text="ok"), Finish()]])
    app = _make_app(provider)

    _run_collect(app, "q")
    snapshot = app.history

    assert isinstance(snapshot, tuple)
    assert snapshot == (_user("q"), _assistant("ok"))
    # 快照是独立对象：重复取值得到相同内容，但内部历史可继续增长。
    assert app.history == snapshot
    _run_collect(app, "q2")
    assert snapshot == (_user("q"), _assistant("ok"))  # 旧快照不受影响
    assert app.history == (_user("q"), _assistant("ok"), _user("q2"), _assistant("ok"))


# ---------------------------------------------------------------------------
# 并发与关闭
# ---------------------------------------------------------------------------


def test_second_run_is_rejected_while_first_is_active() -> None:
    class SlowProvider:
        def __init__(self) -> None:
            self.requests: list[ProviderRequest] = []

        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            self.requests.append(request)
            yield ProviderTextDelta(text="first")
            await asyncio.sleep(30)
            yield Finish()

        async def aclose(self) -> None:
            return None

    app = _make_app(SlowProvider())  # type: ignore[arg-type]
    first_events: list[EngineEvent] = []

    async def first_run() -> None:
        async for event in app.run("first"):
            first_events.append(event)

    async def main() -> None:
        task = asyncio.create_task(first_run())
        await asyncio.sleep(0.05)  # 等待第一个 Run 进入活动状态
        with pytest.raises(AppBusyError):
            async for _ in app.run("second"):  # 第二个 Run 必须失败
                pass
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    # 第一个 Run 未被破坏：文本事件已产出，历史未因并发尝试而修改。
    assert any(isinstance(event, TextDelta) for event in first_events)
    assert app.history == ()


def test_run_allowed_again_after_previous_run_terminates() -> None:
    provider = _ScriptedProvider(
        [
            [Finish()],
            [ProviderTextDelta(text="ok"), Finish()],
        ]
    )
    app = _make_app(provider)

    _run_collect(app, "one")
    events = _run_collect(app, "two")  # 第一个 Run 终止后允许再次运行

    assert isinstance(events[-1], RunCompleted)
    assert app.history == (_user("one"), _assistant(""), _user("two"), _assistant("ok"))


def test_run_allowed_after_failed_run_releases_active_state() -> None:
    provider = _ScriptedProvider(
        [
            [ProviderErrorEvent(error=AuthenticationError("auth"))],
            [Finish()],
        ]
    )
    app = _make_app(provider)

    _run_collect(app, "one")
    events = _run_collect(app, "two")

    assert isinstance(events[-1], RunCompleted)


def test_aclose_closes_provider_and_blocks_further_runs() -> None:
    provider = _ScriptedProvider([[Finish()]])
    app = _make_app(provider)

    _run_collect(app, "q")
    asyncio.run(app.aclose())

    assert provider.aclose_calls == 1
    with pytest.raises(AppClosedError):
        _run_collect(app, "after close")
    assert len(provider.requests) == 1  # 关闭后未访问 Provider


def test_aclose_is_idempotent() -> None:
    provider = _ScriptedProvider([[Finish()]])
    app = _make_app(provider)

    asyncio.run(app.aclose())
    asyncio.run(app.aclose())

    assert provider.aclose_calls == 1  # 第二次调用不重复关闭


def test_closed_app_never_touches_provider() -> None:
    provider = _ScriptedProvider([[Finish()]])
    app = _make_app(provider)
    asyncio.run(app.aclose())

    with pytest.raises(AppClosedError):
        _run_collect(app, "late")  # type: ignore[arg-type]

    assert provider.requests == []


# ---------------------------------------------------------------------------
# 跨层链路：MockTransport → OpenAICompatibleProvider → Engine → App
# ---------------------------------------------------------------------------


def test_full_stack_via_openai_provider_and_mock_transport() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))  # type: ignore[arg-type]
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"!"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
            request=request,
        )

    provider = OpenAICompatibleProvider(
        api_key="sk-test-key",
        base_url="https://api.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    app = MicaApp(
        provider=provider,
        model="test-model",
        system_prompt="be brief",
        run_id_factory=lambda: "run-test",
    )

    first = _run_collect(app, "hello")
    second = _run_collect(app, "again")

    # 事件流透传 Engine 事件；两轮都以 RunCompleted 结束。
    assert isinstance(first[-1], RunCompleted)
    assert isinstance(second[-1], RunCompleted)
    assert [event.text for event in first if isinstance(event, TextDelta)] == [
        "hi",
        "!",
    ]
    # 第二轮请求携带完整前文；system prompt 始终在最前且只出现一次。
    assert captured[0]["messages"] == [  # type: ignore[index]
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
    ]
    assert captured[1]["messages"] == [  # type: ignore[index]
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi!"},
        {"role": "user", "content": "again"},
    ]
    # 进程内 history 与提交规则一致。
    assert app.history == (
        Message(role=MessageRole.SYSTEM, content="be brief"),
        _user("hello"),
        _assistant("hi!"),
        _user("again"),
        _assistant("hi!"),
    )

    asyncio.run(app.aclose())
