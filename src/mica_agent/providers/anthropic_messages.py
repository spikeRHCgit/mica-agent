"""Single-step Messages adaptation with strict terminals and bounded private replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import httpx

from ..continuation_contracts import (
    MAX_TURN_CONTINUATION_BYTES,
    ContinuationStatus,
    MessagesRedactedThinkingItem,
    MessagesThinkingItem,
    ModelStepContinuation,
    ProviderApi,
    TextItem,
    ToolCallItem,
    continuation_size,
)
from ..models import MessageRole
from ..reasoning import ReasoningEffort
from ..tool_contracts import ToolCall, to_mutable_json
from .base import (
    AuthenticationError,
    Finish,
    MalformedResponseError,
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

DEFAULT_MAX_TOKENS = 4096
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
DEFAULT_THINKING_LEVEL_MAP = MappingProxyType(
    {effort: effort.value for effort in ReasoningEffort if effort.value in _EFFORTS}
)
_ID = re.compile(r"[a-zA-Z0-9_-]+\Z")
SleepFn = Callable[[float], Awaitable[None]]


def resolve_endpoint(base_url: str) -> str:
    """Resolve /messages and remove URL credentials before HTTP authentication."""
    try:
        url = httpx.URL(base_url)
    except (httpx.InvalidURL, TypeError):
        raise ValueError("base_url must be a valid HTTP URL") from None
    if url.scheme not in ("http", "https") or not url.host:
        raise ValueError("base_url must be a valid HTTP URL")
    path = url.path.rstrip("/")
    if not path.endswith("/messages"):
        path += "/messages"
    return str(url.copy_with(path=path, username="", password=""))


def _thinking_map(value: Mapping[ReasoningEffort, str] | None) -> Mapping:
    if value is None:
        return DEFAULT_THINKING_LEVEL_MAP
    if not isinstance(value, Mapping):
        raise TypeError("thinking_level_map must be a mapping or None")
    if not value:
        return DEFAULT_THINKING_LEVEL_MAP
    if any(
        not isinstance(key, ReasoningEffort)
        or key is ReasoningEffort.OFF
        or not isinstance(target, str)
        or target not in _EFFORTS
        for key, target in value.items()
    ):
        raise ValueError("thinking_level_map is invalid")
    return MappingProxyType(dict(value))


def _malformed() -> MalformedResponseError:
    return MalformedResponseError("invalid Messages stream")


def _string(value: object, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise _malformed()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _malformed() from None
    return value


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _malformed()
    return value


def _validate_json(value: object) -> None:
    if isinstance(value, str):
        _string(value)
    elif isinstance(value, float) and not math.isfinite(value):
        raise _malformed()
    elif isinstance(value, dict):
        for key, member in value.items():
            _string(key)
            _validate_json(member)
    elif isinstance(value, list):
        for member in value:
            _validate_json(member)


def _json(value: str) -> Any:
    def reject_constant(_: str) -> None:
        raise ValueError

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, member in pairs:
            if key in result:
                raise ValueError
            result[key] = member
        return result

    try:
        parsed = json.loads(
            value, parse_constant=reject_constant, object_pairs_hook=unique_pairs
        )
        _validate_json(parsed)
        return parsed
    except (ValueError, RecursionError):
        raise _malformed() from None


async def _sse_frames(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    data: list[str] = []
    event: str | None = None
    async for line in response.aiter_lines():
        if line == "":
            if data:
                parsed = _object(_json("\n".join(data)))
                kind = _string(parsed.get("type"), nonempty=True)
                if event is not None and event != kind:
                    raise _malformed()
                yield parsed
            data = []
            event = None
        elif not line.startswith(":"):
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if name == "data":
                data.append(value)
            elif name == "event":
                event = value
    raise _malformed()


def _wire_size(value: object) -> int:
    return len(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    )


@dataclass(repr=False)
class _PrivateText:
    saved: str | None = ""
    length: int = 0
    digest: Any = field(default_factory=hashlib.sha256)

    def append(self, value: str) -> None:
        self.length += len(value)
        self.digest.update(value.encode("utf-8"))
        if self.saved is not None:
            self.saved += value


@dataclass(repr=False)
class _Block:
    kind: str
    stopped: bool = False
    text_length: int = 0
    thinking: _PrivateText = field(default_factory=_PrivateText)
    signature: _PrivateText = field(default_factory=_PrivateText)
    data: _PrivateText = field(default_factory=_PrivateText)
    call_id: str | None = None
    name: str | None = None
    start_input: dict[str, Any] = field(default_factory=dict)
    fragments: list[str] = field(default_factory=list)
    has_fragments: bool = False
    call: ToolCall | None = None


_BLOCK_FIELDS = {
    "text": {"type", "text", "citations"},
    "thinking": {"type", "thinking", "signature"},
    "redacted_thinking": {"type", "data"},
    "tool_use": {"type", "id", "name", "input"},
}
_FRAME_FIELDS = {
    "ping": {"type"},
    "message_start": {"type", "message"},
    "content_block_start": {"type", "index", "content_block"},
    "content_block_delta": {"type", "index", "delta"},
    "content_block_stop": {"type", "index"},
    "message_delta": {"type", "delta", "usage"},
    "message_stop": {"type"},
}


class _MessagesStep:
    """Own one message's lifecycle and release all private data on degradation."""

    def __init__(self, request: ProviderRequest) -> None:
        self.request = request
        self.started = False
        self.ending = False
        self.reason: str | None = None
        self.blocks: list[_Block] = []
        self.call_ids: set[str] = set()
        self.discarded = request.provider_id is None
        self.item_count = 0
        self.text_blocks = 0
        # The two status spellings differ by two bytes; [] has no item separator.
        self.size = (
            continuation_size(self._record(ContinuationStatus.UNREPLAYABLE)) - 2
            if request.provider_id is not None
            else 0
        )

    def _record(self, status: ContinuationStatus, items=()) -> ModelStepContinuation:
        return ModelStepContinuation(
            self.request.provider_id,
            self.request.model,
            ProviderApi.ANTHROPIC_MESSAGES,
            status,
            tuple(items),
        )

    def discard(self) -> None:
        self.discarded = True
        for block in self.blocks:
            block.thinking.saved = None
            block.signature.saved = None
            block.data.saved = None

    def _add_size(self, amount: int) -> None:
        if not self.discarded:
            self.size += amount
            if self.size > MAX_TURN_CONTINUATION_BYTES:
                self.discard()

    def _item(self, wire: dict[str, Any]) -> None:
        self._add_size(_wire_size(wire) + bool(self.item_count))
        self.item_count += 1

    def _private(self, target: _PrivateText, value: str) -> None:
        self._add_size(_wire_size(value) - 2)
        target.append(value)

    def _text(self, block: _Block, value: str) -> list[ProviderEvent]:
        if value and not block.text_length:
            self._item({"type": "text", "phase": None})
        block.text_length += len(value)
        return [TextDelta(value)] if value else []

    def _block(self, index: object) -> _Block:
        if type(index) is not int or not 0 <= index < len(self.blocks):
            raise _malformed()
        block = self.blocks[index]
        if block.stopped or self.ending:
            raise _malformed()
        return block

    def _start_block(self, frame: dict[str, Any]) -> list[ProviderEvent]:
        index = frame.get("index")
        if (
            type(index) is not int
            or index != len(self.blocks)
            or self.ending
            or any(not block.stopped for block in self.blocks)
        ):
            raise _malformed()
        raw = _object(frame.get("content_block"))
        kind = _string(raw.get("type"), nonempty=True)
        block = _Block(kind)
        self.blocks.append(block)
        if self.discarded:
            self.discard()
        if kind not in _BLOCK_FIELDS or set(raw) - _BLOCK_FIELDS[kind]:
            self.discard()
        if kind == "text":
            self.text_blocks += 1
            if self.text_blocks > 1 or raw.get("citations"):
                self.discard()
            return self._text(block, _string(raw.get("text")))
        if kind == "thinking":
            self._item({"type": "thinking", "thinking": "", "signature": ""})
            thinking = _string(raw.get("thinking"))
            signature = _string(raw.get("signature", ""))
            self._private(block.thinking, thinking)
            self._private(block.signature, signature)
            return [ProviderReasoningDelta(thinking)] if thinking else []
        if kind == "redacted_thinking":
            self._item({"type": "redacted_thinking", "data": ""})
            self._private(block.data, _string(raw.get("data", "")))
        elif kind == "tool_use":
            block.call_id = _string(raw.get("id"), nonempty=True)
            block.name = _string(raw.get("name"), nonempty=True)
            if block.call_id in self.call_ids:
                raise _malformed()
            self.call_ids.add(block.call_id)
            block.start_input = _object(raw.get("input"))
            try:
                ToolCall(block.call_id, block.name, block.start_input)
                ToolCallItem(block.call_id)
            except (TypeError, ValueError):
                raise _malformed() from None
            self._item({"type": "tool_call", "call_id": block.call_id})
        return []

    def _delta(self, frame: dict[str, Any]) -> list[ProviderEvent]:
        block = self._block(frame.get("index"))
        raw = _object(frame.get("delta"))
        kind = _string(raw.get("type"), nonempty=True)
        if block.kind == "tool_use" and any(
            name in raw and raw[name] != expected
            for name, expected in (("id", block.call_id), ("name", block.name))
        ):
            raise _malformed()
        fields = {
            "text_delta": ("text", "text"),
            "thinking_delta": ("thinking", "thinking"),
            "signature_delta": ("thinking", "signature"),
            "input_json_delta": ("tool_use", "partial_json"),
        }
        if kind not in fields:
            if block.kind == "tool_use":
                # An unrecognized tool delta cannot establish complete arguments.
                raise _malformed()
            self.discard()
            return []
        expected, name = fields[kind]
        if block.kind not in _BLOCK_FIELDS:
            self.discard()
            return []
        if block.kind != expected:
            raise _malformed()
        if set(raw) - {"type", name}:
            self.discard()
        value = _string(raw.get(name))
        if kind == "text_delta":
            return self._text(block, value)
        if kind == "thinking_delta":
            self._private(block.thinking, value)
            return [ProviderReasoningDelta(value)] if value else []
        if kind == "signature_delta":
            self._private(block.signature, value)
        else:
            if block.start_input:
                raise _malformed()
            block.has_fragments = True
            block.fragments.append(value)
        return []

    def _stop_block(self, frame: dict[str, Any]) -> None:
        block = self._block(frame.get("index"))
        if block.kind == "tool_use":
            arguments = (
                _object(_json("".join(block.fragments)))
                if block.has_fragments
                else block.start_input
            )
            try:
                block.call = ToolCall(block.call_id, block.name, arguments)
            except (TypeError, ValueError):
                raise _malformed() from None
            block.fragments.clear()
            block.start_input = {}
        elif not self.discarded:
            try:
                if block.kind == "thinking":
                    MessagesThinkingItem(block.thinking.saved, block.signature.saved)
                elif block.kind == "redacted_thinking":
                    MessagesRedactedThinkingItem(block.data.saved)
            except (TypeError, ValueError):
                self.discard()
        block.stopped = True

    def _finish(self) -> Finish:
        if not self.ending or any(not block.stopped for block in self.blocks):
            raise _malformed()
        calls = tuple(block.call for block in self.blocks if block.call is not None)
        if self.reason is None:
            raise _malformed()
        if self.reason in {"max_tokens", "model_context_window_exceeded"}:
            raise ProviderError("Messages output was truncated")
        if self.reason == "refusal":
            raise ProviderError("Messages request was refused")
        if self.reason == "pause_turn":
            raise ProviderError("Messages server turn was paused")
        if self.reason not in {"end_turn", "stop_sequence", "tool_use"}:
            raise ProviderError("unsupported Messages stop reason")
        if bool(calls) != (self.reason == "tool_use"):
            raise _malformed()
        record = None
        if self.request.provider_id is not None:
            if self.discarded:
                record = self._record(ContinuationStatus.UNREPLAYABLE)
            else:
                items = []
                for block in self.blocks:
                    if block.kind == "thinking":
                        items.append(
                            MessagesThinkingItem(
                                block.thinking.saved, block.signature.saved
                            )
                        )
                    elif block.kind == "redacted_thinking":
                        items.append(MessagesRedactedThinkingItem(block.data.saved))
                    elif block.kind == "text" and block.text_length:
                        items.append(TextItem(None))
                    elif block.kind == "tool_use":
                        items.append(ToolCallItem(block.call_id))
                if items:
                    record = self._record(ContinuationStatus.REPLAYABLE, items)
        return Finish(self.reason, calls, record)

    def consume(self, frame: dict[str, Any]) -> list[ProviderEvent]:
        kind = frame["type"]
        if kind == "error":
            error = _object(frame.get("error"))
            error_type = _string(error.get("type"), nonempty=True)
            classification = {
                "authentication_error": AuthenticationError,
                "permission_error": AuthenticationError,
                "rate_limit_error": RateLimitError,
                "timeout_error": ProviderTimeoutError,
                "overloaded_error": TemporaryServerError,
                "api_error": TemporaryServerError,
            }.get(error_type, ProviderError)
            raise classification("Messages stream failed")
        if kind not in _FRAME_FIELDS:
            self.discard()
            return []
        if set(frame) - _FRAME_FIELDS[kind]:
            self.discard()
        if kind == "ping":
            return []
        if kind == "message_start":
            if self.started:
                raise _malformed()
            message = _object(frame.get("message"))
            _string(message.get("id"), nonempty=True)
            _string(message.get("model"), nonempty=True)
            if (
                message.get("type") != "message"
                or message.get("role") != "assistant"
                or message.get("content") != []
                or message.get("stop_reason") is not None
                or message.get("stop_sequence") is not None
            ):
                raise _malformed()
            if (
                set(message)
                - {
                    "id",
                    "type",
                    "role",
                    "content",
                    "model",
                    "stop_reason",
                    "stop_sequence",
                    "usage",
                    "container",
                }
                or message.get("container") is not None
            ):
                self.discard()
            self.started = True
            return []
        if not self.started:
            raise _malformed()
        if kind == "content_block_start":
            return self._start_block(frame)
        if kind == "content_block_delta":
            return self._delta(frame)
        if kind == "content_block_stop":
            self._stop_block(frame)
        elif kind == "message_delta":
            if any(not block.stopped for block in self.blocks):
                raise _malformed()
            self.ending = True
            delta = _object(frame.get("delta"))
            if set(delta) - {"stop_reason", "stop_sequence"}:
                self.discard()
            reason = delta.get("stop_reason")
            if reason is not None:
                reason = _string(reason, nonempty=True)
                if self.reason is not None and self.reason != reason:
                    raise _malformed()
                self.reason = reason
            if delta.get("stop_sequence") is not None:
                _string(delta["stop_sequence"])
        elif kind == "message_stop":
            return [self._finish()]
        return []


