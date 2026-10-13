"""Responses 的请求、流式事实、续接和资源边界；仅使用假凭据与隔离传输。"""

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
    ModelStepContinuation,
    ProviderApi,
    ResponsesReasoningItem,
    TextItem,
    TextPhase,
    ToolCallItem,
    continuation_size,
)
from mica_agent.engine import AgentEngine
from mica_agent.events import RunCompleted, RunFailed, ToolFinished
from mica_agent.models import Message, MessageRole
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine, PolicyInput, PolicyOperation
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
from mica_agent.providers.openai_responses import (
    OpenAIResponsesProvider,
    _ResponseStep,
    resolve_endpoint,
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
            provider_id="source",
            api=ProviderApi.OPENAI_RESPONSES,
        ),
        **changes,
    )


def frame(kind, **fields):
    return "data: " + json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n\n"


def completed(items, **changes):
    return frame(
        "response.completed",
        response={"status": "completed", "output": items, **changes},
    )


def text_item(text="answer", item_id="msg-one", **changes):
    return {
        "type": "message",
        "id": item_id,
        "status": "completed",
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": text, "annotations": [], "logprobs": []}
        ],
        **changes,
    }


def reasoning_item(summary="thought", encrypted="opaque-final", **changes):
    return {
        "type": "reasoning",
        "id": "rs-one",
        "summary": [{"type": "summary_text", "text": summary}],
        "encrypted_content": encrypted,
        **changes,
    }


def call_item(
    call_id="call-one", item_id="fc-one", arguments='{"query":"x"}', **changes
):
    return {
        "type": "function_call",
        "id": item_id,
        "call_id": call_id,
        "name": "lookup",
        "arguments": arguments,
        "status": "completed",
        **changes,
    }


def item_frame(item, index=0, *, done=False):
    return frame(
        "response.output_item.done" if done else "response.output_item.added",
        output_index=index,
        item=item,
    )


def delta_frame(kind, text, item_id="msg-one", index=0, part_index=0, *, done=False):
    index_name = (
        "summary_index" if kind == "reasoning_summary_text" else "content_index"
    )
    value_name = "refusal" if kind == "refusal" else "text"
    return frame(
        f"response.{kind}.{'done' if done else 'delta'}",
        output_index=index,
        item_id=item_id,
        **{index_name: part_index, value_name if done else "delta": text},
    )


async def collect(chunks, req=None, **options):
    async def handler(_):
        return sse_response(chunks)

    transport = RecordingTransport(handler)
    provider = OpenAIResponsesProvider(
        "fake-test-key",
        "https://example.invalid/v1",
        transport=transport,
        max_retries=0,
        **options,
    )
    try:
        events = [event async for event in provider.stream(req or request())]
    finally:
        await provider.aclose()
        await provider.aclose()
    assert all(response.is_closed for response in transport.responses)
    assert transport.aclose_count == 1
    return events, [json.loads(sent.content) for sent in transport.requests]


def run(chunks, req=None, **options):
    return asyncio.run(collect(chunks, req, **options))


def assert_error(events, kind=MalformedResponseError):
    assert isinstance(events[-1], ProviderErrorEvent)
    assert type(events[-1].error) is kind
    assert sum(isinstance(event, ProviderErrorEvent) for event in events) == 1
    assert not any(isinstance(event, Finish) for event in events)
    diagnostic = str(events[-1].error) + repr(events[-1].error)
    for secret in (
        "fake-test-key",
        "raw-error-secret",
        "private/thought",
        "opaque-final",
        "C:/private",
    ):
        assert secret not in diagnostic


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://example.invalid/v1", "https://example.invalid/v1/responses"),
        ("https://example.invalid/v1/", "https://example.invalid/v1/responses"),
        (
            "https://example.invalid/v1/responses/",
            "https://example.invalid/v1/responses",
        ),
        (
            "https://example.invalid/responses/?version=x",
            "https://example.invalid/responses?version=x",
        ),
        (
            "https://example.invalid/v1?version=x",
            "https://example.invalid/v1/responses?version=x",
        ),
    ],
)
def test_endpoint(base, expected):
    assert resolve_endpoint(base) == expected


@pytest.mark.parametrize(
    "base",
    [
        "relative/path",
        "ftp://example.invalid",
        "https://example.invalid:raw-error-secret",
        None,
    ],
)
def test_invalid_endpoint_has_fixed_diagnostic(base):
    with pytest.raises(ValueError, match="^base_url must be a valid HTTP URL$"):
        resolve_endpoint(base)


@pytest.mark.parametrize("budget", [None, 71])
def test_literal_request_and_tool_schema(budget):
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "optional": {"type": "string"}},
        "required": ["query"],
    }
    req = request(
        messages=(
            Message(MessageRole.SYSTEM, "be brief"),
            Message(MessageRole.USER, "hello"),
            Message(MessageRole.ASSISTANT, "hi"),
        ),
        tools=(ToolSpec("lookup", "Lookup", schema),),
        max_tokens=budget,
        continuations=(),
    )
    events, bodies = run([completed([text_item()])], req)
    assert isinstance(events[-1], Finish)
    assert bodies == [
        {
            "model": "test-model",
            "input": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": budget or 4096,
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Lookup",
                    "parameters": schema,
                    "strict": False,
                }
            ],
        }
    ]


