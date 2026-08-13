"""OpenAI-compatible Provider 生产实现测试（P2-02 Worker A）。

所有测试使用 ``httpx.MockTransport`` 固定 HTTP 传输响应，
不访问真实网络、不使用真实 API Key。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from mica_agent.models import Message, MessageRole
from mica_agent.providers import (
    AuthenticationError,
    Finish,
    MalformedResponseError,
    OpenAICompatibleProvider,
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
from mica_agent.providers.openai_compatible import resolve_endpoint

# ---------------------------------------------------------------------------
# URL 契约
# ---------------------------------------------------------------------------


def test_resolve_endpoint_appends_chat_completions_to_base_url() -> None:
    assert (
        resolve_endpoint("https://api.example.com/v1")
        == "https://api.example.com/v1/chat/completions"
    )


def test_resolve_endpoint_preserves_full_url_with_trailing_slash() -> None:
    assert (
        resolve_endpoint("https://api.example.com/v1/")
        == "https://api.example.com/v1/chat/completions"
    )


def test_resolve_endpoint_keeps_url_already_pointing_at_chat_completions() -> None:
    assert (
        resolve_endpoint("https://api.example.com/v1/chat/completions")
        == "https://api.example.com/v1/chat/completions"
    )


def test_resolve_endpoint_never_duplicates_chat_completions_path() -> None:
    endpoint = resolve_endpoint("https://api.example.com/v1/chat/completions/")

    assert endpoint.count("/chat/completions") == 1


def test_resolve_endpoint_keeps_query_string() -> None:
    assert (
        resolve_endpoint("https://api.example.com/v1?foo=bar")
        == "https://api.example.com/v1/chat/completions?foo=bar"
    )


# ---------------------------------------------------------------------------
# 请求构造与鉴权
# ---------------------------------------------------------------------------


def _make_request() -> ProviderRequest:
    return ProviderRequest(
        model="gpt-test",
        messages=(
            Message(MessageRole.SYSTEM, "be brief"),
            Message(MessageRole.USER, "hello"),
            Message(MessageRole.ASSISTANT, "hi there"),
        ),
    )


async def _noop_sleep(_: float) -> None:
    """测试用退避：立即返回，不真实等待。"""
    return


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str = "test-api-key",
    base_url: str = "https://api.example.com/v1",
    max_retries: int = 0,
    retry_backoff: float = 1.0,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key=api_key,
        base_url=base_url,
        transport=httpx.MockTransport(handler),
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        sleep=_noop_sleep,
    )


async def _collect(
    provider: OpenAICompatibleProvider,
    request: ProviderRequest,
    collected: list[ProviderEvent] | None = None,
) -> list[ProviderEvent]:
    if collected is None:
        collected = []
    async for event in provider.stream(request):
        collected.append(event)
    return collected


def _run_collect(
    provider: OpenAICompatibleProvider, request: ProviderRequest
) -> list[ProviderEvent]:
    return asyncio.run(_collect(provider, request))


def test_provider_conforms_to_public_contract() -> None:
    provider = make_provider(lambda request: httpx.Response(200, request=request))

    assert isinstance(provider, Provider)


def test_request_uses_post_with_resolved_endpoint() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, text="data: [DONE]\n\n", request=request)

    provider = make_provider(handler)
    _run_collect(provider, _make_request())

    request = captured["request"]
    assert isinstance(request, httpx.Request)
    assert request.method == "POST"
    assert request.url == "https://api.example.com/v1/chat/completions"


def test_request_carries_auth_and_json_headers() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, text="data: [DONE]\n\n", request=request)

    provider = make_provider(handler, api_key="sk-test-secret")
    _run_collect(provider, _make_request())

    request = captured["request"]
    assert isinstance(request, httpx.Request)
    assert request.headers["Authorization"] == "Bearer sk-test-secret"
    assert request.headers["Content-Type"] == "application/json"


def test_request_body_uses_wire_roles_and_stream_flag() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, text="data: [DONE]\n\n", request=request)

    provider = make_provider(handler)
    _run_collect(provider, _make_request())

    request = captured["request"]
    assert isinstance(request, httpx.Request)
    body = request.read()
    payload = json.loads(body)
    assert payload["model"] == "gpt-test"
    assert payload["stream"] is True
    assert payload["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


def test_api_key_never_appears_in_repr() -> None:
    provider = make_provider(
        lambda request: httpx.Response(200, request=request),
        api_key="sk-super-secret-value",
    )

    assert "sk-super-secret-value" not in repr(provider)


def test_blank_api_key_is_rejected() -> None:
    with pytest.raises(ValueError):
        make_provider(
            lambda request: httpx.Response(200, request=request), api_key="   "
        )


def test_negative_max_retries_is_rejected() -> None:
    with pytest.raises(ValueError):
        make_provider(
            lambda request: httpx.Response(200, request=request), max_retries=-1
        )


# ---------------------------------------------------------------------------
# SSE 成功流解析
# ---------------------------------------------------------------------------


DONE_SSE = "data: [DONE]\n\n"


def _sse_response(*lines: str) -> httpx.Response:
    return httpx.Response(200, text="".join(lines))


def _text_delta(text: str) -> TextDelta:
    return TextDelta(text=text)


def test_multiple_deltas_then_finish_reason() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [
        _text_delta("hello"),
        _text_delta(" world"),
        Finish(reason="stop"),
    ]


def test_network_chunk_boundaries_are_not_message_boundaries() -> None:
    class ChunkyStream(httpx.AsyncByteStream):
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in self._chunks:
                yield chunk

    stream = ChunkyStream(
        [
            b'data: {"choices":',
            b'[{"delta":{"content":"he',
            b'llo"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}\n\n',
            b"data: [DONE]\n\n",
        ]
    )
    provider = make_provider(
        lambda request: httpx.Response(200, stream=stream, request=request)
    )

    events = _run_collect(provider, _make_request())

    assert events == [_text_delta("hello"), _text_delta(" world"), Finish()]


def test_empty_stream_with_only_done_ends_normally() -> None:
    provider = make_provider(lambda request: _sse_response(DONE_SSE))

    events = _run_collect(provider, _make_request())

    assert events == [Finish()]


def test_missing_or_null_content_is_not_rendered_as_none() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{}}]}\n\n',
            'data: {"choices":[{"delta":{"content":null}}]}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [Finish()]
    assert all(not isinstance(event, TextDelta) for event in events)


def test_empty_choices_chunk_is_ignored_as_heartbeat() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[]}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [Finish()]


def test_blank_lines_and_comments_are_ignored() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            ": keep-alive comment\n\n",
            "\n",
            'data: {"choices":[{"delta":{"content":"x"}}]}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [_text_delta("x"), Finish()]


def test_data_lines_without_blank_separator_are_handled_line_by_line() -> None:
    # 两条 data 行间没有空行：逐行处理，各自成事件。
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"content":"a"}}]}\n',
            'data: {"choices":[{"delta":{"content":"b"}}]}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [_text_delta("a"), _text_delta("b"), Finish()]


def test_finish_reason_then_done_yields_single_finish() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [Finish(reason="stop")]


# ---------------------------------------------------------------------------
# HTTP 错误分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationError),
        (403, AuthenticationError),
        (408, ProviderTimeoutError),
        (429, RateLimitError),
        (500, TemporaryServerError),
        (502, TemporaryServerError),
        (503, TemporaryServerError),
        (504, TemporaryServerError),
    ],
)
def test_http_status_error_mapping(
    status: int, error_type: type[ProviderError]
) -> None:
    provider = make_provider(lambda request: httpx.Response(status, request=request))

    events = _run_collect(provider, _make_request())

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, error_type)


@pytest.mark.parametrize("status", [400, 404, 422])
def test_other_http_errors_map_to_unknown_provider_error(status: int) -> None:
    provider = make_provider(lambda request: httpx.Response(status, request=request))

    events = _run_collect(provider, _make_request())

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert type(events[0].error) is ProviderError


def test_error_message_is_short_and_never_echoes_response_body() -> None:
    provider = make_provider(
        lambda request: httpx.Response(
            500,
            text='{"error": "boom with secret sk-server-secret-999"}',
            request=request,
        )
    )

    events = _run_collect(provider, _make_request())

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    message = str(events[0].error)
    assert "sk-server-secret-999" not in message
    assert "boom" not in message
    assert len(message) < 100


def test_error_event_is_terminal_and_stream_ends_immediately() -> None:
    provider = make_provider(lambda request: httpx.Response(401, request=request))

    events = _run_collect(provider, _make_request())

    assert [type(event).__name__ for event in events] == ["ProviderErrorEvent"]


# ---------------------------------------------------------------------------
# 畸形响应
# ---------------------------------------------------------------------------


def test_invalid_json_data_is_malformed_error() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            "data: not-json-at-all\n\n",
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert [type(event).__name__ for event in events] == ["ProviderErrorEvent"]
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, MalformedResponseError)


@pytest.mark.parametrize(
    "chunk",
    [
        'data: {"choices":"nope"}\n\n',
        'data: {"choices":[42]}\n\n',
        'data: {"choices":[{"delta":"nope"}]}\n\n',
        'data: {"choices":[{"delta":{"content":123}}]}\n\n',
        "data: [1, 2, 3]\n\n",
    ],
)
def test_structural_mismatches_are_malformed_errors(chunk: str) -> None:
    provider = make_provider(lambda request: _sse_response(chunk, DONE_SSE))

    events = _run_collect(provider, _make_request())

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, MalformedResponseError)


def test_stream_ending_without_finish_signal_is_malformed() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
        )
    )

    events = _run_collect(provider, _make_request())

    assert [type(event).__name__ for event in events] == [
        "TextDelta",
        "ProviderErrorEvent",
    ]
    assert isinstance(events[-1], ProviderErrorEvent)
    assert isinstance(events[-1].error, MalformedResponseError)
    assert "partial" not in str(events[-1].error)


# ---------------------------------------------------------------------------
# 有限重试
# ---------------------------------------------------------------------------


class _FlakyStream(httpx.AsyncByteStream):
    """产出部分文本后抛出超时异常的响应流。"""

    def __init__(self, chunks: list[bytes], exc: httpx.HTTPError | None = None) -> None:
        self._chunks = chunks
        self._exc = exc

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._exc is not None:
            raise self._exc


def _make_flaky_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int,
    sleep_calls: list[float] | None = None,
) -> OpenAICompatibleProvider:
    if sleep_calls is None:
        return OpenAICompatibleProvider(
            api_key="test-api-key",
            base_url="https://api.example.com/v1",
            transport=httpx.MockTransport(handler),
            max_retries=max_retries,
            retry_backoff=0.5,
            sleep=_noop_sleep,
        )

    async def recorded_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    return OpenAICompatibleProvider(
        api_key="test-api-key",
        base_url="https://api.example.com/v1",
        transport=httpx.MockTransport(handler),
        max_retries=max_retries,
        retry_backoff=0.5,
        sleep=recorded_sleep,
    )


def test_temporary_error_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503, request=request)
        return httpx.Response(200, text=DONE_SSE, request=request)

    sleep_calls: list[float] = []
    provider = _make_flaky_provider(handler, max_retries=2, sleep_calls=sleep_calls)

    events = _run_collect(provider, _make_request())

    assert calls["n"] == 2
    assert events == [Finish()]
    assert sleep_calls == [0.5]


def test_retry_count_is_capped() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, request=request)

    sleep_calls: list[float] = []
    provider = _make_flaky_provider(handler, max_retries=1, sleep_calls=sleep_calls)

    events = _run_collect(provider, _make_request())

    assert calls["n"] == 2  # 初始尝试 + 1 次重试
    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, TemporaryServerError)
    assert sleep_calls == [0.5]


def test_auth_error_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, request=request)

    provider = make_provider(handler, max_retries=3)

    events = _run_collect(provider, _make_request())

    assert calls["n"] == 1
    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, AuthenticationError)


def test_rate_limit_error_is_not_auto_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, request=request)

    provider = make_provider(handler, max_retries=3)

    events = _run_collect(provider, _make_request())

    assert calls["n"] == 1
    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, RateLimitError)


def test_connect_timeout_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("connection timed out")
        return httpx.Response(200, text=DONE_SSE, request=request)

    provider = make_provider(handler, max_retries=2)

    events = _run_collect(provider, _make_request())

    assert calls["n"] == 2
    assert events == [Finish()]


def test_no_retry_after_text_has_been_produced() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            stream=_FlakyStream(
                [b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'],
                exc=httpx.ReadTimeout("read timed out"),
            ),
            request=request,
        )

    sleep_calls: list[float] = []
    provider = _make_flaky_provider(handler, max_retries=3, sleep_calls=sleep_calls)

    events = _run_collect(provider, _make_request())

    assert calls["n"] == 1  # 已产出文本后不重试
    assert sleep_calls == []
    assert [type(event).__name__ for event in events] == [
        "TextDelta",
        "ProviderErrorEvent",
    ]
    assert isinstance(events[-1], ProviderErrorEvent)
    assert isinstance(events[-1].error, ProviderTimeoutError)


# ---------------------------------------------------------------------------
# 取消与资源生命周期
# ---------------------------------------------------------------------------


class _SlowStream(httpx.AsyncByteStream):
    """产出第一个事件后挂起的响应流，用于测试取消传播。"""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        await asyncio.sleep(30)
        yield b"data: [DONE]\n\n"


def test_cancellation_propagates_and_is_not_wrapped_as_error_event() -> None:
    provider = make_provider(
        lambda request: httpx.Response(200, stream=_SlowStream(), request=request)
    )

    async def main() -> None:
        collected: list[ProviderEvent] = []
        task = asyncio.create_task(_collect(provider, _make_request(), collected))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not any(isinstance(event, ProviderErrorEvent) for event in collected)
        await provider.aclose()

    asyncio.run(main())


def test_stream_can_be_consumed_multiple_times_with_reused_client() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=DONE_SSE, request=request)

    provider = make_provider(handler)

    first = _run_collect(provider, _make_request())
    second = _run_collect(provider, _make_request())

    assert first == [Finish()]
    assert second == [Finish()]
    assert calls["n"] == 2
    asyncio.run(provider.aclose())


def test_aclose_is_idempotent() -> None:
    provider = make_provider(
        lambda request: httpx.Response(200, text=DONE_SSE, request=request)
    )

    asyncio.run(provider.aclose())
    asyncio.run(provider.aclose())  # 第二次调用不抛异常
