"""OpenAI-compatible Provider 传输边界测试（P2-02-B）。

固定 Provider 在 HTTP 传输边界上的可观察行为，全部从公共入口
构造和调用：

- 构造：``OpenAICompatibleProvider(api_key=..., base_url=..., transport=...)``
- 调用：``async for event in provider.stream(request)``

传输替身（``RecordingTransport``）只模拟 HTTP 层，不实现
FakeProvider；不访问外部网络、不使用真实 Key（全部使用明显的
假测试值）。

基线（P2-01）缺少 httpx 依赖且 Provider 实现尚未合并时，本文件
被 ``pytest.importorskip`` 整模块跳过；Worker A 分支合并后自动
生效。测试不修改任何生产代码。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from sse_transport_helpers import RecordingTransport, sse_event, sse_response

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
    TextDelta,
)

httpx = pytest.importorskip("httpx")
openai_compatible = pytest.importorskip("mica_agent.providers.openai_compatible")

OpenAICompatibleProvider = openai_compatible.OpenAICompatibleProvider

TEST_KEY = "sk-test-transport-boundary-0001"
BASE_URL = "https://api.example.test/v1"
MODEL = "test-model"


# ---------------------------------------------------------------- 公共辅助


def build_request() -> ProviderRequest:
    return ProviderRequest(
        model=MODEL,
        messages=(
            Message(role=MessageRole.SYSTEM, content="be brief"),
            Message(role=MessageRole.USER, content="hi"),
            Message(role=MessageRole.ASSISTANT, content="hello there"),
        ),
    )


def make_provider(
    transport: RecordingTransport, **kwargs: object
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key=TEST_KEY,
        base_url=BASE_URL,
        transport=transport,
        **kwargs,
    )


async def collect(
    provider: OpenAICompatibleProvider, request: ProviderRequest
) -> list[ProviderEvent]:
    events: list[ProviderEvent] = []
    async for event in provider.stream(request):
        events.append(event)
    return events


def collect_with(
    handler: object, **provider_kwargs: object
) -> tuple[list[ProviderEvent], RecordingTransport]:
    transport = RecordingTransport(handler)  # type: ignore[arg-type]
    provider = make_provider(transport, **provider_kwargs)
    events = asyncio.run(collect(provider, build_request()))
    return events, transport


def texts(events: list[ProviderEvent]) -> list[str]:
    return [event.text for event in events if isinstance(event, TextDelta)]


def last_error(events: list[ProviderEvent]) -> ProviderError:
    assert isinstance(events[-1], ProviderErrorEvent)
    return events[-1].error


async def ok_handler(request: httpx.Request) -> httpx.Response:
    """正常 SSE：两段文本增量 + finish_reason + [DONE]。"""
    return sse_response(
        [
            sse_event('{"choices":[{"delta":{"role":"assistant","content":"Hello"}}]}'),
            sse_event('{"choices":[{"delta":{"content":" world"}}]}'),
            sse_event('{"choices":[{"delta":{},"finish_reason":"stop"}]}'),
            sse_event("[DONE]"),
        ]
    )


class SleepRecorder:
    """记录退避调用并立即返回（不真实等待，测试安全缩短退避）。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


# ------------------------------------------------------------ 1. 请求格式


def test_request_uses_post_to_chat_completions_endpoint() -> None:
    transport = RecordingTransport(ok_handler)
    asyncio.run(collect(make_provider(transport), build_request()))

    request = transport.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/chat/completions"


def test_request_does_not_duplicate_chat_completions_suffix() -> None:
    endpoint = f"{BASE_URL}/chat/completions"
    transport = RecordingTransport(ok_handler)
    provider = OpenAICompatibleProvider(
        api_key=TEST_KEY,
        base_url=endpoint,
        transport=transport,
    )
    asyncio.run(collect(provider, build_request()))

    assert str(transport.requests[0].url) == endpoint


def test_request_json_carries_model_messages_and_stream_flag() -> None:
    transport = RecordingTransport(ok_handler)
    asyncio.run(collect(make_provider(transport), build_request()))

    body = json.loads(transport.requests[0].content)
    assert body["model"] == MODEL
    assert body["stream"] is True
    assert [message["role"] for message in body["messages"]] == [
        "system",
        "user",
        "assistant",
    ]


