"""Provider 公共契约测试（阶段计划 4.2）。

验证：请求对象承载模型 ID 与标准消息历史且不可变；流式事件的
文本增量/正常结束/错误类别可判别；错误对象只携带面向用户的诊断。
不包含任何 FakeProvider——传输边界测试由 P2-02 负责。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from typing import get_args

import pytest

from mica_agent.models import Message, MessageRole
from mica_agent.providers import (
    AuthenticationError,
    Finish,
    MalformedResponseError,
    Provider,
    ProviderError,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderRequest,
    ProviderTimeoutError,
    RateLimitError,
    TemporaryServerError,
    TextDelta,
)


def build_request() -> ProviderRequest:
    return ProviderRequest(
        model="test-model",
        messages=(
            Message(role=MessageRole.SYSTEM, content="be brief"),
            Message(role=MessageRole.USER, content="hi"),
        ),
    )


def test_request_carries_model_and_message_history() -> None:
    request = build_request()

    assert request.model == "test-model"
    assert request.messages == (
        Message(role=MessageRole.SYSTEM, content="be brief"),
        Message(role=MessageRole.USER, content="hi"),
    )


def test_request_messages_are_immutable_tuple() -> None:
    request = build_request()

    assert isinstance(request.messages, tuple)
    with pytest.raises(AttributeError):
        request.messages.append(Message(role=MessageRole.USER, content="x"))  # type: ignore[attr-defined]


def test_request_is_immutable() -> None:
    request = build_request()

    with pytest.raises(FrozenInstanceError):
        request.model = "other"  # type: ignore[misc]


@pytest.mark.parametrize("model", ["", "   ", "\t\n"])
def test_request_rejects_blank_model(model: str) -> None:
    with pytest.raises(ValueError):
        ProviderRequest(
            model=model,
            messages=(Message(role=MessageRole.USER, content="hi"),),
        )


def test_request_rejects_non_str_model() -> None:
    with pytest.raises(TypeError):
        ProviderRequest(  # type: ignore[arg-type]
            model=123,
            messages=(Message(role=MessageRole.USER, content="hi"),),
        )


def test_request_rejects_empty_messages() -> None:
    with pytest.raises(ValueError):
        ProviderRequest(model="test-model", messages=())


def test_request_rejects_non_tuple_messages() -> None:
    with pytest.raises(TypeError):
        ProviderRequest(  # type: ignore[arg-type]
            model="test-model",
            messages=[Message(role=MessageRole.USER, content="hi")],
        )


def test_request_rejects_non_message_element() -> None:
    with pytest.raises(TypeError):
        ProviderRequest(  # type: ignore[arg-type]
            model="test-model",
            messages=("not a message",),
        )


def test_text_delta_event_carries_text() -> None:
    event = TextDelta(text="hello")

    assert event.text == "hello"


def test_finish_event_has_no_reason_by_default() -> None:
    event = Finish()

    assert event.reason is None


def test_finish_event_may_carry_stop_reason() -> None:
    event = Finish(reason="stop")

    assert event.reason == "stop"


def test_provider_error_event_carries_error_object() -> None:
    error = ProviderError("diagnostic")
    event = ProviderErrorEvent(error=error)

    assert event.error is error


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        (AuthenticationError, "authentication failed"),
        (RateLimitError, "rate limited"),
        (ProviderTimeoutError, "request timed out"),
        (TemporaryServerError, "server hiccup"),
        (MalformedResponseError, "unparseable body"),
    ],
)
def test_specific_errors_are_provider_errors(
    error_type: type[ProviderError], message: str
) -> None:
    error = error_type(message)

    assert isinstance(error, ProviderError)
    assert str(error) == message


@pytest.mark.parametrize(
    "sensitive_input",
    [
        "Authorization: Bearer sk-secret-value",
        "Bearer sk-live-1234567890abcdef",
        "api_key=sk-proj-abcdefghijklmnop",
        "upstream said sk-ant-abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_credentials_never_leak_through_str_or_repr(sensitive_input: str) -> None:
    error = AuthenticationError(f"request rejected: {sensitive_input}")

    assert "sk-secret-value" not in str(error)
    assert "sk-secret-value" not in repr(error)
    assert "sk-live-" not in str(error)
    assert "sk-live-" not in repr(error)
    assert "sk-proj-" not in str(error)
    assert "sk-proj-" not in repr(error)
    assert "sk-ant-" not in str(error)
    assert "sk-ant-" not in repr(error)


def test_auth_header_value_is_redacted_from_str_and_repr() -> None:
    error = AuthenticationError("Authorization: Bearer sk-secret-value")

    assert "sk-secret-value" not in str(error)
    assert "sk-secret-value" not in repr(error)
    assert "[redacted]" in str(error)  # 保留可读的清洗后诊断


def test_error_message_keeps_safe_public_diagnostic_after_redaction() -> None:
    error = ProviderError("Authorization: Bearer sk-secret-value")

    assert str(error) == "Authorization: Bearer [redacted]"
    assert repr(error) == "ProviderError('Authorization: Bearer [redacted]')"


def test_non_string_message_is_replaced_with_fixed_diagnostic() -> None:
    error = ProviderError(b"raw response body")  # type: ignore[arg-type]

    assert "raw response body" not in str(error)
    assert "raw response body" not in repr(error)
    assert str(error) == "<non-string diagnostic>"


def test_internal_cause_is_kept_but_never_rendered() -> None:
    cause = ValueError("raw payload: sk-secret-value")
    error = ProviderError("request failed", cause=cause)

    assert error.cause is cause
    assert "sk-secret-value" not in str(error)
    assert "sk-secret-value" not in repr(error)


def test_base_provider_error_serves_as_unknown_category() -> None:
    error = ProviderError("unexpected")

    assert str(error) == "unexpected"


def test_stream_events_are_mutually_distinguishable() -> None:
    events = [
        TextDelta(text="x"),
        Finish(),
        ProviderErrorEvent(error=ProviderError("e")),
    ]

    assert [type(event).__name__ for event in events] == [
        "TextDelta",
        "Finish",
        "ProviderErrorEvent",
    ]
    for event in events:
        assert isinstance(event, get_args(ProviderEvent))


def test_provider_contract_exposes_async_stream() -> None:
    class StreamingProvider:
        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            yield TextDelta(text="ok")
            yield Finish()

        async def aclose(self) -> None:
            return None

    provider = StreamingProvider()

    assert isinstance(provider, Provider)


def test_provider_protocol_declares_aclose() -> None:
    class ClosableProvider:
        def __init__(self) -> None:
            self.closed = 0

        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            yield Finish()

        async def aclose(self) -> None:
            self.closed += 1

    provider = ClosableProvider()

    # 扩展后的 Provider 契约要求 stream 与 aclose 两个成员。
    assert isinstance(provider, Provider)


def test_provider_aclose_is_repeatable() -> None:
    class ClosableProvider:
        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            yield Finish()

        async def aclose(self) -> None:
            return None

    async def main() -> None:
        provider = ClosableProvider()
        await provider.aclose()
        await provider.aclose()  # 重复调用必须安全
        collected: list[ProviderEvent] = []
        async for event in provider.stream(build_request()):
            collected.append(event)
        assert collected == [Finish()]

    asyncio.run(main())


def test_stream_is_consumed_with_async_for() -> None:
    class StreamingProvider:
        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            yield TextDelta(text="hello")
            yield TextDelta(text=" world")
            yield Finish()

        async def aclose(self) -> None:
            return None

    async def consume() -> list[ProviderEvent]:
        collected: list[ProviderEvent] = []
        async for event in StreamingProvider().stream(build_request()):
            collected.append(event)
        return collected

    collected = asyncio.run(consume())

    assert [type(event).__name__ for event in collected] == [
        "TextDelta",
        "TextDelta",
        "Finish",
    ]
    assert [event.text for event in collected if isinstance(event, TextDelta)] == [
        "hello",
        " world",
    ]


def test_classified_error_is_terminal_in_stream() -> None:
    class ErroringProvider:
        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            yield TextDelta(text="partial")
            yield ProviderErrorEvent(error=AuthenticationError("authentication failed"))

        async def aclose(self) -> None:
            return None

    async def consume() -> list[ProviderEvent]:
        collected: list[ProviderEvent] = []
        async for event in ErroringProvider().stream(build_request()):
            collected.append(event)
        return collected

    collected = asyncio.run(consume())

    # 已分类错误以 ProviderErrorEvent 作为流内最后一个事件，
    # 其后不得再出现 TextDelta 或 Finish。
    assert [type(event).__name__ for event in collected] == [
        "TextDelta",
        "ProviderErrorEvent",
    ]
    assert isinstance(collected[-1], ProviderErrorEvent)


def test_unexpected_program_error_propagates_as_exception() -> None:
    class BrokenProvider:
        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            yield TextDelta(text="partial")
            raise RuntimeError("programmer bug")

        async def aclose(self) -> None:
            return None

    async def consume() -> list[ProviderEvent]:
        collected: list[ProviderEvent] = []
        async for event in BrokenProvider().stream(build_request()):
            collected.append(event)
        return collected

    with pytest.raises(RuntimeError, match="programmer bug"):
        asyncio.run(consume())