@pytest.mark.parametrize("effort", list(ReasoningEffort))
@pytest.mark.parametrize("mapping", [None, {}])
def test_default_reasoning_mapping(effort, mapping):
    _, bodies = run(
        [completed([])],
        request(reasoning_effort=effort, thinking_level_map=mapping),
        thinking_level_map=mapping,
    )
    if effort is ReasoningEffort.OFF:
        assert "reasoning" not in bodies[0]
    else:
        assert bodies[0]["reasoning"] == {"effort": effort.value, "summary": "auto"}
    assert "context" not in bodies[0].get("reasoning", {})


@pytest.mark.parametrize(
    "request_map,provider_map,target",
    [
        (None, {ReasoningEffort.HIGH: "medium"}, "medium"),
        ({ReasoningEffort.HIGH: "low"}, {ReasoningEffort.HIGH: "medium"}, "low"),
        ({}, {ReasoningEffort.HIGH: "low"}, "high"),
    ],
)
def test_explicit_reasoning_mapping(request_map, provider_map, target):
    _, bodies = run(
        [completed([])],
        request(reasoning_effort=ReasoningEffort.HIGH, thinking_level_map=request_map),
        thinking_level_map=provider_map,
    )
    assert bodies[0]["reasoning"] == {"effort": target, "summary": "auto"}


@pytest.mark.parametrize(
    "req",
    [
        request(api=ProviderApi.OPENAI_COMPLETIONS),
        request(api=ProviderApi.ANTHROPIC_MESSAGES),
        request(
            reasoning_effort=ReasoningEffort.MAX,
            thinking_level_map={ReasoningEffort.LOW: "low"},
        ),
    ],
)
def test_invalid_interface_or_effort_never_sends_request(req):
    events, bodies = run([], req)
    assert_error(events, ProviderError)
    assert bodies == []


def test_headers_and_repr_hide_credentials_and_sensitive_url():
    async def scenario():
        async def handler(sent):
            assert sent.headers["Authorization"] == "Bearer fake-test-key"
            assert "fake-test-key" not in sent.content.decode()
            assert sent.url.path == "/v1/responses"
            return sse_response([completed([])])

        transport = RecordingTransport(handler)
        provider = OpenAIResponsesProvider(
            "fake-test-key",
            "https://user:password@example.invalid/v1?token=url-secret",
            transport=transport,
        )
        assert all(
            secret not in repr(provider)
            for secret in ("fake-test-key", "password", "url-secret", "example.invalid")
        )
        try:
            assert isinstance([e async for e in provider.stream(request())][-1], Finish)
        finally:
            await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "items,expected_text,expected_thought,expected_items,expected_calls",
    [
        (
            [reasoning_item()],
            "",
            "thought",
            (ResponsesReasoningItem("rs-one", ("thought",), "opaque-final"),),
            (),
        ),
        ([text_item("")], "", "", None, ()),
        ([], "", "", None, ()),
        (
            [text_item("answer", phase="final_answer")],
            "answer",
            "",
            (TextItem(TextPhase.FINAL_ANSWER),),
            (),
        ),
        (
            [reasoning_item(), text_item()],
            "answer",
            "thought",
            (
                ResponsesReasoningItem("rs-one", ("thought",), "opaque-final"),
                TextItem(None),
            ),
            (),
        ),
        (
            [call_item()],
            "",
            "",
            (ToolCallItem("call-one"),),
            (ToolCall("call-one", "lookup", {"query": "x"}),),
        ),
    ],
)
@pytest.mark.parametrize("provider_id", ["source", None])
def test_complete_snapshot_only_and_empty_steps(
    items, expected_text, expected_thought, expected_items, expected_calls, provider_id
):
    events, _ = run(
        [completed(items), frame("error", code="server_error")],
        request(provider_id=provider_id),
    )
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == expected_text
    assert (
        "".join(e.text for e in events if isinstance(e, ProviderReasoningDelta))
        == expected_thought
    )
    assert sum(isinstance(e, Finish) for e in events) == 1
    continuation = (
        record(expected_items)
        if expected_items is not None and provider_id is not None
        else None
    )
    assert events[-1] == Finish("completed", expected_calls, continuation)


@pytest.mark.parametrize(
    "chunks",
    [
        [
            frame(
                "response.completed",
                response={"status": "completed", "output": []},
                new_semantic_field=True,
            )
        ],
        [completed([text_item("", "msg-one"), text_item("", "msg-two")])],
        [completed([reasoning_item(id="invalid id")])],
    ],
)
def test_empty_items_after_real_degradation_keep_unreplayable_marker(chunks):
    events, _ = run(chunks)
    assert isinstance(events[-1], Finish)
    assert events[-1].continuation == ModelStepContinuation(
        "source",
        "test-model",
        ProviderApi.OPENAI_RESPONSES,
        ContinuationStatus.UNREPLAYABLE,
    )


