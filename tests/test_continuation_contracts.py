"""模型步骤续接的 wire、校验、关联和安全边界测试。"""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from mica_agent.continuation_contracts import (
    MAX_TURN_CONTINUATION_BYTES,
    ChatReasoningItem,
    ContinuationFormatError,
    ContinuationStatus,
    MessagesRedactedThinkingItem,
    MessagesThinkingItem,
    ModelStepContinuation,
    ProviderApi,
    ReasoningDetail,
    ReasoningDetailType,
    ResponsesReasoningItem,
    TextItem,
    TextPhase,
    ToolCallItem,
    apply_turn_size_limit,
    check_step_association,
    continuation_size,
    parse_continuation,
    serialize_continuation,
)
from mica_agent.message_sanitizer import sanitize_step_continuation
from mica_agent.models import Message, MessageRole
from mica_agent.tool_contracts import ToolCall

RESPONSES_WIRE = {
    "provider_id": "provider-1",
    "model_id": "model-1",
    "api": "openai-responses",
    "status": "replayable",
    "items": [
        {
            "type": "reasoning",
            "id": "reason-1",
            "summary": ["总结", "more thought"],
            "encrypted_content": "opaque-encrypted",
        },
        {"type": "text", "phase": "commentary"},
        {"type": "tool_call", "call_id": "call-1"},
        {"type": "tool_call", "call_id": "call-2"},
    ],
}
MESSAGES_WIRE = {
    "provider_id": "provider-1",
    "model_id": "model-1",
    "api": "anthropic-messages",
    "status": "replayable",
    "items": [
        {"type": "thinking", "thinking": "thought", "signature": "opaque-signature"},
        {"type": "redacted_thinking", "data": "opaque-redacted"},
        {"type": "text", "phase": None},
        {"type": "tool_call", "call_id": "call-1"},
    ],
}
CHAT_WIRE = {
    "provider_id": "provider-1",
    "model_id": "model-1",
    "api": "openai-completions",
    "status": "replayable",
    "items": [
        {
            "type": "chat_reasoning",
            "reasoning_content": "deep thought",
            "reasoning": None,
            "reasoning_details": [
                {
                    "type": "reasoning.summary",
                    "id": "detail-1",
                    "format": "format-1",
                    "index": 0,
                    "summary": "summary thought",
                    "data": None,
                    "text": None,
                    "signature": None,
                },
                {
                    "type": "reasoning.encrypted",
                    "id": None,
                    "format": None,
                    "index": None,
                    "summary": None,
                    "data": "detail-encrypted",
                    "text": None,
                    "signature": None,
                },
                {
                    "type": "reasoning.text",
                    "id": None,
                    "format": None,
                    "index": 2,
                    "summary": None,
                    "data": None,
                    "text": "detail thought",
                    "signature": "detail-signature",
                },
            ],
        }
    ],
}


def make_record(api=ProviderApi.OPENAI_RESPONSES, items=None, **changes):
    values = {
        "provider_id": "provider-1",
        "model_id": "model-1",
        "api": api,
        "status": ContinuationStatus.REPLAYABLE,
        "items": (ResponsesReasoningItem("reason-1", (), "opaque"),)
        if items is None
        else items,
    }
    values.update(changes)
    return ModelStepContinuation(**values)


@pytest.mark.parametrize(
    ("record", "wire"),
    [
        (
            make_record(
                items=[
                    ResponsesReasoningItem(
                        "reason-1", ["总结", "more thought"], "opaque-encrypted"
                    ),
                    TextItem(TextPhase.COMMENTARY),
                    ToolCallItem("call-1"),
                    ToolCallItem("call-2"),
                ]
            ),
            RESPONSES_WIRE,
        ),
        (
            make_record(
                ProviderApi.ANTHROPIC_MESSAGES,
                [
                    MessagesThinkingItem("thought", "opaque-signature"),
                    MessagesRedactedThinkingItem("opaque-redacted"),
                    TextItem(None),
                    ToolCallItem("call-1"),
                ],
            ),
            MESSAGES_WIRE,
        ),
        (
            make_record(
                ProviderApi.OPENAI_COMPLETIONS,
                [
                    ChatReasoningItem(
                        "deep thought",
                        None,
                        [
                            ReasoningDetail(
                                ReasoningDetailType.SUMMARY,
                                "detail-1",
                                "format-1",
                                0,
                                summary="summary thought",
                            ),
                            ReasoningDetail(
                                ReasoningDetailType.ENCRYPTED, data="detail-encrypted"
                            ),
                            ReasoningDetail(
                                ReasoningDetailType.TEXT,
                                index=2,
                                text="detail thought",
                                signature="detail-signature",
                            ),
                        ],
                    )
                ],
            ),
            CHAT_WIRE,
        ),
    ],
)
def test_fixed_wire_shapes_and_round_trips(record, wire):
    assert serialize_continuation(record) == wire
    assert parse_continuation(json.loads(json.dumps(wire))) == record