def test_request_messages_use_wire_roles_and_verbatim_content() -> None:
    request = ProviderRequest(
        model=MODEL,
        messages=(
            Message(role=MessageRole.SYSTEM, content="你是助手"),
            Message(role=MessageRole.USER, content="line1\nline2 😀"),
            Message(role=MessageRole.ASSISTANT, content='say "hi"'),
        ),
    )
    transport = RecordingTransport(ok_handler)
    asyncio.run(collect(make_provider(transport), request))

    messages = json.loads(transport.requests[0].content)["messages"]
    assert messages[0] == {"role": "system", "content": "你是助手"}
    assert messages[1] == {"role": "user", "content": "line1\nline2 😀"}
    assert messages[2] == {"role": "assistant", "content": 'say "hi"'}


def test_request_sends_bearer_authorization_header() -> None:
    transport = RecordingTransport(ok_handler)
    asyncio.run(collect(make_provider(transport), build_request()))

    assert transport.requests[0].headers["authorization"] == f"Bearer {TEST_KEY}"


def test_request_sends_json_content_type() -> None:
    transport = RecordingTransport(ok_handler)
    asyncio.run(collect(make_provider(transport), build_request()))

    assert transport.requests[0].headers["content-type"] == "application/json"


# -------------------------------------------------------------- 2. 成功 SSE


def test_multiple_data_events_produce_multiple_text_deltas() -> None:
    events, _ = collect_with(ok_handler)

    assert [type(event).__name__ for event in events] == [
        "TextDelta",
        "TextDelta",
        "Finish",
    ]


def test_text_chunks_concatenate_in_order() -> None:
    events, _ = collect_with(ok_handler)

    assert texts(events) == ["Hello", " world"]
    assert "".join(texts(events)) == "Hello world"


def test_finish_reason_is_surfaced_on_finish_event() -> None:
    events, _ = collect_with(ok_handler)

    assert isinstance(events[-1], Finish)
    assert events[-1].reason == "stop"


def test_done_marker_does_not_duplicate_finish() -> None:
    events, _ = collect_with(ok_handler)

    assert [type(event).__name__ for event in events].count("Finish") == 1


def test_blank_lines_and_heartbeats_produce_no_text_events() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(
            [
                "",
                ": keep-alive heartbeat\n\n",
                sse_event('{"choices":[{"delta":{"content":"ok"}}]}'),
                sse_event("[DONE]"),
            ]
        )

    events, _ = collect_with(handler)

    assert texts(events) == ["ok"]
    assert isinstance(events[-1], Finish)


def test_null_delta_content_does_not_emit_none_text() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(
            [
                sse_event('{"choices":[{"delta":{"content":null}}]}'),
                sse_event("[DONE]"),
            ]
        )

    events, _ = collect_with(handler)

    assert texts(events) == []


def test_stream_with_only_done_marker_completes_normally() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response([sse_event("[DONE]")])

    events, _ = collect_with(handler)

    assert [type(event).__name__ for event in events] == ["Finish"]
    assert events[-1].reason is None


# -------------------------------------------------------------- 3. 畸形 SSE


def malformed_handler(payload: str) -> object:
    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response([sse_event(payload), sse_event("[DONE]")])

    return handler


def test_invalid_json_emits_malformed_error_then_stops() -> None:
    events, _ = collect_with(malformed_handler("{not json"))

    assert len(events) == 1
    assert isinstance(last_error(events), MalformedResponseError)


def test_missing_choices_field_emits_malformed_error() -> None:
    events, _ = collect_with(
        malformed_handler('{"id":"x","object":"chat.completion.chunk"}')
    )

    assert len(events) == 1
    assert isinstance(last_error(events), MalformedResponseError)


@pytest.mark.parametrize(
    "payload",
    [
        '{"choices":{}}',
        '{"choices":[]}',
    ],
)
def test_non_list_or_empty_choices_emits_malformed_error(payload: str) -> None:
    events, _ = collect_with(malformed_handler(payload))

    assert len(events) == 1
    assert isinstance(last_error(events), MalformedResponseError)


def test_malformed_delta_structure_emits_malformed_error() -> None:
    events, _ = collect_with(malformed_handler('{"choices":[{"delta":"oops"}]}'))

    assert len(events) == 1
    assert isinstance(last_error(events), MalformedResponseError)


def test_non_string_delta_content_emits_malformed_error() -> None:
    events, _ = collect_with(
        malformed_handler('{"choices":[{"delta":{"content":123}}]}')
    )

    assert len(events) == 1
    assert isinstance(last_error(events), MalformedResponseError)