def test_deltas_done_and_snapshots_emit_each_suffix_once_and_final_cipher():
    reasoning = reasoning_item("thought")
    text = text_item("answer", phase="commentary")
    chunks = [
        item_frame(reasoning_item("", "incomplete-added")),
        frame(
            "response.reasoning_summary_part.added",
            output_index=0,
            item_id="rs-one",
            summary_index=0,
            part={"type": "summary_text", "text": ""},
        ),
        delta_frame("reasoning_summary_text", "tho", "rs-one"),
        delta_frame("reasoning_summary_text", "thought", "rs-one", done=True),
        frame(
            "response.reasoning_summary_part.done",
            output_index=0,
            item_id="rs-one",
            summary_index=0,
            part={"type": "summary_text", "text": "thought"},
        ),
        item_frame(reasoning, done=True),
        item_frame(
            text_item("", phase="commentary", status="in_progress", content=[]), 1
        ),
        frame(
            "response.content_part.added",
            output_index=1,
            item_id="msg-one",
            content_index=0,
            part={"type": "output_text", "text": "", "annotations": []},
        ),
        delta_frame("output_text", "ans", index=1),
        delta_frame("output_text", "answer", index=1, done=True),
        frame(
            "response.content_part.done",
            output_index=1,
            item_id="msg-one",
            content_index=0,
            part=text["content"][0],
        ),
        item_frame(text, 1, done=True),
        completed([reasoning, text]),
    ]
    events, _ = run(chunks)
    assert [e.text for e in events if isinstance(e, ProviderReasoningDelta)] == [
        "tho",
        "ught",
    ]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["ans", "wer"]
    assert events[-1].continuation.items == (
        ResponsesReasoningItem("rs-one", ("thought",), "opaque-final"),
        TextItem(TextPhase.COMMENTARY),
    )


def test_known_reasoning_text_stays_visible_without_entering_replay_fields():
    raw = reasoning_item(
        "", content=[{"type": "reasoning_text", "text": "private/thought"}]
    )
    events, _ = run(
        [
            item_frame(reasoning_item("", content=[])),
            delta_frame("reasoning_text", "private/", "rs-one"),
            delta_frame("reasoning_text", "private/thought", "rs-one", done=True),
            completed([raw, text_item("ordinary")]),
        ]
    )
    assert [e.text for e in events if isinstance(e, ProviderReasoningDelta)] == [
        "private/",
        "thought",
    ]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["ordinary"]
    assert events[-1].continuation == record(
        (ResponsesReasoningItem("rs-one", ("",), "opaque-final"), TextItem(None))
    )


@pytest.mark.parametrize("cipher_source", ["done", "completed"])
def test_cipher_can_come_from_either_final_item_source(cipher_source):
    full = reasoning_item()
    without_cipher = {
        key: value for key, value in full.items() if key != "encrypted_content"
    }
    chunks = [
        item_frame(full if cipher_source == "done" else without_cipher, done=True),
        completed([without_cipher if cipher_source == "done" else full]),
    ]
    events, _ = run(chunks)
    assert [e.text for e in events if isinstance(e, ProviderReasoningDelta)] == [
        "thought"
    ]
    assert events[-1].continuation == record(
        (ResponsesReasoningItem("rs-one", ("thought",), "opaque-final"),)
    )


def record(items, **changes):
    return ModelStepContinuation(
        "source",
        "test-model",
        ProviderApi.OPENAI_RESPONSES,
        ContinuationStatus.REPLAYABLE,
        items,
        **changes,
    )


@pytest.mark.parametrize(
    "mismatch",
    ["provider", "model", "api", "unknown", "none", "unreplayable", "compatible"],
)
def test_history_binding_and_order(mismatch):
    call = ToolCall("call-one", "lookup", {"query": "x"})
    rec = record(
        (
            ResponsesReasoningItem("rs-one", ("thought",), "opaque-final"),
            ToolCallItem("call-one"),
            TextItem(TextPhase.COMMENTARY),
        )
    )
    if mismatch == "provider":
        rec = replace(rec, provider_id="different")
    elif mismatch == "model":
        rec = replace(rec, model_id="different")
    elif mismatch == "api":
        rec = ModelStepContinuation(
            "source",
            "test-model",
            ProviderApi.ANTHROPIC_MESSAGES,
            ContinuationStatus.REPLAYABLE,
            (ToolCallItem("call-one"), TextItem(None)),
        )
    elif mismatch == "none":
        rec = None
    elif mismatch == "unreplayable":
        rec = replace(rec, status=ContinuationStatus.UNREPLAYABLE, items=())
    req = request(
        messages=(
            Message(MessageRole.SYSTEM, "system"),
            Message(MessageRole.ASSISTANT, "body", (call,)),
            Message(MessageRole.TOOL, "real result", tool_call_id="call-one"),
        ),
        continuations=(rec,),
        provider_id=None if mismatch == "unknown" else "source",
    )
    events, bodies = run([completed([text_item()])], req)
    generic = [
        {"role": "assistant", "content": "body"},
        {
            "type": "function_call",
            "call_id": "call-one",
            "name": "lookup",
            "arguments": '{"query":"x"}',
        },
    ]
    private = [
        {
            "type": "reasoning",
            "id": "rs-one",
            "summary": [{"type": "summary_text", "text": "thought"}],
            "encrypted_content": "opaque-final",
        },
        generic[1],
        {"role": "assistant", "content": "body", "phase": "commentary"},
    ]
    assert bodies[0]["input"] == [
        {"role": "system", "content": "system"},
        *(private if mismatch == "compatible" else generic),
        {
            "type": "function_call_output",
            "call_id": "call-one",
            "output": "real result",
        },
    ]
    if mismatch == "unknown":
        assert events[-1].continuation is None