@pytest.mark.parametrize("api", list(ProviderApi))
def test_unreplayable_wire_is_empty_for_each_api(api):
    record = make_record(api, (), status=ContinuationStatus.UNREPLAYABLE)
    wire = {
        "provider_id": "provider-1",
        "model_id": "model-1",
        "api": api.value,
        "status": "unreplayable",
        "items": [],
    }
    assert serialize_continuation(record) == wire
    assert parse_continuation(wire) == record


def test_collections_are_frozen_and_do_not_alias_inputs():
    summary = ["thought"]
    details = [ReasoningDetail(ReasoningDetailType.TEXT, text="thought")]
    item = ResponsesReasoningItem("reason-1", summary, "opaque")
    chat = ChatReasoningItem(None, None, details)
    items = [item]
    record = make_record(items=items)
    summary.clear()
    details.clear()
    items.clear()
    assert item.summary == ("thought",)
    assert len(chat.reasoning_details) == 1
    assert record.items == (item,)
    for value, name in [
        (record, "items"),
        (item, "summary"),
        (chat, "reasoning_details"),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(value, name, ())


# 固定样例的逐层变异，验证不可信输入的严格边界。
@pytest.mark.parametrize(
    ("base", "path", "value"),
    [
        (RESPONSES_WIRE, ("extra",), "private-value"),
        (RESPONSES_WIRE, ("api",), "private-unknown-api"),
        (RESPONSES_WIRE, ("status",), "private-unknown-status"),
        (RESPONSES_WIRE, ("items", 0, "type"), "private-unknown-type"),
        (RESPONSES_WIRE, ("items", 0, "extra"), "private-value"),
        (RESPONSES_WIRE, ("items", 1, "phase"), "private-unknown-phase"),
        (RESPONSES_WIRE, ("items", 0, "id"), "private id"),
        (RESPONSES_WIRE, ("items", 0, "id"), "x" * 257),
        (RESPONSES_WIRE, ("items", 0, "id"), "private\x7fvalue"),
        (RESPONSES_WIRE, ("items", 2, "call_id"), "private call"),
        (RESPONSES_WIRE, ("items", 0, "summary"), "private-value"),
        (RESPONSES_WIRE, ("items", 0, "summary"), [4]),
        (RESPONSES_WIRE, ("items", 0, "encrypted_content"), ""),
        (RESPONSES_WIRE, ("items", 0, "encrypted_content"), "private\nvalue"),
        (RESPONSES_WIRE, ("items", 0, "encrypted_content"), 4),
        (RESPONSES_WIRE, ("items",), ()),
        (RESPONSES_WIRE, ("items",), []),
        (RESPONSES_WIRE, ("status",), "unreplayable"),
        (RESPONSES_WIRE, ("api",), "anthropic-messages"),
        (RESPONSES_WIRE, ("items",), [{"type": "text", "phase": None}] * 2),
        (RESPONSES_WIRE, ("items",), [{"type": "tool_call", "call_id": "call-1"}] * 2),
        (RESPONSES_WIRE, ("provider_id",), "   "),
        (RESPONSES_WIRE, ("model_id",), 4),
        (MESSAGES_WIRE, ("items", 2, "phase"), "final_answer"),
        (MESSAGES_WIRE, ("items", 0, "thinking"), 4),
        (MESSAGES_WIRE, ("items", 0, "signature"), ""),
        (MESSAGES_WIRE, ("items", 0, "signature"), "private\x00value"),
        (MESSAGES_WIRE, ("items", 1, "data"), ""),
        (MESSAGES_WIRE, ("items", 1, "data"), "private\x85value"),
        (CHAT_WIRE, ("items",), []),
        (CHAT_WIRE, ("items",), CHAT_WIRE["items"] * 2),
        (
            CHAT_WIRE,
            ("items",),
            [
                {
                    "type": "chat_reasoning",
                    "reasoning_content": "",
                    "reasoning": None,
                    "reasoning_details": [],
                }
            ],
        ),
        (CHAT_WIRE, ("items", 0, "reasoning"), 4),
        (CHAT_WIRE, ("items", 0, "reasoning_details"), {}),
        (
            CHAT_WIRE,
            ("items", 0, "reasoning_details", 0, "type"),
            "private-unknown-detail",
        ),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 0, "summary"), None),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 0, "text"), "private-value"),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 0, "signature"), "private-value"),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 0, "index"), -1),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 0, "index"), True),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 0, "id"), "private id"),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 0, "format"), 4),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 0, "extra"), "private-value"),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 1, "data"), ""),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 1, "data"), "private\nvalue"),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 1, "summary"), "private-value"),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 2, "text"), None),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 2, "data"), "private-value"),
        (CHAT_WIRE, ("items", 0, "reasoning_details", 2, "signature"), ""),
    ],
)
def test_parse_rejects_invalid_wire_without_echoing_values(base, path, value):
    wire = copy.deepcopy(base)
    target = wire
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ContinuationFormatError) as error:
        parse_continuation(wire)
    assert "private" not in str(error.value)
    assert "thought" not in str(error.value)


