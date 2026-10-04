"""OpenAI-compatible Provider 生产实现测试。

测试在 HTTP 传输边界提供固定响应并观测资源生命周期，
不访问真实网络、不使用真实 API Key。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from sse_transport_helpers import RecordingTransport, sse_response

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
    ProviderReasoningDelta,
    ProviderRequest,
    ProviderTimeoutError,
    RateLimitError,
    TemporaryServerError,
    TextDelta,
)
from mica_agent.providers.openai_compatible import resolve_endpoint
from mica_agent.reasoning import ReasoningEffort
from mica_agent.tool_contracts import ToolCall, ToolSpec

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
    thinking_level_map: dict[ReasoningEffort, str] | None = None,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key=api_key,
        base_url=base_url,
        transport=httpx.MockTransport(handler),
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        sleep=_noop_sleep,
        thinking_level_map=thinking_level_map,
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


def test_provider_conforms_to_extended_protocol_with_aclose() -> None:
    # 生产实现必须符合包含 stream 与 aclose 的 Provider 协议。
    provider = make_provider(lambda request: httpx.Response(200, request=request))

    assert isinstance(provider, Provider)
    assert hasattr(provider, "aclose")
    asyncio.run(provider.aclose())
    asyncio.run(provider.aclose())  # 幂等


@pytest.mark.parametrize(
    ("base_url", "expected_url"),
    [
        (
            "https://api.example.com/v1",
            "https://api.example.com/v1/chat/completions",
        ),
        (
            "https://api.example.com/v1/chat/completions",
            "https://api.example.com/v1/chat/completions",
        ),
    ],
)
def test_request_uses_post_with_resolved_endpoint(
    base_url: str, expected_url: str
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, text=VALID_SSE, request=request)

    provider = make_provider(handler, base_url=base_url)
    _run_collect(provider, _make_request())

    request = captured["request"]
    assert isinstance(request, httpx.Request)
    assert request.method == "POST"
    assert str(request.url) == expected_url


def test_request_carries_auth_and_json_headers() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, text=VALID_SSE, request=request)

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
        return httpx.Response(200, text=VALID_SSE, request=request)

    provider = make_provider(handler)
    request = ProviderRequest(
        model="gpt-test",
        messages=(
            Message(MessageRole.SYSTEM, "你是助手"),
            Message(MessageRole.USER, "line1\nline2 😀"),
            Message(MessageRole.ASSISTANT, 'say "hi"'),
        ),
    )
    _run_collect(provider, request)

    http_request = captured["request"]
    assert isinstance(http_request, httpx.Request)
    body = http_request.read()
    payload = json.loads(body)
    assert set(payload) == {"model", "messages", "stream", "max_tokens"}
    assert payload["max_tokens"] == 4096
    assert payload["model"] == "gpt-test"
    assert payload["stream"] is True
    assert "reasoning_effort" not in payload
    assert "reasoning" not in payload
    assert "thinking" not in payload
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert payload["messages"] == [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "line1\nline2 😀"},
        {"role": "assistant", "content": 'say "hi"'},
    ]


@pytest.mark.parametrize(
    ("effort", "wire_value"),
    [
        (ReasoningEffort.MINIMAL, "minimal"),
        (ReasoningEffort.LOW, "low"),
        (ReasoningEffort.MEDIUM, "medium"),
        (ReasoningEffort.HIGH, "high"),
        (ReasoningEffort.XHIGH, "xhigh"),
        (ReasoningEffort.MAX, "max"),
    ],
)
def test_request_sends_non_off_reasoning_effort(
    effort: ReasoningEffort, wire_value: str
) -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, text=VALID_SSE, request=request)

    request = ProviderRequest(
        "gpt-test",
        (Message(MessageRole.USER, "think"),),
        reasoning_effort=effort,
    )
    provider = make_provider(handler)
    _run_collect(provider, request)

    assert json.loads(captured["request"].read())["reasoning_effort"] == wire_value


def test_request_uses_custom_thinking_level_map() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, text=VALID_SSE, request=request)

    request = ProviderRequest(
        "gpt-test",
        (Message(MessageRole.USER, "think"),),
        reasoning_effort=ReasoningEffort.HIGH,
    )
    provider = make_provider(
        handler, thinking_level_map={ReasoningEffort.HIGH: "balanced"}
    )
    _run_collect(provider, request)

    assert json.loads(captured["request"].read())["reasoning_effort"] == "balanced"


def test_custom_thinking_level_map_is_copied_and_not_mutable() -> None:
    mapping = {ReasoningEffort.HIGH: "balanced"}
    provider = make_provider(
        lambda request: httpx.Response(200, text=VALID_SSE, request=request),
        thinking_level_map=mapping,
    )
    mapping[ReasoningEffort.HIGH] = "changed"

    request = ProviderRequest(
        "gpt-test",
        (Message(MessageRole.USER, "think"),),
        reasoning_effort=ReasoningEffort.HIGH,
    )
    events = _run_collect(provider, request)

    assert events == [TextDelta("ok"), Finish()]


def test_unmapped_custom_reasoning_effort_fails_safely_before_http() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, text=VALID_SSE, request=request)

    request = ProviderRequest(
        "gpt-test",
        (Message(MessageRole.USER, "think"),),
        reasoning_effort=ReasoningEffort.LOW,
    )
    provider = make_provider(
        handler, thinking_level_map={ReasoningEffort.HIGH: "balanced"}
    )

    events = _run_collect(provider, request)

    assert requests == 0
    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert type(events[0].error) is ProviderError
    assert str(events[0].error) == "reasoning effort is not supported"


@pytest.mark.parametrize(
    "invalid_map",
    [
        {"high": "secret-sk-mapping-value"},
        {ReasoningEffort.OFF: "secret-sk-mapping-value"},
        {ReasoningEffort.HIGH: ""},
        {ReasoningEffort.HIGH: "   "},
        {ReasoningEffort.HIGH: "unsafe\nvalue"},
        {ReasoningEffort.HIGH: 3},
        object(),
    ],
)
def test_invalid_thinking_level_map_fails_without_echoing_values(
    invalid_map: object,
) -> None:
    with pytest.raises((TypeError, ValueError)) as captured:
        OpenAICompatibleProvider(
            api_key="test-api-key",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=VALID_SSE, request=request)
            ),
            thinking_level_map=invalid_map,  # type: ignore[arg-type]
        )

    assert "secret-sk-mapping-value" not in str(captured.value)
    assert "secret-sk-mapping-value" not in repr(captured.value)


def test_request_sends_tool_specs_as_standard_tools_payload() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, text=VALID_SSE, request=request)

    request = ProviderRequest(
        model="gpt-test",
        messages=(Message(MessageRole.USER, "use lookup"),),
        tools=(
            ToolSpec(
                "lookup",
                "Look up a value",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "filters": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["query"],
                },
            ),
        ),
    )
    provider = make_provider(handler)
    _run_collect(provider, request)

    body = json.loads(captured["request"].read())  # type: ignore[union-attr]
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look up a value",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "filters": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["query"],
                },
            },
        }
    ]
    body["tools"][0]["function"]["parameters"]["properties"]["query"]["type"] = (
        "integer"
    )
    assert request.tools[0].input_schema["properties"]["query"]["type"] == "string"


def test_request_preserves_order_for_multiple_tool_specs() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, text=VALID_SSE, request=request)

    request = ProviderRequest(
        model="gpt-test",
        messages=(Message(MessageRole.USER, "choose a tool"),),
        tools=(
            ToolSpec(
                "first_lookup",
                "First description",
                {
                    "type": "object",
                    "properties": {"first": {"type": "string"}},
                },
            ),
            ToolSpec(
                "second_lookup",
                "Second description",
                {
                    "type": "object",
                    "properties": {"second": {"type": "integer"}},
                },
            ),
        ),
    )

    provider = make_provider(handler)
    _run_collect(provider, request)

    http_request = captured["request"]
    assert isinstance(http_request, httpx.Request)
    payload = json.loads(http_request.read())
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "first_lookup",
                "description": "First description",
                "parameters": {
                    "type": "object",
                    "properties": {"first": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "second_lookup",
                "description": "Second description",
                "parameters": {
                    "type": "object",
                    "properties": {"second": {"type": "integer"}},
                },
            },
        },
    ]

    request = ProviderRequest(
        model="gpt-test",
        messages=(
            Message(
                MessageRole.ASSISTANT,
                "",
                tool_calls=(
                    ToolCall(
                        "call-1",
                        "lookup",
                        {"query": "你好", "empty": {}, "items": [1, True]},
                    ),
                ),
            ),
            Message(MessageRole.TOOL, "result 😀", tool_call_id="call-1"),
        ),
    )
    captured: dict[str, object] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured["request"] = http_request
        return httpx.Response(200, text=VALID_SSE, request=http_request)

    _run_collect(make_provider(handler), request)
    payload = json.loads(captured["request"].read())  # type: ignore[union-attr]

    assert payload["messages"] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "arguments": '{"query":"你好","empty":{},"items":[1,true]}',
                    },
                }
            ],
        },
        {"role": "tool", "content": "result 😀", "tool_call_id": "call-1"},
    ]

    payload["messages"][0]["tool_calls"][0]["function"]["arguments"] = "mutated"
    assert request.messages[0].tool_calls[0].arguments["query"] == "你好"


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
VALID_SSE = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n' + DONE_SSE


def _sse_response(*lines: str) -> httpx.Response:
    return httpx.Response(200, text="".join(lines))


def _text_delta(text: str) -> TextDelta:
    return TextDelta(text=text)


def test_reasoning_precedes_content_from_the_same_delta() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"reasoning_content":"先分析","content":"正式回答"}}]}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [
        ProviderReasoningDelta("先分析"),
        TextDelta("正式回答"),
        Finish(),
    ]


def test_reasoning_only_deltas_preserve_order_unicode_and_ignore_empty_values() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"reasoning_content":"第一步"}}]}\n\n',
            'data: {"choices":[{"delta":{"reasoning_content":null}}]}\n\n',
            'data: {"choices":[{"delta":{"reasoning_content":""}}]}\n\n',
            'data: {"choices":[{"delta":{"reasoning_content":"\ud574\uc11d \u2713"}}]}\n\n',
            VALID_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [
        ProviderReasoningDelta("第一步"),
        ProviderReasoningDelta("해석 ✓"),
        TextDelta("ok"),
        Finish(),
    ]


def test_reasoning_precedes_tool_call_assembly_without_pollution() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"reasoning_content":"inspect","tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{}"}}]}}]}\n\n',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [
        ProviderReasoningDelta("inspect"),
        Finish(
            reason="tool_calls",
            tool_calls=(ToolCall("call-1", "lookup", {}),),
        ),
    ]


@pytest.mark.parametrize("invalid", [3, True, [], {"token": "sk-secret-sentinel"}])
def test_invalid_reasoning_content_is_one_safe_terminal_error(invalid: object) -> None:
    unsafe_chunk = json.dumps(
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_content": invalid,
                        "content": "must not appear",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "sensitive-call-id",
                                "type": "function",
                                "function": {
                                    "name": "sensitive_tool",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    }
                }
            ]
        }
    )
    provider = make_provider(
        lambda request: _sse_response(
            f"data: {unsafe_chunk}\n\n",
            'data: {"choices":[{"delta":{"reasoning_content":"later","content":"later"}}]}\n\n',
            DONE_SSE,
        ),
        api_key="sk-provider-secret",
        base_url="https://secret.example/v1",
    )

    events = _run_collect(provider, _make_request())

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert type(events[0].error) is MalformedResponseError
    rendered = f"{events[0].error!s} {events[0].error!r}"
    for secret in (
        "sk-secret-sentinel",
        "sk-provider-secret",
        "secret.example",
        "sensitive-call-id",
        "sensitive_tool",
        "must not appear",
        "traceback",
    ):
        assert secret not in rendered


@pytest.mark.parametrize("pending_reason", [None, "", "missing"])
def test_stream_assembles_one_fragmented_tool_call(pending_reason) -> None:
    def chunk(delta):
        choice = {"delta": delta}
        if pending_reason != "missing":
            choice["finish_reason"] = pending_reason
        return f"data: {json.dumps({'choices': [choice]})}\n\n"

    provider = make_provider(
        lambda request: _sse_response(
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q":'},
                        }
                    ]
                }
            ),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"你好"}'}}]}),
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [
        Finish(
            reason="tool_calls",
            tool_calls=(ToolCall("call-1", "lookup", {"q": "你好"}),),
        )
    ]


def test_stream_assembles_interleaved_multiple_tool_calls_in_index_order() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call-2","type":"function","function":{"name":"second","arguments":"{\\"b\\":"}},{"index":0,"id":"call-1","type":"function","function":{"name":"first","arguments":"{\\"a\\":"}}]}}]}\n\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}},{"index":1,"function":{"arguments":"2}"}}]}}]}\n\n',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [
        Finish(
            reason="tool_calls",
            tool_calls=(
                ToolCall("call-1", "first", {"a": 1}),
                ToolCall("call-2", "second", {"b": 2}),
            ),
        )
    ]


def test_stream_preserves_text_before_tool_calls_and_finishes_once() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"content":"I will check."}}]}\n\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{}"}}]}}]}\n\n',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [
        TextDelta("I will check."),
        Finish(
            reason="tool_calls",
            tool_calls=(ToolCall("call-1", "lookup", {}),),
        ),
    ]


def test_done_completes_tool_calls_without_duplicate_finish() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{}"}}]}}]}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [
        Finish(
            reason="tool_calls",
            tool_calls=(ToolCall("call-1", "lookup", {}),),
        )
    ]


@pytest.mark.parametrize(
    "chunks",
    [
        [{"choices": [{"delta": {"tool_calls": "nope"}}]}],
        [{"choices": [{"delta": {"tool_calls": [1]}}]}],
        [{"choices": [{"delta": {"tool_calls": [{"index": 0, "function": "nope"}]}}]}],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": 1}}]
                        }
                    }
                ]
            }
        ],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": True, "function": {"arguments": "{}"}}
                            ]
                        }
                    }
                ]
            }
        ],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": -1, "function": {"arguments": "{}"}}
                            ]
                        }
                    }
                ]
            }
        ],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "type": "custom",
                                    "function": {"arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "lookup"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": "[]",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                },
                                {
                                    "index": 1,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                },
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-2"}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"name": "other"}}]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [{"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}],
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ],
    ],
)
def test_malformed_tool_streams_emit_one_safe_terminal_error(
    chunks: list[dict[str, object]],
) -> None:
    provider = make_provider(
        lambda request: _sse_response(
            *(f"data: {json.dumps(chunk)}\\n\\n" for chunk in chunks),
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, MalformedResponseError)
    public_error = str(events[0].error)
    assert "call-1" not in public_error
    assert "call-2" not in public_error
    assert "lookup" not in public_error
    assert "sk-secret-value" not in public_error
    assert "traceback" not in public_error.lower()


def test_finish_reason_without_delta_preserves_plain_text_compatibility() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n',
            'data: {"choices":[{"finish_reason":"stop"}]}\n\n',
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [TextDelta("answer"), Finish(reason="stop")]
    assert not any(isinstance(event, ProviderErrorEvent) for event in events)


def test_tool_calls_finish_without_delta_assembles_completed_call() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{\\"q\\":\\"x\\"}"}}]}}]}\n\n',
            'data: {"choices":[{"finish_reason":"tool_calls"}]}\n\n',
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [
        Finish(
            reason="tool_calls",
            tool_calls=(ToolCall("call-1", "lookup", {"q": "x"}),),
        )
    ]
    assert sum(isinstance(event, Finish) for event in events) == 1


@pytest.mark.parametrize("invalid_delta", ["bad", [], None])
def test_explicit_invalid_delta_with_finish_reason_remains_malformed(
    invalid_delta: object,
) -> None:
    chunk = {"choices": [{"delta": invalid_delta, "finish_reason": "stop"}]}
    provider = make_provider(
        lambda request: _sse_response(f"data: {json.dumps(chunk)}\n\n")
    )

    events = _run_collect(provider, _make_request())

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, MalformedResponseError)


def test_malformed_tool_arguments_with_sensitive_sentinel_never_leak() -> None:
    original_arguments = '{"token":"sk-secret-value"'
    chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-secret",
                            "type": "function",
                            "function": {
                                "name": "secret_lookup",
                                "arguments": original_arguments,
                            },
                        }
                    ]
                }
            }
        ]
    }
    provider = make_provider(
        lambda request: _sse_response(
            f"data: {json.dumps(chunk)}\n\n",
            'data: {"choices":[{"finish_reason":"tool_calls"}]}\n\n',
        )
    )

    events = _run_collect(provider, _make_request())

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, MalformedResponseError)
    public_error = str(events[0].error)
    public_repr = repr(events[0].error)
    for text in (public_error, public_repr):
        assert "sk-secret-value" not in text
        assert original_arguments not in text
        assert "call-secret" not in text
        assert "secret_lookup" not in text
        assert "traceback" not in text.lower()
    assert not any(isinstance(event, (TextDelta, Finish)) for event in events)

    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{\\"q\\":"}}]}}]}\n\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"lookup","arguments":"\\"x\\"}"}}]}}]}\n\n',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [
        Finish(
            reason="tool_calls",
            tool_calls=(ToolCall("call-1", "lookup", {"q": "x"}),),
        )
    ]


@pytest.mark.parametrize("pending_reason", [None, "", "missing"])
def test_multiple_deltas_then_finish_reason(pending_reason) -> None:
    def chunk(delta):
        choice = {"delta": delta}
        if pending_reason != "missing":
            choice["finish_reason"] = pending_reason
        return f"data: {json.dumps({'choices': [choice]})}\n\n"

    provider = make_provider(
        lambda request: _sse_response(
            chunk({"role": "assistant", "content": ""}),
            chunk({"reasoning_content": "offline analysis"}),
            chunk({"content": "hello"}),
            chunk({"content": " world"}),
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [
        ProviderReasoningDelta("offline analysis"),
        _text_delta("hello"),
        _text_delta(" world"),
        Finish(reason="stop"),
    ]


def test_tool_call_arguments_survive_network_chunk_boundaries() -> None:
    payload = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"query":"你好"}',
                            },
                        }
                    ]
                }
            }
        ]
    }
    event = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
    split_at = event.index(b'\\"query\\"') + 4

    class ChunkyStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield event[:split_at]
            yield event[split_at:]
            yield b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'

    provider = make_provider(
        lambda request: httpx.Response(200, stream=ChunkyStream(), request=request)
    )

    events = _run_collect(provider, _make_request())

    assert events == [
        Finish(
            reason="tool_calls",
            tool_calls=(ToolCall("call-1", "lookup", {"query": "你好"}),),
        )
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


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        'data: {"choices":[{"delta":{"content":""},"finish_reason":"stop"}]}\n\n',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n',
        'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":""}]}\n\n',
        'data: {"choices":[{"delta":{"reasoning_content":"private offline analysis"},"finish_reason":"stop"}]}\n\n',
        'data: {"choices":[{"delta":{"content":" \\t"},"finish_reason":"stop"}]}\n\n',
    ],
)
def test_empty_completion_is_one_safe_error_without_retry(prefix) -> None:
    requests = []
    responses = []

    def handler(request):
        requests.append(request)
        response = _sse_response(prefix, DONE_SSE)
        responses.append(response)
        return response

    provider = make_provider(handler, max_retries=3)
    events = _run_collect(provider, _make_request())
    assert len(requests) == 1
    assert sum(isinstance(event, ProviderErrorEvent) for event in events) == 1
    assert isinstance(events[-1], ProviderErrorEvent)
    assert isinstance(events[-1].error, MalformedResponseError)
    assert str(events[-1].error) == "completion contained no text or tool calls"
    assert not any(isinstance(event, Finish) for event in events)
    assert all(response.is_closed for response in responses)
    assert "private offline analysis" not in str(events[-1].error)
    asyncio.run(provider.aclose())


def test_missing_or_null_content_is_not_rendered_as_none() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{}}]}\n\n',
            'data: {"choices":[{"delta":{"content":null}}]}\n\n',
            VALID_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [TextDelta("ok"), Finish()]
    assert all(getattr(event, "text", None) != "None" for event in events)


def test_empty_choices_chunk_is_malformed_error() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[]}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    # 空 choices 是畸形响应；错误事件为流内最后事件，不再消费 [DONE]。
    assert [type(event).__name__ for event in events] == ["ProviderErrorEvent"]
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, MalformedResponseError)


def test_missing_choices_field_is_malformed_error() -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"id":"x","object":"chat.completion.chunk"}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    # 缺失 choices 的补充事件不是合法心跳；畸形错误为最后事件。
    assert [type(event).__name__ for event in events] == ["ProviderErrorEvent"]
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, MalformedResponseError)


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


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter"])
def test_finish_reason_then_done_yields_single_finish(reason) -> None:
    provider = make_provider(
        lambda request: _sse_response(
            'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n',
            f'data: {{"choices":[{{"delta":{{}},"finish_reason":"{reason}"}}]}}\n\n',
            DONE_SSE,
        )
    )

    events = _run_collect(provider, _make_request())

    assert events == [TextDelta("answer"), Finish(reason=reason)]


# ---------------------------------------------------------------------------
# HTTP 错误分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationError),
        (403, AuthenticationError),
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


def test_http_408_timeout_is_retried_and_preserves_classification() -> None:
    calls = 0
    sleep_calls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(408, request=request)

    async def recorded_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    provider = OpenAICompatibleProvider(
        api_key="test-api-key",
        base_url="https://api.example.com/v1",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        retry_backoff=0.5,
        sleep=recorded_sleep,
    )

    events = _run_collect(provider, _make_request())

    assert calls == 2
    assert sleep_calls == [0.5]
    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, ProviderTimeoutError)


@pytest.mark.parametrize("status", [400, 404, 418, 422])
def test_other_http_errors_map_to_unknown_provider_error(status: int) -> None:
    provider = make_provider(lambda request: httpx.Response(status, request=request))

    events = _run_collect(provider, _make_request())

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert type(events[0].error) is ProviderError


def test_error_diagnostics_never_echo_nested_sensitive_response_body() -> None:
    provider = make_provider(
        lambda request: httpx.Response(
            500,
            json={
                "error": {
                    "message": (
                        "invalid api_key=sk-live-secret-123456 "
                        "Authorization: Bearer sk-top-secret-123456"
                    )
                },
                "trace": {"internal": "full-sensitive-json-payload"},
            },
            request=request,
        )
    )

    events = _run_collect(provider, _make_request())

    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    rendered = f"{events[0].error!s} {events[0].error!r} {events[0]!r}"
    for sensitive in (
        "sk-live-secret-123456",
        "sk-top-secret-123456",
        "full-sensitive-json-payload",
        "Authorization",
        "trace",
    ):
        assert sensitive not in rendered
    assert len(str(events[0].error)) < 100


def test_error_event_is_terminal_and_stream_ends_immediately() -> None:
    provider = make_provider(lambda request: httpx.Response(401, request=request))

    events = _run_collect(provider, _make_request())

    assert [type(event).__name__ for event in events] == ["ProviderErrorEvent"]


# ---------------------------------------------------------------------------
# 畸形响应
# ---------------------------------------------------------------------------


def test_invalid_json_data_is_malformed_error_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _sse_response(
            "data: not-json-at-all\n\n",
            DONE_SSE,
        )

    provider = make_provider(handler, max_retries=5)

    events = _run_collect(provider, _make_request())

    assert calls == 1
    assert [type(event).__name__ for event in events] == ["ProviderErrorEvent"]
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, MalformedResponseError)


@pytest.mark.parametrize(
    "chunk",
    [
        'data: {"choices":{}}\n\n',
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
    """产出部分文本后抛出指定传输异常的响应流。"""

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
        return httpx.Response(200, text=VALID_SSE, request=request)

    sleep_calls: list[float] = []
    provider = _make_flaky_provider(handler, max_retries=2, sleep_calls=sleep_calls)

    events = _run_collect(provider, _make_request())

    assert calls["n"] == 2
    assert events == [TextDelta("ok"), Finish()]
    assert sleep_calls == [0.5]


def test_retry_count_is_capped() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, request=request)

    sleep_calls: list[float] = []
    provider = _make_flaky_provider(handler, max_retries=2, sleep_calls=sleep_calls)

    events = _run_collect(provider, _make_request())

    assert calls["n"] == 3
    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, TemporaryServerError)
    assert sleep_calls == [0.5, 0.5]


def test_auth_error_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, request=request)

    provider = make_provider(handler, max_retries=5)

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
        return httpx.Response(200, text=VALID_SSE, request=request)

    sleep_calls: list[float] = []
    provider = _make_flaky_provider(handler, max_retries=2, sleep_calls=sleep_calls)

    events = _run_collect(provider, _make_request())

    assert calls["n"] == 2
    assert sleep_calls == [0.5]
    assert events == [TextDelta("ok"), Finish()]


def test_no_retry_after_tool_call_delta_has_been_produced() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            stream=_FlakyStream(
                [
                    b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"lookup","arguments":"{\\"q\\":"}}]}}]}\n\n'
                ],
                exc=httpx.ReadTimeout("read timed out"),
            ),
            request=request,
        )

    provider = _make_flaky_provider(handler, max_retries=3)
    events = _run_collect(provider, _make_request())

    assert calls["n"] == 1
    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, MalformedResponseError)


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
    # 响应已建立后 SSE 消费中超时 → 畸形响应（内容不完整）。
    assert isinstance(events[-1], ProviderErrorEvent)
    assert isinstance(events[-1].error, MalformedResponseError)


def test_stream_interrupted_after_response_is_malformed_without_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            stream=_FlakyStream(
                [b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'],
                exc=httpx.ReadError("connection reset"),
            ),
            request=request,
        )

    sleep_calls: list[float] = []
    provider = _make_flaky_provider(handler, max_retries=2, sleep_calls=sleep_calls)

    events = _run_collect(provider, _make_request())

    # HTTP 200 建立后传输中断 → 畸形；不重试、不重复发送请求，
    # 已产出的文本增量保留。
    assert calls["n"] == 1
    assert sleep_calls == []
    assert [type(event).__name__ for event in events] == [
        "TextDelta",
        "ProviderErrorEvent",
    ]
    assert isinstance(events[-1], ProviderErrorEvent)
    assert isinstance(events[-1].error, MalformedResponseError)


def test_stream_interrupted_before_any_text_is_malformed_error() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            stream=_FlakyStream([], exc=httpx.ReadError("connection reset")),
            request=request,
        )

    provider = make_provider(handler, max_retries=3)

    events = _run_collect(provider, _make_request())

    # 未产出文本时中断同样视为畸形（响应已建立）。
    assert calls["n"] == 1
    assert [type(event).__name__ for event in events] == ["ProviderErrorEvent"]
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, MalformedResponseError)


@pytest.mark.parametrize(
    ("max_retries", "expected_requests"),
    [(1, 2), (2, 3)],
)
def test_connect_timeout_exhausts_retries_with_timeout_error(
    max_retries: int, expected_requests: int
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("connection timed out")

    provider = make_provider(handler, max_retries=max_retries)

    events = _run_collect(provider, _make_request())

    assert calls["n"] == expected_requests
    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, ProviderTimeoutError)


def test_transport_error_before_response_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, text=VALID_SSE, request=request)

    provider = make_provider(handler, max_retries=2)

    events = _run_collect(provider, _make_request())

    assert calls["n"] == 2
    assert events == [TextDelta("ok"), Finish()]


def test_transport_error_before_response_exhausts_retries() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    provider = make_provider(handler, max_retries=1)

    events = _run_collect(provider, _make_request())

    # 响应建立前的传输错误保持 TemporaryServerError 分类并遵守重试上限。
    assert calls["n"] == 2  # 初始尝试 + 1 次重试
    assert len(events) == 1
    assert isinstance(events[0], ProviderErrorEvent)
    assert isinstance(events[0].error, TemporaryServerError)


# ---------------------------------------------------------------------------
# 取消与资源生命周期
# ---------------------------------------------------------------------------


class _BlockingStream(httpx.AsyncByteStream):
    """输出一个事件，然后暴露确定的取消点。"""

    def __init__(self) -> None:
        self.blocked = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        self.blocked.set()
        await asyncio.Event().wait()


def _make_recording_provider(
    transport: RecordingTransport,
    *,
    max_retries: int = 0,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key="test-api-key",
        base_url="https://api.example.com/v1",
        transport=transport,
        max_retries=max_retries,
        sleep=_noop_sleep,
    )


def test_cancellation_propagates_and_is_not_wrapped_as_error_event() -> None:
    stream = _BlockingStream()
    provider = make_provider(
        lambda request: httpx.Response(200, stream=stream, request=request)
    )

    async def main() -> None:
        collected: list[ProviderEvent] = []
        task = asyncio.create_task(_collect(provider, _make_request(), collected))
        await stream.blocked.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert collected == [TextDelta("a")]
        assert not any(isinstance(event, ProviderErrorEvent) for event in collected)
        await provider.aclose()

    asyncio.run(main())


def test_success_closes_response_and_explicit_aclose_closes_transport() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(content=VALID_SSE.encode())

    transport = RecordingTransport(handler)
    provider = _make_recording_provider(transport)

    async def scenario() -> None:
        events = await _collect(provider, _make_request())

        assert events == [TextDelta("ok"), Finish()]
        assert transport.responses[0].is_closed
        assert transport.aclose_count == 0
        assert all(
            isinstance(event, (TextDelta, Finish, ProviderErrorEvent))
            for event in events
        )
        assert not any(isinstance(event, httpx.Response) for event in events)

        await provider.aclose()
        assert transport.aclose_count >= 1

    asyncio.run(scenario())


def test_error_closes_response_and_explicit_aclose_closes_transport() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(status=500, content=b'{"error":"sensitive"}')

    transport = RecordingTransport(handler)
    provider = _make_recording_provider(transport, max_retries=1)

    async def scenario() -> None:
        events = await _collect(provider, _make_request())

        assert len(events) == 1
        assert isinstance(events[0], ProviderErrorEvent)
        assert isinstance(events[0].error, TemporaryServerError)
        assert len(transport.responses) == 2
        assert all(response.is_closed for response in transport.responses)
        assert transport.aclose_count == 0
        assert all(
            isinstance(event, (TextDelta, Finish, ProviderErrorEvent))
            for event in events
        )
        assert not any(isinstance(event, httpx.Response) for event in events)

        await provider.aclose()
        assert transport.aclose_count >= 1

    asyncio.run(scenario())


def test_cancellation_closes_response_and_explicit_aclose_closes_transport() -> None:
    stream = _BlockingStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(content=stream)

    transport = RecordingTransport(handler)
    provider = _make_recording_provider(transport)

    async def scenario() -> None:
        collected: list[ProviderEvent] = []
        task = asyncio.create_task(_collect(provider, _make_request(), collected))
        await stream.blocked.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert collected == [TextDelta("a")]
        assert transport.responses[0].is_closed
        assert transport.aclose_count == 0
        assert all(
            isinstance(event, (TextDelta, Finish, ProviderErrorEvent))
            for event in collected
        )
        assert not any(isinstance(event, httpx.Response) for event in collected)

        await provider.aclose()
        assert transport.aclose_count >= 1

    asyncio.run(scenario())


def test_stream_can_be_consumed_multiple_times_with_reused_client() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text=VALID_SSE, request=request)

    provider = make_provider(handler)

    first = _run_collect(provider, _make_request())
    second = _run_collect(provider, _make_request())

    assert first == [TextDelta("ok"), Finish()]
    assert second == [TextDelta("ok"), Finish()]
    assert calls["n"] == 2
    asyncio.run(provider.aclose())


def test_aclose_is_idempotent() -> None:
    provider = make_provider(
        lambda request: httpx.Response(200, text=VALID_SSE, request=request)
    )

    asyncio.run(provider.aclose())
    asyncio.run(provider.aclose())  # 第二次调用不抛异常