def test_slots_consumed_even_when_incompatible_and_empty_assistant_preserved():
    old = replace(record((TextItem(None),)), model_id="old-model")
    current = record((ResponsesReasoningItem("rs-one", (), "opaque-final"),))
    _, bodies = run(
        [completed([])],
        request(
            messages=(
                Message(MessageRole.ASSISTANT, "old"),
                Message(MessageRole.ASSISTANT, ""),
                Message(MessageRole.ASSISTANT, ""),
            ),
            continuations=(old, None, current),
        ),
    )
    assert bodies[0]["input"] == [
        {"role": "assistant", "content": "old"},
        {"role": "assistant", "content": ""},
        {
            "type": "reasoning",
            "id": "rs-one",
            "summary": [],
            "encrypted_content": "opaque-final",
        },
    ]


@pytest.mark.parametrize(
    "mode", ["orphan", "before", "duplicate_result", "duplicate_call"]
)
def test_invalid_historical_tool_associations_are_not_erased(mode):
    assistant = Message(
        MessageRole.ASSISTANT, "", (ToolCall("call-one", "lookup", {}),)
    )
    result = Message(MessageRole.TOOL, "real result", tool_call_id="call-one")
    messages = {
        "orphan": (result,),
        "before": (result, assistant),
        "duplicate_result": (assistant, result, result),
        "duplicate_call": (assistant, assistant),
    }[mode]
    events, bodies = run([], request(messages=messages, continuations=()))
    assert_error(events, ProviderError)
    assert not bodies


def test_unfinished_historical_call_has_no_fabricated_result():
    _, bodies = run(
        [completed([])],
        request(
            messages=(
                Message(
                    MessageRole.ASSISTANT, "", (ToolCall("call-one", "lookup", {}),)
                ),
            ),
            continuations=(),
        ),
    )
    assert bodies[0]["input"] == [
        {
            "type": "function_call",
            "call_id": "call-one",
            "name": "lookup",
            "arguments": "{}",
        }
    ]


@pytest.mark.parametrize(
    "items",
    [
        [reasoning_item(encrypted=None), text_item()],
        [reasoning_item(encrypted=""), text_item()],
        [reasoning_item(encrypted="invalid\nopaque"), text_item()],
        [reasoning_item(extra_semantic="new"), text_item()],
        [reasoning_item(content=[{"type": "unknown_content"}]), text_item()],
        [text_item(phase="unknown")],
        [text_item(phase="")],
        [text_item(extra_semantic="new")],
        [
            text_item(
                content=[
                    {"type": "output_text", "text": "answer", "new_semantic": True}
                ]
            )
        ],
        [
            text_item(
                content=[
                    {
                        "type": "output_text",
                        "text": "answer",
                        "annotations": [
                            {"type": "url_citation", "url": "https://example.invalid"}
                        ],
                    }
                ]
            )
        ],
        [text_item("an", "msg-one"), text_item("swer", "msg-two")],
        [
            text_item(
                content=[
                    {"type": "output_text", "text": "an"},
                    {"type": "output_text", "text": "swer"},
                ]
            )
        ],
        [
            {
                "type": "web_search_call",
                "id": "web-one",
                "status": "completed",
                "action": {"type": "search"},
            },
            text_item(),
        ],
        [text_item(content=[{"type": "refusal", "refusal": "answer"}])],
    ],
)
def test_unrepresentable_output_keeps_body_and_real_calls(items):
    events, _ = run([completed([*items, call_item()])])
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "answer"
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.tool_calls == (ToolCall("call-one", "lookup", {"query": "x"}),)
    assert finish.continuation.status is ContinuationStatus.UNREPLAYABLE
    assert finish.continuation.items == ()


def test_refusal_deltas_are_body_without_duplicate_or_synthetic_output():
    raw = text_item(content=[{"type": "refusal", "refusal": "refused"}])
    events, _ = run(
        [
            delta_frame("refusal", "refu"),
            delta_frame("refusal", "refused", done=True),
            completed([raw]),
        ]
    )
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["refu", "sed"]
    assert events[-1].continuation.items == ()
    assert events[-1].tool_calls == ()


@pytest.mark.parametrize(
    "bad",
    [
        "not-json",
        "[]",
        "null",
        '"x"',
        '{"q":NaN}',
        '{"q":1,"q":2}',
        '{"q":1e400}',
        '{"q":"\\ud800"}',
        '{"q":' + "[" * 600 + "0" + "]" * 600 + "}",
    ],
)
def test_invalid_tool_arguments_never_commit(bad):
    events, _ = run([completed([call_item(arguments=bad)])])
    assert_error(events)