@pytest.mark.parametrize("base", [RESPONSES_WIRE, MESSAGES_WIRE, CHAT_WIRE])
def test_parse_requires_every_field_at_every_level(base):
    paths = [()]
    paths.extend(("items", index) for index in range(len(base["items"])))
    if base is CHAT_WIRE:
        paths.extend(("items", 0, "reasoning_details", index) for index in range(3))
    for path in paths:
        original = base
        for key in path:
            original = original[key]
        for field_name in original:
            wire = copy.deepcopy(base)
            target = wire
            for key in path:
                target = target[key]
            del target[field_name]
            with pytest.raises(ContinuationFormatError):
                parse_continuation(wire)


@pytest.mark.parametrize("value", [None, [], "private-value", 4, {"items": []}])
def test_parse_rejects_non_record_inputs(value):
    with pytest.raises(ContinuationFormatError):
        parse_continuation(value)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: make_record(items=[]),
        lambda: make_record(status=ContinuationStatus.UNREPLAYABLE),
        lambda: make_record(items=[TextItem(None), TextItem(None)]),
        lambda: make_record(items=[ToolCallItem("call"), ToolCallItem("call")]),
        lambda: make_record(items=[MessagesThinkingItem("", "opaque")]),
        lambda: make_record(
            ProviderApi.ANTHROPIC_MESSAGES, [TextItem(TextPhase.COMMENTARY)]
        ),
        lambda: make_record(
            ProviderApi.OPENAI_COMPLETIONS, [ChatReasoningItem("thought", None, ())] * 2
        ),
        lambda: make_record(provider_id=""),
        lambda: make_record(model_id="\t"),
        lambda: make_record(api="openai-responses"),
        lambda: make_record(status="replayable"),
        lambda: make_record(items=[{}]),
        lambda: make_record(items="private-value"),
        lambda: ResponsesReasoningItem("bad id", (), "opaque"),
        lambda: ResponsesReasoningItem("x" * 257, (), "opaque"),
        lambda: ResponsesReasoningItem("reason", "private-value", "opaque"),
        lambda: ResponsesReasoningItem("reason", [4], "opaque"),
        lambda: ResponsesReasoningItem("reason", (), ""),
        lambda: MessagesThinkingItem(4, "opaque"),
        lambda: MessagesThinkingItem("private-thought", "private\nvalue"),
        lambda: MessagesRedactedThinkingItem(""),
        lambda: ToolCallItem("bad\x00id"),
        lambda: TextItem("private-phase"),
        lambda: ChatReasoningItem(None, "", ()),
        lambda: ChatReasoningItem(None, 4, ()),
        lambda: ChatReasoningItem(None, None, [{}]),
        lambda: ReasoningDetail(ReasoningDetailType.SUMMARY),
        lambda: ReasoningDetail(
            ReasoningDetailType.SUMMARY, summary="private", data="private"
        ),
        lambda: ReasoningDetail(ReasoningDetailType.ENCRYPTED, data=""),
        lambda: ReasoningDetail(ReasoningDetailType.TEXT, text="private", signature=""),
        lambda: ReasoningDetail(ReasoningDetailType.TEXT, text="private", index=True),
        lambda: ReasoningDetail(ReasoningDetailType.TEXT, text="private", index=-1),
        lambda: ReasoningDetail("private-type", text="private"),
    ],
)
def test_constructors_reject_invalid_types_and_values_safely(factory):
    with pytest.raises((TypeError, ValueError)) as error:
        factory()
    assert "private" not in str(error.value)


