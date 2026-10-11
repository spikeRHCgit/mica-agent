"""Immutable, source-bound model-step continuation contracts and strict wire parsing."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, TypeAlias

from .models import Message, MessageRole

MAX_TURN_CONTINUATION_BYTES = 1024 * 1024


class ProviderApi(Enum):
    OPENAI_COMPLETIONS = "openai-completions"
    OPENAI_RESPONSES = "openai-responses"
    ANTHROPIC_MESSAGES = "anthropic-messages"


class ContinuationStatus(Enum):
    REPLAYABLE = "replayable"
    UNREPLAYABLE = "unreplayable"


class TextPhase(Enum):
    COMMENTARY = "commentary"
    FINAL_ANSWER = "final_answer"


class ReasoningDetailType(Enum):
    SUMMARY = "reasoning.summary"
    ENCRYPTED = "reasoning.encrypted"
    TEXT = "reasoning.text"


class ContinuationFormatError(ValueError):
    """An untrusted continuation does not satisfy the wire contract."""


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str")


def _has_control(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in value)


def _require_id(value: object, name: str) -> None:
    _require_text(value, name)
    if not value or len(value) > 256 or any(char.isspace() for char in value):
        raise ValueError(
            f"{name} must be non-empty, without whitespace, at most 256 chars"
        )
    if _has_control(value):
        raise ValueError(f"{name} must not contain control characters")


def _require_opaque(value: object, name: str) -> None:
    _require_text(value, name)
    if not value or _has_control(value):
        raise ValueError(f"{name} must be non-empty and without control characters")


def _freeze_sequence(
    value: object, name: str, item_type: type | tuple[type, ...]
) -> tuple[Any, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be a sequence")
    result = tuple(value)
    if not all(isinstance(item, item_type) for item in result):
        raise TypeError(f"{name} contains an invalid item type")
    return result


@dataclass(frozen=True, slots=True)
class ReasoningDetail:
    """One whitelisted reasoning detail, with content selected by its type."""

    type: ReasoningDetailType
    id: str | None = None
    format: str | None = None
    index: int | None = None
    summary: str | None = field(default=None, repr=False)
    data: str | None = field(default=None, repr=False)
    text: str | None = field(default=None, repr=False)
    signature: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.type, ReasoningDetailType):
            raise TypeError("type must be a ReasoningDetailType")
        if self.id is not None:
            _require_id(self.id, "id")
        if self.format is not None:
            _require_text(self.format, "format")
        if self.index is not None:
            if isinstance(self.index, bool) or not isinstance(self.index, int):
                raise TypeError("index must be an int or None")
            if self.index < 0:
                raise ValueError("index must be non-negative")
        required = {
            ReasoningDetailType.SUMMARY: "summary",
            ReasoningDetailType.ENCRYPTED: "data",
            ReasoningDetailType.TEXT: "text",
        }[self.type]
        for name in ("summary", "data", "text", "signature"):
            value = getattr(self, name)
            if name == required:
                if name == "data":
                    _require_opaque(value, name)
                else:
                    _require_text(value, name)
            elif name == "signature" and self.type is ReasoningDetailType.TEXT:
                if value is not None:
                    _require_opaque(value, name)
            elif value is not None:
                raise ValueError(f"{name} must be None for this detail type")


@dataclass(frozen=True, slots=True)
class ResponsesReasoningItem:
    id: str
    summary: tuple[str, ...] = field(repr=False)
    encrypted_content: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_id(self.id, "id")
        object.__setattr__(
            self, "summary", _freeze_sequence(self.summary, "summary", str)
        )
        _require_opaque(self.encrypted_content, "encrypted_content")


@dataclass(frozen=True, slots=True)
class MessagesThinkingItem:
    thinking: str = field(repr=False)
    signature: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_text(self.thinking, "thinking")
        _require_opaque(self.signature, "signature")


@dataclass(frozen=True, slots=True)
class MessagesRedactedThinkingItem:
    data: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_opaque(self.data, "data")


@dataclass(frozen=True, slots=True)
class TextItem:
    phase: TextPhase | None

    def __post_init__(self) -> None:
        if self.phase is not None and not isinstance(self.phase, TextPhase):
            raise TypeError("phase must be a TextPhase or None")


@dataclass(frozen=True, slots=True)
class ToolCallItem:
    call_id: str

    def __post_init__(self) -> None:
        _require_id(self.call_id, "call_id")


@dataclass(frozen=True, slots=True)
class ChatReasoningItem:
    reasoning_content: str | None = field(repr=False)
    reasoning: str | None = field(repr=False)
    reasoning_details: tuple[ReasoningDetail, ...] = field(repr=False)

    def __post_init__(self) -> None:
        for name in ("reasoning_content", "reasoning"):
            value = getattr(self, name)
            if value is not None:
                _require_text(value, name)
        object.__setattr__(
            self,
            "reasoning_details",
            _freeze_sequence(
                self.reasoning_details, "reasoning_details", ReasoningDetail
            ),
        )
        if not (self.reasoning_content or self.reasoning or self.reasoning_details):
            raise ValueError("chat_reasoning requires non-empty content")


ContinuationItem: TypeAlias = (
    ResponsesReasoningItem
    | MessagesThinkingItem
    | MessagesRedactedThinkingItem
    | TextItem
    | ToolCallItem
    | ChatReasoningItem
)

_ITEM_TYPES = {
    "reasoning": ResponsesReasoningItem,
    "thinking": MessagesThinkingItem,
    "redacted_thinking": MessagesRedactedThinkingItem,
    "text": TextItem,
    "tool_call": ToolCallItem,
    "chat_reasoning": ChatReasoningItem,
}
_API_ITEMS = {
    ProviderApi.OPENAI_COMPLETIONS: (ChatReasoningItem,),
    ProviderApi.OPENAI_RESPONSES: (ResponsesReasoningItem, TextItem, ToolCallItem),
    ProviderApi.ANTHROPIC_MESSAGES: (
        MessagesThinkingItem,
        MessagesRedactedThinkingItem,
        TextItem,
        ToolCallItem,
    ),
}


@dataclass(frozen=True, slots=True)
class ModelStepContinuation:
    """Ordered continuation items bound to one provider, model and API."""

    provider_id: str
    model_id: str
    api: ProviderApi
    status: ContinuationStatus
    items: tuple[ContinuationItem, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        for name in ("provider_id", "model_id"):
            value = getattr(self, name)
            _require_text(value, name)
            if not value.strip():
                raise ValueError(f"{name} must be non-empty and non-blank")
        if not isinstance(self.api, ProviderApi):
            raise TypeError("api must be a ProviderApi")
        if not isinstance(self.status, ContinuationStatus):
            raise TypeError("status must be a ContinuationStatus")
        object.__setattr__(
            self,
            "items",
            _freeze_sequence(self.items, "items", tuple(_ITEM_TYPES.values())),
        )
        if self.status is ContinuationStatus.UNREPLAYABLE:
            if self.items:
                raise ValueError("unreplayable items must be empty")
            return
        if not self.items:
            raise ValueError("replayable items must be non-empty")
        if not all(isinstance(item, _API_ITEMS[self.api]) for item in self.items):
            raise ValueError("items are not allowed for this api")
        if self.api is ProviderApi.OPENAI_COMPLETIONS and len(self.items) != 1:
            raise ValueError(
                "completions items must contain exactly one chat_reasoning"
            )
        text_items = [item for item in self.items if isinstance(item, TextItem)]
        if len(text_items) > 1:
            raise ValueError("items must contain at most one text")
        if self.api is ProviderApi.ANTHROPIC_MESSAGES and any(
            item.phase is not None for item in text_items
        ):
            raise ValueError("phase must be None for messages text")
        call_ids = [
            item.call_id for item in self.items if isinstance(item, ToolCallItem)
        ]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("call_id must not be repeated")


def _serialize_fields(value: object, schema: type) -> dict[str, object]:
    result = {}
    for entry in fields(schema):
        member = getattr(value, entry.name)
        if isinstance(member, Enum):
            member = member.value
        elif isinstance(member, tuple):
            member = [
                _serialize_fields(item, ReasoningDetail)
                if isinstance(item, ReasoningDetail)
                else item
                for item in member
            ]
        result[entry.name] = member
    return result


def serialize_continuation(record: ModelStepContinuation) -> dict[str, object]:
    """Produce the complete whitelisted JSON shape, including nullable fields."""
    if not isinstance(record, ModelStepContinuation):
        raise TypeError("record must be a ModelStepContinuation")
    return {
        "provider_id": record.provider_id,
        "model_id": record.model_id,
        "api": record.api.value,
        "status": record.status.value,
        "items": [
            {"type": name, **_serialize_fields(item, item_type)}
            for item in record.items
            for name, item_type in _ITEM_TYPES.items()
            if isinstance(item, item_type)
        ],
    }


def _require_exact_fields(
    value: object, expected: frozenset[str], name: str
) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise ContinuationFormatError(f"{name} must be a JSON object")
    missing = expected - set(value)
    if missing:
        raise ContinuationFormatError(
            f"{name} is missing fields: {', '.join(sorted(missing))}"
        )
    if set(value) - expected:
        raise ContinuationFormatError(f"{name} contains unexpected fields")
    return value


def _parse_enum(value: object, enum_type: type[Enum], name: str) -> Enum:
    if not isinstance(value, str):
        raise ContinuationFormatError(f"{name} must be a string")
    try:
        return enum_type(value)
    except ValueError:
        raise ContinuationFormatError(f"{name} has an unknown value") from None


def _require_array(value: object, name: str) -> list:
    if not isinstance(value, list):
        raise ContinuationFormatError(f"{name} must be an array")
    return value


def _parse_detail(value: object) -> ReasoningDetail:
    detail = _require_exact_fields(
        value,
        frozenset(entry.name for entry in fields(ReasoningDetail)),
        "reasoning_detail",
    )
    return ReasoningDetail(
        **{
            **detail,
            "type": _parse_enum(detail["type"], ReasoningDetailType, "type"),
        }
    )


def _parse_item(value: object) -> ContinuationItem:
    if not isinstance(value, Mapping) or "type" not in value:
        raise ContinuationFormatError("item must be a JSON object with type")
    name = value["type"]
    if not isinstance(name, str) or name not in _ITEM_TYPES:
        raise ContinuationFormatError("type has an unknown value")
    item_type = _ITEM_TYPES[name]
    item = _require_exact_fields(
        value, frozenset({"type", *(entry.name for entry in fields(item_type))}), "item"
    )
    values = {key: member for key, member in item.items() if key != "type"}
    if item_type is TextItem and values["phase"] is not None:
        values["phase"] = _parse_enum(values["phase"], TextPhase, "phase")
    if item_type is ResponsesReasoningItem:
        values["summary"] = _require_array(values["summary"], "summary")
    if item_type is ChatReasoningItem:
        values["reasoning_details"] = tuple(
            _parse_detail(detail)
            for detail in _require_array(
                values["reasoning_details"], "reasoning_details"
            )
        )
    return item_type(**values)


def parse_continuation(value: object) -> ModelStepContinuation:
    """Reject malformed wire data without including untrusted values in errors."""
    try:
        record = _require_exact_fields(
            value,
            frozenset({"provider_id", "model_id", "api", "status", "items"}),
            "continuation",
        )
        return ModelStepContinuation(
            provider_id=record["provider_id"],
            model_id=record["model_id"],
            api=_parse_enum(record["api"], ProviderApi, "api"),
            status=_parse_enum(record["status"], ContinuationStatus, "status"),
            items=tuple(
                _parse_item(item) for item in _require_array(record["items"], "items")
            ),
        )
    except ContinuationFormatError:
        raise
    except (TypeError, ValueError) as exc:
        raise ContinuationFormatError(str(exc)) from None


def continuation_size(record: ModelStepContinuation) -> int:
    """Measure compact, sorted JSON as UTF-8 bytes."""
    return len(
        json.dumps(
            serialize_continuation(record),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    )


def apply_turn_size_limit(
    records: Sequence[ModelStepContinuation | None],
) -> tuple[ModelStepContinuation | None, ...]:
    """Discard every step's replay data together when the turn exceeds its budget."""
    records = tuple(records)
    if (
        sum(continuation_size(record) for record in records if record is not None)
        <= MAX_TURN_CONTINUATION_BYTES
    ):
        return records
    return tuple(
        None
        if record is None
        else ModelStepContinuation(
            record.provider_id,
            record.model_id,
            record.api,
            ContinuationStatus.UNREPLAYABLE,
        )
        for record in records
    )