def test_arguments_done_without_deltas_uses_complete_value():
    events, _ = run(
        [
            item_frame(call_item(arguments="", status="in_progress")),
            frame(
                "response.function_call_arguments.done",
                output_index=0,
                item_id="fc-one",
                arguments='{"query":"x"}',
            ),
            completed([call_item()]),
        ]
    )
    assert events[-1].tool_calls == (ToolCall("call-one", "lookup", {"query": "x"}),)
    assert events[-1].continuation == record((ToolCallItem("call-one"),))


def test_unknown_semantic_delta_field_downgrades_without_losing_body_or_call():
    events, _ = run(
        [
            frame(
                "response.reasoning_summary_text.delta",
                output_index=0,
                item_id="rs-one",
                summary_index=0,
                delta="thought",
                new_semantic_field="opaque-final",
            ),
            completed([reasoning_item(), text_item(), call_item()]),
        ]
    )
    assert [e.text for e in events if isinstance(e, ProviderReasoningDelta)] == [
        "thought"
    ]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["answer"]
    assert events[-1].continuation.items == ()
    assert events[-1].tool_calls == (ToolCall("call-one", "lookup", {"query": "x"}),)


def test_arguments_event_cannot_change_call_metadata():
    events, _ = run(
        [
            item_frame(call_item(arguments="", status="in_progress")),
            frame(
                "response.function_call_arguments.done",
                output_index=0,
                item_id="fc-one",
                arguments='{"query":"x"}',
                name="changed",
            ),
            completed([call_item()]),
        ]
    )
    assert_error(events)


@pytest.mark.parametrize(
    "chunks",
    [
        [
            item_frame(call_item(arguments="", status="in_progress")),
            frame(
                "response.function_call_arguments.delta",
                output_index=0,
                item_id="fc-one",
                delta="{}",
            ),
            frame(
                "response.function_call_arguments.done",
                output_index=0,
                item_id="fc-one",
                arguments='{"q":1}',
            ),
        ],
        [item_frame(call_item()), item_frame(call_item(name="other"), done=True)],
        [item_frame(call_item()), item_frame(call_item(call_id="other"), done=True)],
        [item_frame(call_item()), item_frame(call_item(item_id="fc-two"), 1)],
        [item_frame(text_item()), delta_frame("output_text", "x", item_id="wrong")],
        [item_frame(text_item()), item_frame(text_item(), 1)],
        [delta_frame("output_text", "a"), delta_frame("output_text", "b", done=True)],
        [delta_frame("output_text", "a"), completed([text_item("b")])],
        [item_frame(text_item("a"), done=True), completed([text_item("b")])],
        [item_frame(call_item(), done=True), completed([call_item(arguments="{}")])],
        [item_frame(text_item(), 1), completed([text_item()])],
        [delta_frame("output_text", "a", part_index=1), completed([text_item("a")])],
        [
            item_frame(reasoning_item(), done=True),
            completed([reasoning_item(encrypted="different")]),
        ],
        [
            delta_frame("reasoning_summary_text", "a", "rs-one"),
            completed([reasoning_item("b")]),
        ],
        [frame("response.completed", response={"status": "incomplete", "output": []})],
        [
            frame("response.created", response={"id": "resp-one"}),
            completed([], id="resp-two"),
        ],
        [
            frame(
                "response.output_text.delta",
                output_index=True,
                item_id="msg-one",
                content_index=0,
                delta="a",
            )
        ],
    ],
)
def test_association_and_snapshot_conflicts_are_safe(chunks):
    events, _ = run([*chunks, completed([])])
    assert_error(events)


@pytest.mark.parametrize(
    "terminal,expected",
    [
        (item_frame(text_item(), done=True), MalformedResponseError),
        (
            frame(
                "response.failed",
                response={
                    "error": {
                        "code": "unknown",
                        "message": "raw-error-secret C:/private",
                    }
                },
            ),
            ProviderError,
        ),
        (
            frame(
                "response.incomplete",
                response={"incomplete_details": {"reason": "raw-error-secret"}},
            ),
            ProviderError,
        ),
        (
            frame(
                "error", code="server_error", message="raw-error-secret private/thought"
            ),
            TemporaryServerError,
        ),
        ("data: [DONE]\n\n", MalformedResponseError),
        ("", MalformedResponseError),
        ("data: not-json\n\n", MalformedResponseError),
        ("data: []\n\n", MalformedResponseError),
        (
            'event: wrong\ndata: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n',
            MalformedResponseError,
        ),
        (
            'data: {"type":"response.completed","response":{"status":"completed","output":[]}}\n',
            MalformedResponseError,
        ),
    ],
)
def test_only_completed_is_a_normal_terminal(terminal, expected):
    events, _ = run([terminal])
    assert_error(events, expected)


def test_multiline_sse_comments_and_event_type_matching():
    terminal = ': keepalive\n\nevent: response.completed\nid: sequence\ndata: {"type":"response.completed",\ndata: "response":{"status":"completed","output":[]}}\n\n'
    events, _ = run([terminal, "data: invalid\n\n"])
    assert events == [Finish("completed", continuation=None)]