@pytest.mark.parametrize("phase", [None, TextPhase.COMMENTARY, TextPhase.FINAL_ANSWER])
def test_responses_accepts_supported_phases(phase):
    record = make_record(items=[TextItem(phase)])
    assert parse_continuation(serialize_continuation(record)) == record


def test_empty_plaintext_and_details_only_chat_are_valid():
    assert MessagesThinkingItem("", "opaque").thinking == ""
    for item in [
        ChatReasoningItem(None, "thought", ()),
        ChatReasoningItem(
            None, None, (ReasoningDetail(ReasoningDetailType.SUMMARY, summary=""),)
        ),
        ChatReasoningItem(
            None, None, (ReasoningDetail(ReasoningDetailType.TEXT, text=""),)
        ),
    ]:
        record = make_record(ProviderApi.OPENAI_COMPLETIONS, [item])
        assert parse_continuation(serialize_continuation(record)) == record


def assistant(content="answer", call_ids=("call-1", "call-2"), arguments=None):
    return Message(
        MessageRole.ASSISTANT,
        content,
        tuple(
            ToolCall(call_id, "read", {} if arguments is None else arguments)
            for call_id in call_ids
        ),
    )


def test_association_accepts_order_and_pure_reasoning():
    check_step_association(parse_continuation(RESPONSES_WIRE), assistant())
    check_step_association(make_record(), assistant("", ()))
    check_step_association(parse_continuation(CHAT_WIRE), assistant())
    check_step_association(
        make_record(items=(), status=ContinuationStatus.UNREPLAYABLE), assistant()
    )


@pytest.mark.parametrize(
    ("content", "call_ids"),
    [
        ("answer", ("call-2", "call-1")),
        ("answer", ("call-1",)),
        ("answer", ("call-1", "call-2", "call-3")),
        ("", ("call-1", "call-2")),
    ],
)
def test_association_rejects_mismatched_references(content, call_ids):
    with pytest.raises(ValueError):
        check_step_association(
            parse_continuation(RESPONSES_WIRE), assistant(content, call_ids)
        )


def test_association_requires_text_for_nonempty_body_and_assistant_role():
    with pytest.raises(ValueError):
        check_step_association(make_record(), assistant("answer", ()))
    for role in [MessageRole.USER, MessageRole.SYSTEM]:
        with pytest.raises(ValueError):
            check_step_association(make_record(), Message(role, ""))