class AnthropicMessagesProvider:
    """Adapt generic history and compatible continuations to one Messages request."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        *,
        timeout: float = 30.0,
        max_retries: int = 1,
        retry_backoff: float = 1.0,
        sleep: SleepFn | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        thinking_level_map: Mapping[ReasoningEffort, str] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        for value, name in ((timeout, "timeout"), (retry_backoff, "retry_backoff")):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (name == "timeout" and value == 0)
            ):
                raise ValueError(f"{name} is invalid")
        self._endpoint = resolve_endpoint(base_url)
        self._thinking_level_map = _thinking_map(thinking_level_map)
        self._api_key = api_key
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    def __repr__(self) -> str:
        return f"AnthropicMessagesProvider(max_retries={self._max_retries})"

    async def aclose(self) -> None:
        """Close the owned HTTP client; repeated calls are safe."""
        await self._client.aclose()

    def _build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        if request.api is not ProviderApi.ANTHROPIC_MESSAGES:
            raise ProviderError("request API is not Messages")
        all_ids = [
            call.call_id for message in request.messages for call in message.tool_calls
        ]
        if len(set(all_ids)) != len(all_ids):
            raise ProviderError("history has invalid tool associations")
        ids = {value: value for value in all_ids if _ID.fullmatch(value)}
        reserved = set(ids.values())
        counter = 0
        for value in all_ids:
            if value in ids:
                continue
            while f"mica_tool_{counter}" in reserved:
                counter += 1
            ids[value] = f"mica_tool_{counter}"
            reserved.add(ids[value])
            counter += 1
        system = []
        messages = []
        records = iter(request.continuations)
        index = 0
        while index < len(request.messages):
            message = request.messages[index]
            index += 1
            if message.role is MessageRole.SYSTEM:
                if messages:
                    raise ProviderError("history has misplaced system messages")
                system.append({"type": "text", "text": message.content})
                continue
            if message.role is MessageRole.TOOL:
                raise ProviderError("history has invalid tool associations")
            if message.role is MessageRole.USER:
                messages.append(
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": message.content}],
                    }
                )
                continue
            encoded_calls = {
                call.call_id: {
                    "type": "tool_use",
                    "id": ids[call.call_id],
                    "name": call.name,
                    "input": to_mutable_json(call.arguments),
                }
                for call in message.tool_calls
            }
            record = next(records)
            compatible = (
                request.provider_id is not None
                and record is not None
                and record.status is ContinuationStatus.REPLAYABLE
                and record.provider_id == request.provider_id
                and record.model_id == request.model
                and record.api is request.api
                and continuation_size(record) <= MAX_TURN_CONTINUATION_BYTES
            )
            content = []
            if compatible:
                if any(
                    isinstance(
                        item, (MessagesThinkingItem, MessagesRedactedThinkingItem)
                    )
                    for item in record.items
                ) and any(
                    ids[call.call_id] != call.call_id for call in message.tool_calls
                ):
                    raise ProviderError("signed history requires unchanged tool IDs")
                for item in record.items:
                    if isinstance(item, MessagesThinkingItem):
                        content.append(
                            {
                                "type": "thinking",
                                "thinking": item.thinking,
                                "signature": item.signature,
                            }
                        )
                    elif isinstance(item, MessagesRedactedThinkingItem):
                        content.append({"type": "redacted_thinking", "data": item.data})
                    elif isinstance(item, TextItem):
                        content.append({"type": "text", "text": message.content})
                    elif isinstance(item, ToolCallItem):
                        content.append(encoded_calls[item.call_id])
            else:
                if message.content or not message.tool_calls:
                    content.append({"type": "text", "text": message.content})
                content.extend(encoded_calls.values())
            messages.append({"role": "assistant", "content": content})
            if not encoded_calls:
                continue
            results = []
            remaining = set(encoded_calls)
            while index < len(request.messages):
                result = request.messages[index]
                if result.role is not MessageRole.TOOL:
                    break
                if result.tool_call_id not in remaining:
                    raise ProviderError("history has invalid tool associations")
                remaining.remove(result.tool_call_id)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": ids[result.tool_call_id],
                        "content": result.content,
                    }
                )
                index += 1
            if remaining:
                raise ProviderError("history has missing tool results")
            if (
                index < len(request.messages)
                and request.messages[index].role is MessageRole.USER
            ):
                results.append(
                    {"type": "text", "text": request.messages[index].content}
                )
                index += 1
            messages.append({"role": "user", "content": results})
        payload = {
            "model": request.model,
            "messages": messages,
            "stream": True,
            "max_tokens": request.max_tokens or DEFAULT_MAX_TOKENS,
        }
        if system:
            payload["system"] = system
        if request.tools:
            payload["tools"] = [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": to_mutable_json(spec.input_schema),
                }
                for spec in request.tools
            ]
        if request.reasoning_effort is ReasoningEffort.OFF:
            payload["thinking"] = {"type": "disabled"}
        else:
            try:
                mapping = (
                    _thinking_map(request.thinking_level_map)
                    if request.thinking_level_map is not None
                    else self._thinking_level_map
                )
            except (TypeError, ValueError):
                raise ProviderError("unsupported Messages reasoning effort") from None
            if request.reasoning_effort not in mapping:
                raise ProviderError("unsupported Messages reasoning effort")
            payload["thinking"] = {"type": "adaptive", "display": "summarized"}
            payload["output_config"] = {"effort": mapping[request.reasoning_effort]}
        try:
            _validate_json(payload)
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError, RecursionError, ProviderError):
            raise ProviderError("history cannot be encoded as Messages") from None
        return payload

    @staticmethod
    def _status_error(status: int) -> ProviderError:
        if status in (401, 403):
            return AuthenticationError("authentication failed")
        if status == 429:
            return RateLimitError("rate limit exceeded")
        if status == 408:
            return ProviderTimeoutError("request timed out")
        if status in (500, 502, 503, 504, 529):
            return TemporaryServerError("temporary server error")
        return ProviderError("unexpected HTTP status")

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        """Emit visible deltas and a single genuine terminal, preserving cancellation."""
        try:
            payload = self._build_payload(request)
        except (UnicodeError, RecursionError):
            yield ProviderErrorEvent(
                ProviderError("history cannot be encoded as Messages")
            )
            return
        except ProviderError as error:
            yield ProviderErrorEvent(error)
            return
        for attempt in range(self._max_retries + 1):
            established = False
            try:
                async with self._client.stream(
                    "POST",
                    self._endpoint,
                    json=payload,
                    headers={
                        "x-api-key": self._api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                ) as response:
                    if not 200 <= response.status_code < 300:
                        error = self._status_error(response.status_code)
                    else:
                        established = True
                        step = _MessagesStep(request)
                        async for frame in _sse_frames(response):
                            for event in step.consume(frame):
                                yield event
                                if isinstance(event, Finish):
                                    return
                        raise _malformed()
            except ProviderError as error:
                yield ProviderErrorEvent(error)
                return
            except httpx.TimeoutException:
                error = (
                    _malformed()
                    if established
                    else ProviderTimeoutError("request timed out")
                )
            except httpx.TransportError:
                error = (
                    _malformed()
                    if established
                    else TemporaryServerError("transport error")
                )
            except UnicodeError:
                error = _malformed()
            if (
                not established
                and attempt < self._max_retries
                and isinstance(error, (ProviderTimeoutError, TemporaryServerError))
            ):
                await self._sleep(self._retry_backoff)
                continue
            yield ProviderErrorEvent(error)
            return