@pytest.mark.parametrize(
    "status,error_type",
    [
        (401, AuthenticationError),
        (403, AuthenticationError),
        (429, RateLimitError),
        (408, ProviderTimeoutError),
        (500, TemporaryServerError),
        (502, TemporaryServerError),
        (503, TemporaryServerError),
        (504, TemporaryServerError),
        (418, ProviderError),
        (302, ProviderError),
    ],
)
def test_http_errors_are_classified_without_body(status, error_type):
    async def scenario():
        async def handler(_):
            return sse_response(
                status=status, content=b"raw-error-secret C:/private fake-test-key"
            )

        transport = RecordingTransport(handler)
        provider = OpenAIResponsesProvider(
            "fake-test-key", transport=transport, max_retries=0
        )
        try:
            events = [e async for e in provider.stream(request())]
        finally:
            await provider.aclose()
        assert transport.responses[0].is_closed
        assert_error(events, error_type)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code,error_type",
    [
        ("invalid_api_key", AuthenticationError),
        ("permission_denied", AuthenticationError),
        ("rate_limit_exceeded", RateLimitError),
        ("insufficient_quota", RateLimitError),
        ("request_timeout", ProviderTimeoutError),
        ("server_error", TemporaryServerError),
        ("unknown", ProviderError),
    ],
)
@pytest.mark.parametrize("failed", [False, True])
def test_stream_errors_are_not_retried_or_replaced_by_second_terminal(
    code, error_type, failed
):
    fields = {"code": code, "message": "raw-error-secret C:/private"}
    terminal = (
        frame("response.failed", response={"error": fields})
        if failed
        else frame("error", **fields)
    )
    events, bodies = run(
        [
            delta_frame("output_text", "partial"),
            terminal,
            completed([text_item("partial")]),
        ]
    )
    assert_error(events, error_type)
    assert events[0] == TextDelta("partial")
    assert len(bodies) == 1


@pytest.mark.parametrize("suffix", ["", "界", '\\"'])
@pytest.mark.parametrize("extra", [0, 1])
def test_continuation_budget_uses_exact_compact_utf8_wire(suffix, extra):
    base = record((ResponsesReasoningItem("rs-one", ("thought",), "x" + suffix),))
    encrypted = (
        "x" * (MAX_TURN_CONTINUATION_BYTES - continuation_size(base) + 1 + extra)
        + suffix
    )
    candidate = record((ResponsesReasoningItem("rs-one", ("thought",), encrypted),))
    assert continuation_size(candidate) == MAX_TURN_CONTINUATION_BYTES + extra
    events, _ = run([completed([reasoning_item(encrypted=encrypted)])])
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.continuation == (
        replace(candidate, status=ContinuationStatus.UNREPLAYABLE, items=())
        if extra
        else candidate
    )
    assert all(
        encrypted not in e.text
        for e in events
        if isinstance(e, (TextDelta, ProviderReasoningDelta))
    )


def test_overflow_during_summary_is_sticky_and_releases_private_accumulation():
    large = "x" * MAX_TURN_CONTINUATION_BYTES
    # 大摘要触发丢弃；最终摘要缩小仍是协议矛盾，不能悄悄恢复。
    events, _ = run(
        [
            delta_frame("reasoning_summary_text", large, "rs-one"),
            completed([reasoning_item("small")]),
        ]
    )
    assert_error(events)
    events, _ = run(
        [
            delta_frame("reasoning_summary_text", large, "rs-one"),
            completed([reasoning_item(large), text_item()]),
        ]
    )
    assert isinstance(events[-1], Finish)
    assert events[-1].continuation.items == ()
    assert [e.text for e in events if isinstance(e, ProviderReasoningDelta)] == [large]


def test_overflow_cipher_done_cannot_be_restored_by_completed():
    huge = reasoning_item(encrypted="x" * MAX_TURN_CONTINUATION_BYTES)
    events, _ = run(
        [item_frame(huge, done=True), completed([huge, text_item(), call_item()])]
    )
    assert isinstance(events[-1], Finish)
    assert events[-1].continuation.items == ()
    assert events[-1].tool_calls == (ToolCall("call-one", "lookup", {"query": "x"}),)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["answer"]