def test_stream_cut_off_before_done_emits_malformed_error() -> None:
    async def cut_body() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadError("connection reset mid-stream")

    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(content=cut_body())

    events, transport = collect_with(handler)

    assert texts(events) == ["partial"]
    assert isinstance(last_error(events), MalformedResponseError)
    assert len(transport.requests) == 1


# ---------------------------------------------------------- 4. HTTP 错误映射


def error_response(status: int, body: str | None = None) -> object:
    payload = (
        body
        if body is not None
        else json.dumps({"error": {"message": f"upstream {status}"}})
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(
            status=status,
            headers={"content-type": "application/json"},
            content=payload.encode("utf-8"),
        )

    return handler


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_status_maps_to_authentication_error(status: int) -> None:
    events, transport = collect_with(error_response(status))

    assert isinstance(last_error(events), AuthenticationError)
    assert len(transport.requests) == 1  # 鉴权失败不重试


def test_408_maps_to_provider_timeout_and_is_retried() -> None:
    events, transport = collect_with(error_response(408))

    assert isinstance(last_error(events), ProviderTimeoutError)
    # 超时类错误按设置的次数上限重试：默认 max_retries=1 → 初始请求 + 1 次重试。
    assert len(transport.requests) == 2


def test_transport_timeout_maps_to_provider_timeout_and_is_retried() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")

    events, transport = collect_with(handler)

    assert isinstance(last_error(events), ProviderTimeoutError)
    # 传输层超时同样按次数上限重试。
    assert len(transport.requests) == 2


def test_429_maps_to_rate_limit_error() -> None:
    events, transport = collect_with(error_response(429))

    assert isinstance(last_error(events), RateLimitError)
    assert len(transport.requests) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_temporary_server_status_maps_to_temporary_server_error(status: int) -> None:
    events, _ = collect_with(error_response(status))

    assert isinstance(last_error(events), TemporaryServerError)


def test_other_status_maps_to_unknown_provider_error() -> None:
    events, _ = collect_with(error_response(418))

    error = last_error(events)
    assert isinstance(error, ProviderError)
    assert not isinstance(
        error,
        (
            AuthenticationError,
            ProviderTimeoutError,
            RateLimitError,
            TemporaryServerError,
            MalformedResponseError,
        ),
    )


def test_error_diagnostics_never_contain_sensitive_response_body() -> None:
    body = json.dumps(
        {
            "error": {
                "message": (
                    "invalid api_key=sk-live-secret-123456 "
                    "Authorization: Bearer sk-top-secret-123456"
                )
            },
            "trace": {"internal": "full-sensitive-json-payload"},
        }
    )
    events, _ = collect_with(error_response(500, body))

    diagnostic = str(last_error(events))
    assert "sk-live-secret-123456" not in diagnostic
    assert "sk-top-secret-123456" not in diagnostic
    assert "full-sensitive-json-payload" not in diagnostic


# ------------------------------------------------------------------ 5. 重试


def test_temporary_server_error_is_retried_then_succeeds() -> None:
    state = {"calls": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return await error_response(503)(request)  # type: ignore[operator]
        return await ok_handler(request)

    recorder = SleepRecorder()
    events, transport = collect_with(
        handler, max_retries=2, retry_backoff=0.5, sleep=recorder
    )

    assert isinstance(events[-1], Finish)
    assert len(transport.requests) == 2
    assert recorder.calls == [0.5]


def test_timeout_is_retried_then_succeeds() -> None:
    state = {"calls": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            raise httpx.ConnectTimeout("connect timed out")
        return await ok_handler(request)

    recorder = SleepRecorder()
    events, transport = collect_with(handler, max_retries=2, sleep=recorder)

    assert isinstance(events[-1], Finish)
    assert len(transport.requests) == 2
    assert len(recorder.calls) == 1


def test_authentication_error_is_not_retried() -> None:
    events, transport = collect_with(error_response(401), max_retries=5)

    assert isinstance(last_error(events), AuthenticationError)
    assert len(transport.requests) == 1


def test_malformed_response_is_not_retried() -> None:
    events, transport = collect_with(malformed_handler("{not json"), max_retries=5)

    assert isinstance(last_error(events), MalformedResponseError)
    assert len(transport.requests) == 1


def test_retries_exhaust_after_max_retries_with_final_error() -> None:
    recorder = SleepRecorder()
    events, transport = collect_with(
        error_response(500), max_retries=2, retry_backoff=1.0, sleep=recorder
    )

    assert len(transport.requests) == 3  # 初始请求 + 2 次重试
    assert isinstance(last_error(events), TemporaryServerError)
    assert recorder.calls == [1.0, 1.0]


def test_stream_that_started_text_is_not_restarted_on_late_error() -> None:
    async def cut_body() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.ReadError("connection reset mid-stream")

    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(content=cut_body())

    recorder = SleepRecorder()
    events, transport = collect_with(handler, max_retries=2, sleep=recorder)

    # 已开始产出文本的流不重启，避免重复文本；错误作为末尾事件。
    assert len(transport.requests) == 1
    assert texts(events) == ["partial"]
    assert isinstance(last_error(events), MalformedResponseError)
    assert recorder.calls == []


# -------------------------------------------------- 6. 取消与资源关闭


def slow_body() -> AsyncIterator[bytes]:
    async def generate() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
        await asyncio.sleep(3600)

    return generate()


async def slow_handler(request: httpx.Request) -> httpx.Response:
    return sse_response(content=slow_body())


async def cancel_after_response(
    transport: RecordingTransport,
    provider: OpenAICompatibleProvider,
) -> None:
    task = asyncio.create_task(collect(provider, build_request()))
    for _ in range(200):
        if transport.responses:
            break
        await asyncio.sleep(0.005)
    assert transport.responses  # 响应已建立，取消发生在流读取期间
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)  # 让资源清理完成


def test_cancellation_propagates_cancelled_error() -> None:
    transport = RecordingTransport(slow_handler)
    provider = make_provider(transport)

    asyncio.run(cancel_after_response(transport, provider))

    # CancelledError 从 stream 传播，不转换成 ProviderErrorEvent。


def test_response_is_closed_after_successful_stream() -> None:
    events, transport = collect_with(ok_handler)

    assert isinstance(events[-1], Finish)
    assert transport.responses[0].is_closed


def test_response_is_closed_after_error_stream() -> None:
    events, transport = collect_with(error_response(500))

    assert isinstance(last_error(events), TemporaryServerError)
    assert transport.responses[0].is_closed


def test_response_is_closed_after_cancellation() -> None:
    transport = RecordingTransport(slow_handler)
    provider = make_provider(transport)

    asyncio.run(cancel_after_response(transport, provider))

    assert transport.responses[0].is_closed


def test_client_is_closed_after_successful_stream() -> None:
    transport = RecordingTransport(ok_handler)
    provider = make_provider(transport)

    async def scenario() -> list[ProviderEvent]:
        events = await collect(provider, build_request())
        await provider.aclose()
        return events

    events = asyncio.run(scenario())

    assert isinstance(events[-1], Finish)
    # Provider 复用 AsyncClient（连接池），通过公共 aclose() 关闭。
    assert transport.aclose_count >= 1


def test_client_is_closed_after_error_stream() -> None:
    transport = RecordingTransport(error_response(500))  # type: ignore[arg-type]
    provider = make_provider(transport)

    async def scenario() -> list[ProviderEvent]:
        events = await collect(provider, build_request())
        await provider.aclose()
        return events

    events = asyncio.run(scenario())

    assert isinstance(last_error(events), TemporaryServerError)
    assert transport.aclose_count >= 1


def test_client_is_closed_after_cancellation() -> None:
    transport = RecordingTransport(slow_handler)
    provider = make_provider(transport)

    async def scenario() -> None:
        await cancel_after_response(transport, provider)
        await provider.aclose()

    asyncio.run(scenario())

    assert transport.aclose_count >= 1


def test_public_events_never_leak_httpx_response_objects() -> None:
    allowed = (TextDelta, Finish, ProviderErrorEvent)

    events, _ = collect_with(ok_handler)
    assert all(isinstance(event, allowed) for event in events)

    events, _ = collect_with(error_response(500))
    assert all(isinstance(event, allowed) for event in events)

    transport = RecordingTransport(slow_handler)
    provider = make_provider(transport)
    cancelled: list[ProviderEvent] = []

    async def consume() -> None:
        async def collect_into() -> None:
            async for event in provider.stream(build_request()):
                cancelled.append(event)

        task = asyncio.create_task(collect_into())
        for _ in range(200):
            if transport.responses:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(consume())
    assert cancelled  # 取消前已产出的事件
    assert all(isinstance(event, allowed) for event in cancelled)