def check_step_association(
    record: ModelStepContinuation, assistant_message: Message
) -> None:
    """Check ordered references against the step's original assistant message."""
    if not isinstance(record, ModelStepContinuation):
        raise TypeError("record must be a ModelStepContinuation")
    if not isinstance(assistant_message, Message):
        raise TypeError("assistant_message must be a Message")
    if assistant_message.role is not MessageRole.ASSISTANT:
        raise ValueError("assistant_message must have assistant role")
    # Unreplayable markers and completions contain no message references.
    if (
        record.status is ContinuationStatus.UNREPLAYABLE
        or record.api is ProviderApi.OPENAI_COMPLETIONS
    ):
        return
    call_ids = tuple(
        item.call_id for item in record.items if isinstance(item, ToolCallItem)
    )
    if call_ids != tuple(call.call_id for call in assistant_message.tool_calls):
        raise ValueError(
            "tool_call references must match assistant tool_calls in order"
        )
    text_count = sum(isinstance(item, TextItem) for item in record.items)
    if text_count != bool(assistant_message.content):
        raise ValueError("text references must match assistant content presence")


__all__ = [
    "MAX_TURN_CONTINUATION_BYTES",
    "ChatReasoningItem",
    "ContinuationFormatError",
    "ContinuationItem",
    "ContinuationStatus",
    "MessagesRedactedThinkingItem",
    "MessagesThinkingItem",
    "ModelStepContinuation",
    "ProviderApi",
    "ReasoningDetail",
    "ReasoningDetailType",
    "ResponsesReasoningItem",
    "TextItem",
    "TextPhase",
    "ToolCallItem",
    "apply_turn_size_limit",
    "check_step_association",
    "continuation_size",
    "parse_continuation",
    "serialize_continuation",
]