@pytest.mark.parametrize("failure", ["timeout", "connect", "http"])
@pytest.mark.parametrize("succeed", [False, True])
def test_connection_retry_is_bounded_and_closes_http_before_backoff(failure, succeed):
    async def scenario():
        sleeps = []

        async def handler(sent):
            if succeed and len(transport.requests) == 2:
                return sse_response([completed([])])
            if failure == "timeout":
                raise httpx.ConnectTimeout("raw-error-secret", request=sent)
            if failure == "connect":
                raise httpx.ConnectError("raw-error-secret", request=sent)
            return sse_response(status=503)

        async def sleep(delay):
            assert all(r.is_closed for r in transport.responses)
            sleeps.append(delay)

        transport = RecordingTransport(handler)
        provider = OpenAIResponsesProvider(
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
        assert len(transport.requests) == 2
        assert sleeps == [0.25]
        if succeed:
            assert isinstance(events[-1], Finish)
        else:
            assert_error(
                events,
                ProviderTimeoutError if failure == "timeout" else TemporaryServerError,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("progress", ["none", "text", "reasoning", "tool", "private"])
@pytest.mark.parametrize("failure", [httpx.ReadError, httpx.ReadTimeout])
def test_established_stream_failure_never_retries(progress, failure):
    async def scenario():
        class BrokenStream(httpx.AsyncByteStream):
            def __init__(self):
                self.closed = False

            async def __aiter__(self):
                chunks = {
                    "none": ": heartbeat\n\n",
                    "text": delta_frame("output_text", "partial"),
                    "reasoning": delta_frame(
                        "reasoning_summary_text", "thought", "rs-one"
                    ),
                    "tool": item_frame(call_item(arguments="", status="in_progress")),
                    "private": item_frame(reasoning_item("", "added-opaque")),
                }
                yield chunks[progress].encode()
                raise failure("raw-error-secret")

            async def aclose(self):
                self.closed = True

        stream = BrokenStream()

        async def handler(_):
            return httpx.Response(200, stream=stream)

        transport = RecordingTransport(handler)
        provider = OpenAIResponsesProvider(
            "fake-test-key", transport=transport, max_retries=3
        )
        try:
            events = [e async for e in provider.stream(request())]
        finally:
            await provider.aclose()
        assert_error(events)
        assert len(transport.requests) == 1
        assert stream.closed and transport.responses[0].is_closed

    asyncio.run(scenario())


def test_cancellation_propagates_and_closes_response_and_client():
    async def scenario():
        entered = asyncio.Event()
        closed = asyncio.Event()

        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield delta_frame("output_text", "partial").encode()
                entered.set()
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        async def handler(_):
            return httpx.Response(200, stream=WaitingStream())

        transport = RecordingTransport(handler)
        provider = OpenAIResponsesProvider("fake-test-key", transport=transport)
        events = []

        async def consume():
            async for event in provider.stream(request()):
                events.append(event)

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()
            assert events == [TextDelta("partial")]
        finally:
            await provider.aclose()
            await provider.aclose()
        assert transport.aclose_count == 1

    asyncio.run(scenario())


def tool_roundtrip_sample():
    reasoning = reasoning_item()
    first = call_item()
    second = call_item("call-two", "fc-two", '{"query":"y"}')
    text = text_item("checking", phase="commentary")
    return [
        item_frame(reasoning_item("", "added-incomplete")),
        delta_frame("reasoning_summary_text", "thought", "rs-one"),
        item_frame(reasoning, done=True),
        item_frame(call_item(arguments="", status="in_progress"), 1),
        frame(
            "response.function_call_arguments.delta",
            output_index=1,
            item_id="fc-one",
            delta='{"query":',
        ),
        item_frame(
            text_item("", phase="commentary", content=[], status="in_progress"), 2
        ),
        delta_frame("output_text", "checking", index=2),
        item_frame(call_item("call-two", "fc-two", "", status="in_progress"), 3),
        frame(
            "response.function_call_arguments.delta",
            output_index=3,
            item_id="fc-two",
            delta='{"query":"',
        ),
        frame(
            "response.function_call_arguments.delta",
            output_index=1,
            item_id="fc-one",
            delta='"x"}',
        ),
        frame(
            "response.function_call_arguments.delta",
            output_index=3,
            item_id="fc-two",
            delta='y"}',
        ),
        frame(
            "response.function_call_arguments.done",
            output_index=1,
            item_id="fc-one",
            arguments='{"query":"x"}',
        ),
        item_frame(first, 1, done=True),
        item_frame(text, 2, done=True),
        item_frame(second, 3, done=True),
        completed([reasoning, first, text, second]),
    ]


def executor_for(observed):
    registry = ToolRegistry()
    spec = ToolSpec(
        "lookup",
        "Lookup",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )

    async def lookup(req, cancellation):
        cancellation.raise_if_cancelled()
        observed.append((req.call_id, dict(req.arguments)))
        return ToolResult(
            req.call_id,
            req.name,
            ToolResultStatus.SUCCESS,
            "found " + req.arguments["query"],
        )

    registry.register(spec, lookup)
    return registry, ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda req, spec: PolicyInput(
            req.name, PolicyOperation.READ, False, False
        ),
    )


def test_two_step_engine_roundtrip_compares_finish_and_next_literal_input():
    expected_record = record(
        (
            ResponsesReasoningItem("rs-one", ("thought",), "opaque-final"),
            ToolCallItem("call-one"),
            TextItem(TextPhase.COMMENTARY),
            ToolCallItem("call-two"),
        )
    )
    first_events, _ = run(tool_roundtrip_sample())
    assert first_events[-1] == Finish(
        "completed",
        (
            ToolCall("call-one", "lookup", {"query": "x"}),
            ToolCall("call-two", "lookup", {"query": "y"}),
        ),
        expected_record,
    )

    async def scenario():
        observed, received_records, received_messages = [], [], []
        registry, executor = executor_for(observed)

        async def handler(_):
            return sse_response(
                tool_roundtrip_sample()
                if len(transport.requests) == 1
                else [completed([text_item("finished", phase="final_answer")])]
            )

        transport = RecordingTransport(handler)
        provider = OpenAIResponsesProvider(
            "fake-test-key", transport=transport, max_retries=0
        )
        try:
            events = [
                e
                async for e in AgentEngine(provider, tool_executor=executor).run(
                    request(
                        messages=(
                            Message(MessageRole.SYSTEM, "system"),
                            Message(MessageRole.USER, "hello"),
                        ),
                        tools=registry.specs(),
                    ),
                    completed_continuations_sink=received_records.append,
                    completed_messages_sink=received_messages.append,
                )
            ]
        finally:
            await provider.aclose()
        assert isinstance(events[-1], RunCompleted)
        assert observed == [("call-one", {"query": "x"}), ("call-two", {"query": "y"})]
        assert [e.call_id for e in events if isinstance(e, ToolFinished)] == [
            "call-one",
            "call-two",
        ]
        assert received_records == [
            (expected_record, record((TextItem(TextPhase.FINAL_ANSWER),)))
        ]
        bodies = [json.loads(r.content) for r in transport.requests]
        assert len(bodies) == 2
        assert bodies[0]["input"] == [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ]
        assert bodies[1] == {
            **bodies[0],
            "input": [
                *bodies[0]["input"],
                {
                    "type": "reasoning",
                    "id": "rs-one",
                    "summary": [{"type": "summary_text", "text": "thought"}],
                    "encrypted_content": "opaque-final",
                },
                {
                    "type": "function_call",
                    "call_id": "call-one",
                    "name": "lookup",
                    "arguments": '{"query":"x"}',
                },
                {"role": "assistant", "content": "checking", "phase": "commentary"},
                {
                    "type": "function_call",
                    "call_id": "call-two",
                    "name": "lookup",
                    "arguments": '{"query":"y"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-one",
                    "output": '{"status":"success","message":"found x","data":null}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-two",
                    "output": '{"status":"success","message":"found y","data":null}',
                },
            ],
        }
        assert received_messages[0][0] == Message(
            MessageRole.ASSISTANT, "checking", first_events[-1].tool_calls
        )
        assert all(r.is_closed for r in transport.responses)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["malformed", "failed", "unknown_server_tool"])