def test_size_is_compact_sorted_utf8_and_limit_is_atomic():
    first = make_record(items=[ResponsesReasoningItem("reason-1", ["总结"], "opaque")])
    assert continuation_size(first) == len(
        json.dumps(
            serialize_continuation(first),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    )
    second = make_record()
    remaining = (
        MAX_TURN_CONTINUATION_BYTES
        - continuation_size(first)
        - continuation_size(second)
    )
    second = replace(
        second,
        items=(ResponsesReasoningItem("reason-1", (), "opaque" + "x" * remaining),),
    )
    records = (first, None, second)
    assert (
        sum(continuation_size(value) for value in (first, second))
        == MAX_TURN_CONTINUATION_BYTES
    )
    within = apply_turn_size_limit(records)
    assert all(left is right for left, right in zip(within, records, strict=True))
    over = replace(
        second,
        items=(
            ResponsesReasoningItem(
                "reason-1", (), second.items[0].encrypted_content + "x"
            ),
        ),
    )
    assert (
        continuation_size(first) + continuation_size(over)
        == MAX_TURN_CONTINUATION_BYTES + 1
    )
    limited = apply_turn_size_limit([first, None, over])
    assert limited[1] is None
    for original, changed in [(first, limited[0]), (over, limited[2])]:
        assert changed == replace(
            original, status=ContinuationStatus.UNREPLAYABLE, items=()
        )
    assert apply_turn_size_limit([None]) == (None,)


@pytest.mark.parametrize("wire", [RESPONSES_WIRE, MESSAGES_WIRE, CHAT_WIRE])
def test_plain_continuations_return_original_identity(wire):
    record = parse_continuation(wire)
    assert sanitize_step_continuation(record, assistant()) is record
    marker = replace(record, status=ContinuationStatus.UNREPLAYABLE, items=())
    assert (
        sanitize_step_continuation(marker, assistant(arguments={"token": "isolated"}))
        is marker
    )


def test_tool_credentials_invalidate_replay_without_changing_original():
    record = parse_continuation(RESPONSES_WIRE)
    message = assistant(arguments={"query": "sk-isolatedTestValue"})
    changed = sanitize_step_continuation(record, message)
    assert changed == replace(record, status=ContinuationStatus.UNREPLAYABLE, items=())
    assert message.tool_calls[0].arguments["query"] == "sk-isolatedTestValue"
    assert record.status is ContinuationStatus.REPLAYABLE


@pytest.mark.parametrize(
    ("api", "item"),
    [
        (
            ProviderApi.OPENAI_RESPONSES,
            ResponsesReasoningItem("reason", ["/isolated/local/file"], "opaque"),
        ),
        (
            ProviderApi.ANTHROPIC_MESSAGES,
            MessagesThinkingItem(r"C:\isolated\local\file", "opaque"),
        ),
        (
            ProviderApi.OPENAI_COMPLETIONS,
            ChatReasoningItem("/isolated/local/file", None, ()),
        ),
        (
            ProviderApi.OPENAI_COMPLETIONS,
            ChatReasoningItem(None, "sk-isolatedTestValue", ()),
        ),
        (
            ProviderApi.OPENAI_COMPLETIONS,
            ChatReasoningItem(
                None,
                None,
                [
                    ReasoningDetail(
                        ReasoningDetailType.SUMMARY, summary="/isolated/local/file"
                    )
                ],
            ),
        ),
        (
            ProviderApi.OPENAI_COMPLETIONS,
            ChatReasoningItem(
                None,
                None,
                [
                    ReasoningDetail(
                        ReasoningDetailType.TEXT, text="/isolated/local/file"
                    )
                ],
            ),
        ),
    ],
)
def test_all_plaintext_fields_invalidate_replay_on_sanitization(api, item):
    record = make_record(api, [item])
    assert sanitize_step_continuation(record, assistant("", ())) == replace(
        record, status=ContinuationStatus.UNREPLAYABLE, items=()
    )


@pytest.mark.parametrize(
    ("api", "item"),
    [
        (
            ProviderApi.OPENAI_RESPONSES,
            ResponsesReasoningItem("reason", (), "sk-isolatedTestValue"),
        ),
        (
            ProviderApi.ANTHROPIC_MESSAGES,
            MessagesThinkingItem("", "sk-isolatedTestValue"),
        ),
        (
            ProviderApi.ANTHROPIC_MESSAGES,
            MessagesRedactedThinkingItem("sk-isolatedTestValue"),
        ),
        (
            ProviderApi.OPENAI_COMPLETIONS,
            ChatReasoningItem(
                None,
                None,
                [
                    ReasoningDetail(
                        ReasoningDetailType.ENCRYPTED, data="sk-isolatedTestValue"
                    )
                ],
            ),
        ),
        (
            ProviderApi.OPENAI_COMPLETIONS,
            ChatReasoningItem(
                None,
                None,
                [
                    ReasoningDetail(
                        ReasoningDetailType.TEXT,
                        text="",
                        signature="sk-isolatedTestValue",
                    )
                ],
            ),
        ),
    ],
)
def test_opaque_fields_are_preserved_without_regex_replacement(api, item):
    record = make_record(api, [item])
    assert sanitize_step_continuation(record, assistant("", ())) is record


@pytest.mark.parametrize("wire", [RESPONSES_WIRE, MESSAGES_WIRE, CHAT_WIRE])
def test_record_and_nested_repr_hide_content(wire):
    record = parse_continuation(wire)
    representations = [repr(record), *(repr(item) for item in record.items)]
    for item in record.items:
        if isinstance(item, ChatReasoningItem):
            representations.extend(repr(detail) for detail in item.reasoning_details)
    for representation in representations:
        for value in [
            "总结",
            "more thought",
            "thought",
            "opaque-encrypted",
            "opaque-signature",
            "opaque-redacted",
            "deep thought",
            "summary thought",
            "detail-encrypted",
            "detail thought",
            "detail-signature",
        ]:
            assert value not in representation
