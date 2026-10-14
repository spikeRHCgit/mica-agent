"""Messages 的真实适配器、Engine 和资源边界，仅使用 fake key 与替身传输。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from sse_transport_helpers import RecordingTransport, sse_response

from mica_agent.continuation_contracts import (
    MAX_TURN_CONTINUATION_BYTES,
    ContinuationStatus,
    MessagesRedactedThinkingItem,
    MessagesThinkingItem,
    ModelStepContinuation,
    ProviderApi,
    ResponsesReasoningItem,
    TextItem,
    ToolCallItem,
    continuation_size,
)
from mica_agent.engine import AgentEngine
from mica_agent.events import RunCompleted, RunFailed, ToolFinished
from mica_agent.models import Message, MessageRole
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine, PolicyInput, PolicyOperation
from mica_agent.providers.anthropic_messages import (
    AnthropicMessagesProvider,
    _MessagesStep,
    resolve_endpoint,
)
from mica_agent.providers.base import (
    AuthenticationError,
    Finish,
    MalformedResponseError,
    ProviderError,
    ProviderErrorEvent,
    ProviderReasoningDelta,
    ProviderRequest,
    ProviderTimeoutError,
    RateLimitError,
    TemporaryServerError,
    TextDelta,
)
from mica_agent.reasoning import ReasoningEffort
from mica_agent.tool_contracts import ToolCall, ToolResult, ToolResultStatus, ToolSpec
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_registry import ToolRegistry


def request(**changes):
    return replace(
        ProviderRequest(
            "test-model",
            (Message(MessageRole.USER, "hello"),),
            provider_id="test-source",
            api=ProviderApi.ANTHROPIC_MESSAGES,
        ),
        **changes,
    )


def record(items=(), **changes):
    return replace(
        ModelStepContinuation(
            "test-source",
            "test-model",
            changes.pop("api", ProviderApi.ANTHROPIC_MESSAGES),
            ContinuationStatus.REPLAYABLE if items else ContinuationStatus.UNREPLAYABLE,
            tuple(items),
        ),
        **changes,
    )


def frame(kind, **fields):
    return (
        "event: "
        + kind
        + "\ndata: "
        + json.dumps({"type": kind, **fields}, ensure_ascii=False)
        + "\n\n"
    )


def start(**fields):
    return frame(
        "message_start",
        message={
            "id": "msg-one",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": "test-model",
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 0},
            **fields,
        },
    )


def block_start(index, block):
    return frame("content_block_start", index=index, content_block=block)


def delta(index, kind, **fields):
    return frame("content_block_delta", index=index, delta={"type": kind, **fields})


def block_stop(index):
    return frame("content_block_stop", index=index)


def ending(reason="end_turn"):
    return [
        frame(
            "message_delta",
            delta={"stop_reason": reason, "stop_sequence": None},
            usage={"output_tokens": 8},
        ),
        frame("message_stop"),
    ]


def sample(blocks=(), reason="end_turn"):
    return [
        start(),
        *[
            part
            for i, block in enumerate(blocks)
            for part in (block_start(i, block), block_stop(i))
        ],
        *ending(reason),
    ]


def text(value="answer", **fields):
    return {"type": "text", "text": value, **fields}


def thinking(value="thought", signature="signature-one", **fields):
    return {"type": "thinking", "thinking": value, "signature": signature, **fields}


def tool(call_id="call-one", query="x", **fields):
    return {
        "type": "tool_use",
        "id": call_id,
        "name": "lookup",
        "input": {"query": query},
        **fields,
    }


async def collect(chunks, req=None, **options):
    async def handler(_):
        return sse_response(chunks)

    transport = RecordingTransport(handler)
    provider = AnthropicMessagesProvider(
        "fake-test-key", transport=transport, max_retries=0, **options
    )
    try:
        events = [event async for event in provider.stream(req or request())]
    finally:
        await provider.aclose()
        await provider.aclose()
    assert all(response.is_closed for response in transport.responses)
    assert transport.aclose_count == 1
    return events, transport


def run(chunks, req=None, **options):
    return asyncio.run(collect(chunks, req, **options))


def assert_error(events, kind=MalformedResponseError):
    assert isinstance(events[-1], ProviderErrorEvent)
    assert type(events[-1].error) is kind
    assert sum(isinstance(event, ProviderErrorEvent) for event in events) == 1
    assert not any(isinstance(event, Finish) for event in events)
    diagnostic = str(events[-1].error) + repr(events[-1].error)
    assert not any(
        value in diagnostic
        for value in (
            "fake-test-key",
            "raw-error-secret",
            "signature-one",
            "private/thought",
            "opaque-data",
            "secret/query",
        )
    )


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.invalid/v1", "https://example.invalid/v1/messages"),
        ("https://example.invalid/v1/messages/", "https://example.invalid/v1/messages"),
        (
            "https://username:password@example.invalid/v1/messages/?x=1",
            "https://example.invalid/v1/messages?x=1",
        ),
    ],
)
def test_request_literal_endpoint_auth_system_schema_budget(url, expected):
    spec = ToolSpec(
        "lookup",
        "Lookup",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    req = request(
        messages=(
            Message(MessageRole.SYSTEM, "first"),
            Message(MessageRole.SYSTEM, "second"),
            Message(MessageRole.USER, "hello"),
        ),
        tools=(spec,),
        max_tokens=17,
    )
    events, transport = run(sample([text()]), req, base_url=url)
    assert isinstance(events[-1], Finish)
    sent = transport.requests[0]
    assert str(sent.url) == expected == resolve_endpoint(url)
    assert sent.method == "POST"
    assert sent.headers["x-api-key"] == "fake-test-key"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert sent.headers["content-type"] == "application/json"
    assert "authorization" not in sent.headers and "anthropic-beta" not in sent.headers
    assert json.loads(sent.content) == {
        "model": "test-model",
        "stream": True,
        "max_tokens": 17,
        "thinking": {"type": "disabled"},
        "system": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        "tools": [
            {
                "name": "lookup",
                "description": "Lookup",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ],
    }


@pytest.mark.parametrize("effort", list(ReasoningEffort))
@pytest.mark.parametrize("mapping", [None, {}])
def test_default_effort_mapping_and_minimal_rejection(effort, mapping):
    events, transport = run(
        sample(), request(reasoning_effort=effort, thinking_level_map=mapping)
    )
    if effort is ReasoningEffort.MINIMAL:
        assert_error(events, ProviderError)
        assert not transport.requests
        return
    payload = json.loads(transport.requests[0].content)
    assert payload["max_tokens"] == 4096
    if effort is ReasoningEffort.OFF:
        assert payload["thinking"] == {"type": "disabled"}
        assert "output_config" not in payload
    else:
        assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert payload["output_config"] == {"effort": effort.value}
    assert "budget_tokens" not in str(payload) and "beta" not in str(payload)


@pytest.mark.parametrize(
    "request_map,expected",
    [(None, "low"), ({}, "high"), ({ReasoningEffort.HIGH: "max"}, "max")],
)
def test_explicit_mapping_precedence(request_map, expected):
    _, transport = run(
        sample(),
        request(reasoning_effort=ReasoningEffort.HIGH, thinking_level_map=request_map),
        thinking_level_map={ReasoningEffort.HIGH: "low"},
    )
    assert json.loads(transport.requests[0].content)["output_config"] == {
        "effort": expected
    }


def test_explicit_minimal_and_invalid_or_missing_mapping():
    events, transport = run(
        sample(),
        request(
            reasoning_effort=ReasoningEffort.MINIMAL,
            thinking_level_map={ReasoningEffort.MINIMAL: "low"},
        ),
    )
    assert isinstance(events[-1], Finish)
    assert json.loads(transport.requests[0].content)["output_config"] == {
        "effort": "low"
    }
    for mapping in ({ReasoningEffort.HIGH: "banana"}, {ReasoningEffort.LOW: "low"}):
        events, transport = run(
            sample(),
            request(reasoning_effort=ReasoningEffort.HIGH, thinking_level_map=mapping),
        )
        assert_error(events, ProviderError)
        assert not transport.requests
    with pytest.raises(ValueError, match="thinking_level_map is invalid"):
        AnthropicMessagesProvider(
            "fake-test-key", thinking_level_map={ReasoningEffort.HIGH: "banana"}
        )


@pytest.mark.parametrize(
    "req",
    [
        request(api=ProviderApi.OPENAI_COMPLETIONS),
        request(
            messages=(
                Message(MessageRole.USER, "hello"),
                Message(MessageRole.SYSTEM, "secret/query"),
            )
        ),
        request(messages=(Message(MessageRole.USER, "\ud800"),)),
    ],
)
def test_request_rejected_before_transport(req):
    events, transport = run(sample(), req)
    assert_error(events, ProviderError)
    assert not transport.requests


@pytest.mark.parametrize(
    "blocks,reason,calls,items,visible",
    [
        (
            [thinking()],
            "end_turn",
            (),
            (MessagesThinkingItem("thought", "signature-one"),),
            [ProviderReasoningDelta("thought")],
        ),
        (
            [thinking("")],
            "end_turn",
            (),
            (MessagesThinkingItem("", "signature-one"),),
            [],
        ),
        (
            [{"type": "redacted_thinking", "data": "opaque-data"}],
            "end_turn",
            (),
            (MessagesRedactedThinkingItem("opaque-data"),),
            [],
        ),
        ([text()], "end_turn", (), (TextItem(None),), [TextDelta("answer")]),
        (
            [tool()],
            "tool_use",
            (ToolCall("call-one", "lookup", {"query": "x"}),),
            (ToolCallItem("call-one"),),
            [],
        ),
        ([], "end_turn", (), (), []),
        ([text("")], "end_turn", (), (), []),
        ([text()], "stop_sequence", (), (TextItem(None),), [TextDelta("answer")]),
    ],
)
@pytest.mark.parametrize("source", ["test-source", None])
def test_complete_finish_shapes_and_known_unknown_source(
    blocks, reason, calls, items, visible, source
):
    events, _ = run(sample(blocks, reason), request(provider_id=source))
    assert events == [
        *visible,
        Finish(reason, calls, record(items) if source and items else None),
    ]


def test_sse_multiline_comments_ping_signature_and_json_fragments():
    chunks = [
        ": comment\n\n",
        start(),
        frame("ping"),
        block_start(0, thinking("initial", "")),
        delta(0, "thinking_delta", thinking=" plus"),
        delta(0, "signature_delta", signature="signature-"),
        delta(0, "signature_delta", signature="one"),
        block_stop(0),
        block_start(1, text("begin")),
        'event: content_block_delta\ndata: {"type":"content_block_delta",\ndata: "index":1,"delta":{"type":"text_delta","text":" end"}}\n\n',
        block_stop(1),
        block_start(2, tool(input={})),
        delta(2, "input_json_delta", partial_json='{"query":'),
        delta(2, "input_json_delta", partial_json='"x"}'),
        block_stop(2),
        *ending("tool_use"),
        "data: invalid-after-terminal\n\n",
    ]
    events, _ = run(chunks)
    assert events == [
        ProviderReasoningDelta("initial"),
        ProviderReasoningDelta(" plus"),
        TextDelta("begin"),
        TextDelta(" end"),
        Finish(
            "tool_use",
            (ToolCall("call-one", "lookup", {"query": "x"}),),
            record(
                (
                    MessagesThinkingItem("initial plus", "signature-one"),
                    TextItem(None),
                    ToolCallItem("call-one"),
                )
            ),
        ),
    ]


@pytest.mark.parametrize(
    "blocks,extra",
    [
        ([thinking(signature="")], []),
        ([thinking(signature="bad\nvalue")], []),
        ([{"type": "redacted_thinking"}], []),
        ([{"type": "redacted_thinking", "data": "bad\nvalue"}], []),
        ([thinking(new_semantic="secret/query"), text()], []),
        ([text(), text("second")], []),
        ([text(citations=[{"type": "future"}])], []),
        (
            [
                {
                    "type": "server_tool_use",
                    "id": "server-one",
                    "name": "lookup",
                    "input": {},
                }
            ],
            [],
        ),
        ([text()], [frame("future_event", data="opaque-data")]),
    ],
)
def test_real_degradation_keeps_generic_facts(blocks, extra):
    chunks = sample(blocks)
    chunks[-2:-2] = extra
    events, _ = run(chunks)
    assert events[-1] == Finish("end_turn", (), record())
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "".join(
        b["text"] for b in blocks if b["type"] == "text"
    )


@pytest.mark.parametrize("where", ["start", "delta", "block_delta", "stop"])
def test_unknown_semantic_field_degrades_and_wrappers_do_not(where):
    chunks = [
        start(),
        block_start(0, text("")),
        delta(0, "text_delta", text="answer"),
        block_stop(0),
        *ending(),
    ]
    position = {"start": 0, "delta": 4, "block_delta": 2, "stop": 5}[where]
    raw = json.loads(chunks[position].split("data: ", 1)[1])
    target = raw["delta"] if where in ("delta", "block_delta") else raw
    target["future"] = "raw-error-secret"
    chunks[position] = frame(raw.pop("type"), **raw)
    events, _ = run(chunks)
    assert events == [TextDelta("answer"), Finish("end_turn", (), record())]


def history(call_ids=("call-one",), content="answer"):
    return (
        Message(MessageRole.USER, "hello"),
        Message(
            MessageRole.ASSISTANT,
            content,
            tuple(
                ToolCall(cid, "lookup", {"query": str(i)})
                for i, cid in enumerate(call_ids)
            ),
        ),
        *(
            Message(MessageRole.TOOL, "true result " + str(i), tool_call_id=cid)
            for i, cid in enumerate(call_ids)
        ),
        Message(MessageRole.USER, "next"),
    )


def test_id_mapping_reserves_legal_ids_is_deterministic_and_does_not_mutate_history():
    messages = history(("bad/id", "mica_tool_0", "bad.id", "合法"))
    req = request(messages=messages)
    _, transport = run(sample(), req)
    _, again = run(sample(), req)
    payload = json.loads(transport.requests[0].content)
    assert payload == json.loads(again.requests[0].content)
    assert [c["id"] for c in payload["messages"][1]["content"][1:]] == [
        "mica_tool_1",
        "mica_tool_0",
        "mica_tool_2",
        "mica_tool_3",
    ]
    assert payload["messages"][2] == {
        "role": "user",
        "content": [
            *(
                {
                    "type": "tool_result",
                    "tool_use_id": cid,
                    "content": "true result " + str(i),
                }
                for i, cid in enumerate(
                    ("mica_tool_1", "mica_tool_0", "mica_tool_2", "mica_tool_3")
                )
            ),
            {"type": "text", "text": "next"},
        ],
    }
    assert req.messages is messages
    assert tuple(c.call_id for c in messages[1].tool_calls) == (
        "bad/id",
        "mica_tool_0",
        "bad.id",
        "合法",
    )


@pytest.mark.parametrize(
    "mismatch", ["none", "unreplayable", "provider", "model", "api", "unknown_source"]
)
def test_replay_binding_and_generic_history(mismatch):
    items = (
        MessagesThinkingItem("private/thought", "signature-one"),
        ToolCallItem("call-one"),
        TextItem(None),
    )
    rec = record(items)
    source = "test-source"
    if mismatch == "none":
        rec = None
    elif mismatch == "unreplayable":
        rec = record()
    elif mismatch == "provider":
        rec = replace(rec, provider_id="other")
    elif mismatch == "model":
        rec = replace(rec, model_id="other")
    elif mismatch == "api":
        rec = record(
            (
                ResponsesReasoningItem("reason", (), "opaque-data"),
                ToolCallItem("call-one"),
                TextItem(None),
            ),
            api=ProviderApi.OPENAI_RESPONSES,
        )
    else:
        source = None
    _, transport = run(
        sample(), request(messages=history(), provider_id=source, continuations=(rec,))
    )
    payload = json.loads(transport.requests[0].content)
    assert payload["messages"][1]["content"] == [text(), tool(query="0")]
    assert "private/thought" not in str(payload) and "signature-one" not in str(payload)


def test_compatible_replay_order_and_all_assistant_slots_are_consumed():
    messages = (
        Message(MessageRole.USER, "hello"),
        Message(MessageRole.ASSISTANT, ""),
        Message(MessageRole.USER, "again"),
        *history()[1:],
    )
    rec = record(
        (
            MessagesThinkingItem("private/thought", "signature-one"),
            ToolCallItem("call-one"),
            TextItem(None),
            MessagesRedactedThinkingItem("opaque-data"),
        )
    )
    _, transport = run(sample(), request(messages=messages, continuations=(None, rec)))
    payload = json.loads(transport.requests[0].content)
    assert payload["messages"][1] == {"role": "assistant", "content": [text("")]}
    assert payload["messages"][3]["content"] == [
        thinking("private/thought"),
        tool(query="0"),
        text(),
        {"type": "redacted_thinking", "data": "opaque-data"},
    ]


@pytest.mark.parametrize(
    "private_item",
    [
        MessagesThinkingItem("thought", "signature-one"),
        MessagesRedactedThinkingItem("opaque-data"),
    ],
)
def test_signed_history_rejects_id_rewrite(private_item):
    req = request(
        messages=history(("bad/id",)),
        continuations=(record((private_item, TextItem(None), ToolCallItem("bad/id"))),),
    )
    events, transport = run(sample(), req)
    assert_error(events, ProviderError)
    assert not transport.requests


@pytest.mark.parametrize(
    "mode", ["orphan", "duplicate", "missing", "before", "intervening", "repeat_call"]
)
def test_invalid_history_tool_associations_are_rejected_without_http(mode):
    messages = list(history())
    if mode == "orphan":
        messages[2] = Message(MessageRole.TOOL, "secret/query", tool_call_id="orphan")
    elif mode == "duplicate":
        messages.insert(3, messages[2])
    elif mode == "missing":
        messages.pop(2)
    elif mode == "before":
        messages[1], messages[2] = messages[2], messages[1]
    elif mode == "intervening":
        messages.insert(2, Message(MessageRole.USER, "intervening"))
    else:
        messages.extend(messages[1:3])
    events, transport = run(sample(), request(messages=tuple(messages)))
    assert_error(events, ProviderError)
    assert not transport.requests


@pytest.mark.parametrize(
    "reason",
    ["max_tokens", "model_context_window_exceeded", "refusal", "pause_turn", "future"],
)
def test_incomplete_refused_paused_unknown_reason_preserves_visible_output(reason):
    events, _ = run(
        sample(
            [thinking("private/thought"), text(), tool(query="secret/query")], reason
        )
    )
    assert_error(events, ProviderError)
    assert events[:2] == [
        ProviderReasoningDelta("private/thought"),
        TextDelta("answer"),
    ]


@pytest.mark.parametrize(
    "error_type,classification",
    [
        ("authentication_error", AuthenticationError),
        ("permission_error", AuthenticationError),
        ("rate_limit_error", RateLimitError),
        ("timeout_error", ProviderTimeoutError),
        ("overloaded_error", TemporaryServerError),
        ("api_error", TemporaryServerError),
        ("unknown", ProviderError),
    ],
)
def test_stream_errors_are_safe_and_never_retried(error_type, classification):
    events, transport = run(
        [
            start(),
            block_start(0, text()),
            block_stop(0),
            frame("error", error={"type": error_type, "message": "raw-error-secret"}),
        ]
    )
    assert_error(events, classification)
    assert events[0] == TextDelta("answer") and len(transport.requests) == 1


@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [start()],
        [start(), block_start(0, text()), block_stop(0)],
        [start(), *ending()[:-1]],
        ["data: [DONE]\n\n"],
        [start(), frame("message_stop")],
        [start(), start(), *ending()],
        [block_start(0, text()), *ending()],
        [start(), block_stop(0), *ending()],
        [start(), block_start(1, text()), *ending()],
        [start(), block_start(True, text()), *ending()],
        [start(), block_start(0, text()), block_start(0, text()), *ending()],
        [start(), block_start(0, text()), *ending()],
        [start(), block_start(0, text()), block_stop(0), block_stop(0), *ending()],
        [
            start(),
            block_start(0, text()),
            block_stop(0),
            delta(0, "text_delta", text="late"),
            *ending(),
        ],
        [
            start(),
            block_start(0, thinking()),
            delta(0, "text_delta", text="bad"),
            block_stop(0),
            *ending(),
        ],
        [start(), *ending()[:-1], block_start(0, text()), frame("message_stop")],
        [
            start(),
            block_start(0, tool()),
            block_stop(0),
            block_start(1, tool()),
            block_stop(1),
            *ending("tool_use"),
        ],
        sample([tool(name="bad.name")], "tool_use"),
        sample([tool()], "end_turn"),
        sample([text()], "tool_use"),
        [start(), 'event: ping\ndata: {"type":"message_stop"}\n\n'],
        [start(), 'data: {"type":"ping","type":"ping"}\n\n'],
        [
            start(),
            block_start(0, tool()),
            delta(0, "input_json_delta", partial_json="{}"),
            block_stop(0),
            *ending("tool_use"),
        ],
        [
            start(),
            frame("message_delta", delta={"stop_reason": "end_turn"}),
            frame("message_delta", delta={"stop_reason": "tool_use"}),
            frame("message_stop"),
        ],
        [start(), *ending()[:-1], frame("message_stop").rstrip("\n")],
    ],
)
def test_malformed_lifecycle_identity_sse_and_terminal_consistency(chunks):
    events, _ = run(chunks)
    assert_error(events)


@pytest.mark.parametrize(
    "arguments",
    [
        "[]",
        "null",
        '"secret/query"',
        "{",
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":1e999}',
        '{"x":"\\ud800"}',
        '{"\\ud800":1}',
        "",
    ],
)
def test_invalid_fragment_json_is_rejected(arguments):
    events, _ = run(
        [
            start(),
            block_start(0, tool(input={})),
            delta(0, "input_json_delta", partial_json=arguments),
            block_stop(0),
            *ending("tool_use"),
        ]
    )
    assert_error(events)


@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize("seed", ["a", '中\\\n"'])
def test_exact_wire_budget_with_utf8_and_escaping(offset, seed):
    base = record((MessagesThinkingItem(seed, "signature-one"), TextItem(None)))
    thought = seed + "x" * (
        MAX_TURN_CONTINUATION_BYTES - continuation_size(base) + offset
    )
    expected = record((MessagesThinkingItem(thought, "signature-one"), TextItem(None)))
    assert continuation_size(expected) == MAX_TURN_CONTINUATION_BYTES + offset
    events, _ = run(sample([thinking(thought), text()]))
    assert events[-1] == Finish("end_turn", (), record() if offset else expected)
    assert events[:2] == [ProviderReasoningDelta(thought), TextDelta("answer")]


def test_incremental_overflow_releases_private_data_and_is_sticky():
    # 专属内存释放没有公共事件可观察，单独检查本适配器的累积器。
    step = _MessagesStep(request())
    for chunk in [
        start(),
        block_start(0, thinking("", "")),
        delta(0, "thinking_delta", thinking="x" * MAX_TURN_CONTINUATION_BYTES),
    ]:
        step.consume(json.loads(chunk.split("data: ", 1)[1]))
    assert step.discarded and step.blocks[0].thinking.saved is None
    assert step.blocks[0].signature.saved is None
    for chunk in [
        delta(0, "signature_delta", signature="signature-one"),
        block_stop(0),
        block_start(1, thinking("later", "signature-two")),
        block_stop(1),
        block_start(2, {"type": "redacted_thinking", "data": "opaque-data"}),
        block_stop(2),
        *ending(),
    ]:
        emitted = step.consume(json.loads(chunk.split("data: ", 1)[1]))
    assert emitted == [Finish("end_turn", (), record())]
    assert all(
        b.thinking.saved is b.signature.saved is b.data.saved is None
        for b in step.blocks
    )
    assert step.blocks[0].thinking.length == MAX_TURN_CONTINUATION_BYTES


def executor_for():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "lookup",
            "Lookup",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        lookup,
    )
    return registry, ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda req, spec: PolicyInput(
            req.name, PolicyOperation.READ, False, False
        ),
    )


async def lookup(req, cancellation):
    cancellation.raise_if_cancelled()
    return ToolResult(
        req.call_id,
        req.name,
        ToolResultStatus.SUCCESS,
        "found " + req.arguments["query"],
    )


def test_real_engine_two_step_multi_tool_roundtrip_and_original_order():
    first = sample(
        [thinking(), tool(), text("checking"), tool("call-two", "y")], "tool_use"
    )
    first_events, _ = run(first)
    expected = record(
        (
            MessagesThinkingItem("thought", "signature-one"),
            ToolCallItem("call-one"),
            TextItem(None),
            ToolCallItem("call-two"),
        )
    )
    assert first_events[-1] == Finish(
        "tool_use",
        (
            ToolCall("call-one", "lookup", {"query": "x"}),
            ToolCall("call-two", "lookup", {"query": "y"}),
        ),
        expected,
    )

    async def scenario():
        records, messages = [], []
        registry, executor = executor_for()

        async def handler(_):
            return sse_response(
                first if len(transport.requests) == 1 else sample([text("finished")])
            )

        transport = RecordingTransport(handler)
        provider = AnthropicMessagesProvider("fake-test-key", transport=transport)
        req = request(
            messages=(
                Message(MessageRole.SYSTEM, "system"),
                Message(MessageRole.USER, "hello"),
            ),
            tools=registry.specs(),
        )
        try:
            events = [
                e
                async for e in AgentEngine(provider, tool_executor=executor).run(
                    req,
                    completed_continuations_sink=records.append,
                    completed_messages_sink=messages.append,
                )
            ]
        finally:
            await provider.aclose()
        assert isinstance(events[-1], RunCompleted)
        assert [e.call_id for e in events if isinstance(e, ToolFinished)] == [
            "call-one",
            "call-two",
        ]
        assert records == [(expected, record((TextItem(None),)))]
        assert messages[0][0] == Message(
            MessageRole.ASSISTANT, "checking", first_events[-1].tool_calls
        )
        bodies = [json.loads(r.content) for r in transport.requests]
        assert len(bodies) == 2
        assert bodies[1] == {
            **bodies[0],
            "messages": [
                *bodies[0]["messages"],
                {
                    "role": "assistant",
                    "content": [
                        thinking(),
                        tool(),
                        text("checking"),
                        tool("call-two", "y"),
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-one",
                            "content": '{"status":"success","message":"found x","data":null}',
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-two",
                            "content": '{"status":"success","message":"found y","data":null}',
                        },
                    ],
                },
            ],
        }
        assert req.messages == (
            Message(MessageRole.SYSTEM, "system"),
            Message(MessageRole.USER, "hello"),
        )
        assert all(r.is_closed for r in transport.responses)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode", ["invalid_json", "max_tokens", "pause_turn", "missing_stop", "server_tool"]
)
def test_real_engine_never_executes_incomplete_or_server_tools(mode):
    async def scenario():
        observed = []
        registry = ToolRegistry()

        async def observe(req, cancellation):
            observed.append(req.call_id)
            return await lookup(req, cancellation)

        registry.register(ToolSpec("lookup", "Lookup", {"type": "object"}), observe)
        executor = ToolExecutor(
            registry,
            PolicyEngine(PermissionMode.DEFAULT),
            policy_input_factory=lambda req, spec: PolicyInput(
                req.name, PolicyOperation.READ, False, False
            ),
        )
        chunks = {
            "invalid_json": [
                start(),
                block_start(0, tool(input={})),
                delta(0, "input_json_delta", partial_json="[]"),
                block_stop(0),
                *ending("tool_use"),
            ],
            "max_tokens": sample([tool()], "max_tokens"),
            "pause_turn": sample([tool()], "pause_turn"),
            "missing_stop": sample([tool()], "tool_use")[:-1],
            "server_tool": sample(
                [
                    {
                        "type": "server_tool_use",
                        "id": "server-one",
                        "name": "lookup",
                        "input": {},
                    }
                ]
            ),
        }[mode]

        async def handler(_):
            return sse_response(chunks)

        provider = AnthropicMessagesProvider(
            "fake-test-key", transport=RecordingTransport(handler)
        )
        try:
            events = [
                e
                async for e in AgentEngine(provider, tool_executor=executor).run(
                    request(tools=registry.specs())
                )
            ]
        finally:
            await provider.aclose()
        assert isinstance(
            events[-1], RunCompleted if mode == "server_tool" else RunFailed
        )
        assert observed == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,classification,retry",
    [
        (401, AuthenticationError, False),
        (403, AuthenticationError, False),
        (429, RateLimitError, False),
        (400, ProviderError, False),
        (408, ProviderTimeoutError, True),
        (500, TemporaryServerError, True),
        (502, TemporaryServerError, True),
        (503, TemporaryServerError, True),
        (504, TemporaryServerError, True),
        (529, TemporaryServerError, True),
    ],
)
def test_http_classification_retry_limit_and_closed_before_backoff(
    status, classification, retry
):
    async def scenario():
        sleeps = []

        async def handler(_):
            return sse_response(status=status, content=b"raw-error-secret")

        transport = RecordingTransport(handler)

        async def sleep(delay):
            assert all(r.is_closed for r in transport.responses)
            sleeps.append(delay)

        provider = AnthropicMessagesProvider(
            "fake-test-key",
            transport=transport,
            max_retries=1,
            retry_backoff=0.25,
            sleep=sleep,
        )
        try:
            events = [e async for e in provider.stream(request())]
        finally:
            await provider.aclose()
        assert_error(events, classification)
        assert len(transport.requests) == (2 if retry else 1)
        assert sleeps == ([0.25] if retry else [])
        assert all(r.is_closed for r in transport.responses)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
@pytest.mark.parametrize("succeed", [True, False])
def test_connection_retry_is_finite(failure, succeed):
    async def scenario():
        sleeps = []

        async def handler(_):
            if len(transport.requests) == 1 or not succeed:
                raise failure("raw-error-secret")
            return sse_response(sample([text()]))

        async def sleep(delay):
            sleeps.append(delay)

        transport = RecordingTransport(handler)
        provider = AnthropicMessagesProvider(
            "fake-test-key", transport=transport, sleep=sleep
        )
        try:
            events = [e async for e in provider.stream(request())]
        finally:
            await provider.aclose()
        assert len(transport.requests) == 2 and sleeps == [1.0]
        if succeed:
            assert events == [
                TextDelta("answer"),
                Finish("end_turn", (), record((TextItem(None),))),
            ]
        else:
            assert_error(
                events,
                ProviderTimeoutError
                if failure is httpx.ConnectTimeout
                else TemporaryServerError,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ReadError])
@pytest.mark.parametrize("output", [False, True])
def test_established_stream_failure_is_malformed_without_retry(failure, output):
    async def scenario():
        class Body(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield (start() + (block_start(0, text()) if output else "")).encode()
                raise failure("raw-error-secret")

            async def aclose(self):
                self.closed = True

        body = Body()

        async def handler(_):
            return httpx.Response(200, stream=body)

        transport = RecordingTransport(handler)
        provider = AnthropicMessagesProvider(
            "fake-test-key", transport=transport, max_retries=3
        )
        try:
            events = [e async for e in provider.stream(request())]
        finally:
            await provider.aclose()
        assert_error(events)
        assert len(transport.requests) == 1
        assert [e for e in events if isinstance(e, TextDelta)] == (
            [TextDelta("answer")] if output else []
        )
        assert body.closed and transport.responses[0].is_closed

    asyncio.run(scenario())


def test_cancellation_propagates_and_closes_response_and_client():
    async def scenario():
        ready = asyncio.Event()
        closed = asyncio.Event()

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield start().encode()
                ready.set()
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        async def handler(_):
            return httpx.Response(200, stream=Body())

        transport = RecordingTransport(handler)
        provider = AnthropicMessagesProvider("fake-test-key", transport=transport)

        async def consume():
            return [e async for e in provider.stream(request())]

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set() and transport.responses[0].is_closed
        finally:
            task.cancel()
            await provider.aclose()
            await provider.aclose()
        assert transport.aclose_count == 1

    asyncio.run(scenario())


def test_provider_repr_never_contains_key_or_url():
    async def scenario():
        provider = AnthropicMessagesProvider(
            "fake-test-key", "https://secret.invalid/private/path"
        )
        try:
            assert repr(provider) == "AnthropicMessagesProvider(max_retries=1)"
        finally:
            await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("mapping", [None, {}])
def test_empty_constructor_mapping_uses_default(mapping):
    events, transport = run(
        sample(),
        request(reasoning_effort=ReasoningEffort.XHIGH),
        thinking_level_map=mapping,
    )
    assert isinstance(events[-1], Finish)
    assert json.loads(transport.requests[0].content)["output_config"] == {
        "effort": "xhigh"
    }


@pytest.mark.parametrize("slot", [None, record()])
def test_none_and_unreplayable_slots_do_not_shift_later_replay(slot):
    messages = (
        Message(MessageRole.USER, "hello"),
        Message(MessageRole.ASSISTANT, ""),
        Message(MessageRole.USER, "again"),
        Message(MessageRole.ASSISTANT, ""),
    )
    rec = record((MessagesThinkingItem("", "signature-one"),))
    _, transport = run(sample(), request(messages=messages, continuations=(slot, rec)))
    assert json.loads(transport.requests[0].content)["messages"][-1] == {
        "role": "assistant",
        "content": [thinking("")],
    }


def test_unsigned_replay_can_map_ids_and_results_have_no_inferred_status():
    messages = list(history(("bad/id",)))
    messages[2] = Message(
        MessageRole.TOOL, "ERROR real failed output", tool_call_id="bad/id"
    )
    _, transport = run(
        sample(),
        request(
            messages=tuple(messages),
            continuations=(record((ToolCallItem("bad/id"), TextItem(None))),),
        ),
    )
    encoded = json.loads(transport.requests[0].content)["messages"]
    assert encoded[1]["content"] == [tool("mica_tool_0", "0"), text()]
    assert encoded[2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "mica_tool_0",
        "content": "ERROR real failed output",
    }


@pytest.mark.parametrize("changes", [{"id": "other-id"}, {"name": "other_name"}])
def test_delta_tool_identity_conflicts_are_malformed(changes):
    events, _ = run(
        [
            start(),
            block_start(0, tool(input={})),
            delta(0, "input_json_delta", partial_json="{}", **changes),
            block_stop(0),
            *ending("tool_use"),
        ]
    )
    assert_error(events)


def test_unknown_tool_delta_cannot_claim_complete_arguments():
    events, _ = run(
        [
            start(),
            block_start(0, tool()),
            delta(0, "future_input_delta", partial_json="secret/query"),
            block_stop(0),
            *ending("tool_use"),
        ]
    )
    assert_error(events)


@pytest.mark.parametrize("kind", ["thinking", "text", "server_tool_use"])
def test_unknown_delta_degrades_supported_visible_or_server_blocks(kind):
    block = {
        "thinking": thinking(),
        "text": text(),
        "server_tool_use": {
            "type": "server_tool_use",
            "id": "server-one",
            "name": "lookup",
            "input": {},
        },
    }[kind]
    events, _ = run(
        [
            start(),
            block_start(0, block),
            delta(0, "future_delta", data="opaque-data"),
            block_stop(0),
            *ending(),
        ]
    )
    assert events[-1] == Finish("end_turn", (), record())


@pytest.mark.parametrize("input_value", [[], None, "secret/query", 2])
def test_nonobject_start_input_is_malformed(input_value):
    events, _ = run(sample([tool(input=input_value)], "tool_use"))
    assert_error(events)


def test_empty_tool_start_without_fragments_is_complete_object():
    events, _ = run(sample([tool(input={})], "tool_use"))
    assert events[-1] == Finish(
        "tool_use",
        (ToolCall("call-one", "lookup", {}),),
        record((ToolCallItem("call-one"),)),
    )


def test_message_delta_waits_for_message_stop_and_usage_only_delta_is_allowed():
    chunks = [
        start(),
        block_start(0, text()),
        block_stop(0),
        frame("message_delta", delta={}, usage={"output_tokens": 1}),
        *ending(),
    ]
    events, _ = run(chunks)
    assert events == [
        TextDelta("answer"),
        Finish("end_turn", (), record((TextItem(None),))),
    ]
    events, _ = run([start(), frame("message_delta", delta={}), frame("message_stop")])
    assert_error(events)


@pytest.mark.parametrize("offset", [0, 1])
def test_combined_private_reference_items_budget(offset):
    prefix = '中\\"'
    items = (
        MessagesRedactedThinkingItem(prefix),
        MessagesThinkingItem("", "s"),
        ToolCallItem("call-one"),
    )
    padding = "x" * (
        MAX_TURN_CONTINUATION_BYTES - continuation_size(record(items)) + offset
    )
    expected = record((MessagesRedactedThinkingItem(prefix + padding), *items[1:]))
    assert continuation_size(expected) == MAX_TURN_CONTINUATION_BYTES + offset
    events, _ = run(
        sample(
            [
                {"type": "redacted_thinking", "data": prefix + padding},
                thinking("", "s"),
                tool(),
            ],
            "tool_use",
        )
    )
    assert events[-1] == Finish(
        "tool_use",
        (ToolCall("call-one", "lookup", {"query": "x"}),),
        record() if offset else expected,
    )


@pytest.mark.parametrize("failure", ["signature", "disabled"])
def test_http_rejection_preserves_original_parameters_without_stripping_or_retry(
    failure,
):
    async def scenario():
        async def handler(_):
            return sse_response(status=400, content=b"raw-error-secret")

        transport = RecordingTransport(handler)
        provider = AnthropicMessagesProvider("fake-test-key", transport=transport)
        req = (
            request()
            if failure == "disabled"
            else request(
                messages=(
                    Message(MessageRole.USER, "hello"),
                    Message(MessageRole.ASSISTANT, ""),
                ),
                continuations=(
                    record((MessagesThinkingItem("thought", "signature-one"),)),
                ),
            )
        )
        try:
            events = [e async for e in provider.stream(req)]
        finally:
            await provider.aclose()
        assert_error(events, ProviderError)
        assert len(transport.requests) == 1
        payload = json.loads(transport.requests[0].content)
        assert payload["thinking"] == {"type": "disabled"}
        assert "prefix_mismatch_behavior" not in str(payload)
        if failure == "signature":
            assert payload["messages"][-1]["content"] == [thinking()]

    asyncio.run(scenario())