def test_engine_never_executes_incomplete_or_hosted_tool_output(mode):
    async def scenario():
        observed = []
        registry, executor = executor_for(observed)
        samples = {
            "malformed": [completed([call_item(arguments="[]")])],
            "failed": [
                item_frame(call_item(), done=True),
                frame(
                    "response.failed",
                    response={
                        "error": {"code": "server_error", "message": "raw-error-secret"}
                    },
                ),
            ],
            "unknown_server_tool": [
                completed(
                    [
                        {
                            "type": "web_search_call",
                            "id": "web-one",
                            "status": "completed",
                        },
                        text_item(),
                    ]
                )
            ],
        }

        async def handler(_):
            return sse_response(samples[mode])

        provider = OpenAIResponsesProvider(
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
            events[-1], RunCompleted if mode == "unknown_server_tool" else RunFailed
        )
        assert not observed

    asyncio.run(scenario())


def test_private_accumulator_releases_overflow_and_never_reloads_snapshot():
    # 内存释放没有公共事件可观测，只在此检查适配器自身的累积器。
    step = _ResponseStep(request())
    large = "x" * MAX_TURN_CONTINUATION_BYTES
    step.consume(
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "item_id": "rs-one",
            "summary_index": 0,
            "delta": large,
        }
    )
    assert step.discarded
    assert step.outputs[0].parts[("summary_text", 0)].saved is None
    step.consume(
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "item_id": "rs-one",
            "summary_index": 0,
            "delta": "suffix",
        }
    )
    raw = reasoning_item(large + "suffix", "x" * MAX_TURN_CONTINUATION_BYTES)
    step.consume({"type": "response.output_item.done", "output_index": 0, "item": raw})
    final = step.consume(
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [raw, text_item(), call_item()],
            },
        }
    )
    assert isinstance(final[-1], Finish)
    assert final[-1].continuation.items == ()
    assert step.outputs[0].encrypted is None
    assert step.outputs[0].parts[("summary_text", 0)].saved is None


@pytest.mark.parametrize(
    "chunks",
    [
        [
            delta_frame("output_text", "partial"),
            completed([text_item(content=[{"type": "unknown_content"}])]),
        ],
        [
            delta_frame("reasoning_summary_text", "partial", "rs-one"),
            completed([reasoning_item() | {"summary": [{"type": "unknown_summary"}]}]),
        ],
        [
            delta_frame("output_text", "partial"),
            frame("response.unknown_event", output_index=0, item_id="wrong"),
            completed([text_item("partial")]),
        ],
    ],
)
def test_unknown_semantics_cannot_hide_conflicts_with_emitted_parts(chunks):
    events, _ = run(chunks)
    assert_error(events)
