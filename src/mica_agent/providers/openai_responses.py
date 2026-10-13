"""Stateless Responses protocol adaptation with bounded private replay data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import httpx

from ..continuation_contracts import (
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
DEFAULT_THINKING_LEVEL_MAP = MappingProxyType(
    {
        effort: effort.value
        for effort in ReasoningEffort
        if effort is not ReasoningEffort.OFF
    }
)
SleepFn = Callable[[float], Awaitable[None]]


def resolve_endpoint(base_url: str) -> str:
    """Append /responses to a base path, preserving an already complete endpoint."""
    try:
        url = httpx.URL(base_url)
    except (httpx.InvalidURL, TypeError):
        raise ValueError("base_url must be a valid HTTP URL") from None
    if url.scheme not in ("http", "https") or not url.host:
        raise ValueError("base_url must be a valid HTTP URL")
    path = url.path.rstrip("/")
    if not path.endswith("/responses"):
        path += "/responses"
    return str(url.copy_with(path=path))


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
        or not target.strip()
        or not target.isprintable()
        for key, target in value.items()
    ):
        raise ValueError("thinking_level_map is invalid")
    return MappingProxyType(dict(value))


def _malformed() -> MalformedResponseError:
    return MalformedResponseError("invalid Responses stream")


def _string(value: object, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise _malformed()
    # Reject unpaired surrogates before hashing, encoding or emitting them.
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _malformed() from None
    return value


def _index(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _malformed()
    return value


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _malformed()
    return value


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
        return json.loads(
            value, parse_constant=reject_constant, object_pairs_hook=unique_pairs
        )
    except (ValueError, RecursionError):
        raise _malformed() from None


async def _sse_frames(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    data: list[str] = []
    event: str | None = None
    async for line in response.aiter_lines():
        if line == "":
            if data:
                payload = "\n".join(data)
                if payload == "[DONE]":
                    raise _malformed()
                parsed = _object(_json(payload))
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
    # SSE dispatch requires a frame boundary, even for a terminal event.
    raise _malformed()


class _StreamText:
    """Verify snapshots using digests after private text has been released."""

    def __init__(self, *, save: bool = False) -> None:
        self.length = 0
        self.digest = hashlib.sha256()
        self.done = False
        self.saved: str | None = "" if save else None

    def delta(self, value: str) -> str:
        if self.done:
            raise _malformed()
        self.digest.update(value.encode("utf-8"))
        self.length += len(value)
        if self.saved is not None:
            self.saved += value
        return value

    def complete(self, value: str) -> str:
        if (
            len(value) < self.length
            or hashlib.sha256(value[: self.length].encode("utf-8")).digest()
            != self.digest.digest()
            or (self.done and len(value) != self.length)
        ):
            raise _malformed()
        suffix = value[self.length :]
        if not self.done:
            self.delta(suffix)
        self.done = True
        return suffix


@dataclass(repr=False)
class _Output:
    item_id: str
    kind: str | None = None
    metadata: dict[str, str | None] = field(default_factory=dict)
    phase_digest: bytes | None = None
    parts: dict[tuple[str, int], _StreamText] = field(default_factory=dict)
    arguments: str = ""
    arguments_delta: bool = False
    arguments_done: bool = False
    done: bool = False
    encrypted: str | None = None
    encrypted_digest: bytes | None = None


_ITEM_FIELDS = {
    "message": {"type", "id", "status", "role", "content", "phase"},
    "reasoning": {"type", "id", "status", "summary", "encrypted_content", "content"},
    "function_call": {"type", "id", "status", "call_id", "name", "arguments"},
}
_PART_FIELDS = {
    "output_text": {"type", "text", "annotations", "logprobs"},
    "refusal": {"type", "refusal"},
    "summary_text": {"type", "text"},
    "reasoning_text": {"type", "text"},
}


class _ResponseStep:
    """Own one stream's associations, visible progress and sticky replay budget."""

    def __init__(self, request: ProviderRequest) -> None:
        self.request = request
        self.outputs: dict[int, _Output] = {}
        self.identities: dict[str, int] = {}
        self.calls: dict[str, int] = {}
        self.discarded = request.provider_id is None
        self.response_id: str | None = None

    def discard(self) -> None:
        self.discarded = True
        for output in self.outputs.values():
            output.encrypted = None
            for part in output.parts.values():
                part.saved = None

    def output(
        self, index: object, item_id: object, kind: str | None = None
    ) -> _Output:
        index = _index(index)
        item_id = _string(item_id, nonempty=True)
        if item_id in self.identities and self.identities[item_id] != index:
            raise _malformed()
        output = self.outputs.get(index)
        if output is None:
            output = self.outputs[index] = _Output(item_id, kind)
            self.identities[item_id] = index
        elif output.item_id != item_id or (
            kind is not None and output.kind is not None and output.kind != kind
        ):
            raise _malformed()
        if kind is not None:
            output.kind = kind
        return output

    def _metadata(self, output: _Output, raw: dict[str, Any]) -> None:
        for name in ("role", "call_id", "name", "phase"):
            if name not in raw:
                continue
            value = raw[name]
            if name != "phase" or value is not None:
                value = _string(value, nonempty=name != "phase")
            if name == "phase":
                digest = hashlib.sha256(
                    json.dumps(value, ensure_ascii=False).encode("utf-8")
                ).digest()
                if output.phase_digest is not None and output.phase_digest != digest:
                    raise _malformed()
                output.phase_digest = digest
                if value not in (None, "commentary", "final_answer"):
                    self.discard()
                    continue
            if name in output.metadata and output.metadata[name] != value:
                raise _malformed()
            output.metadata[name] = value
        if output.kind == "message" and output.metadata.get("role") != "assistant":
            raise _malformed()
        if output.kind == "function_call":
            call_id = output.metadata.get("call_id")
            if not call_id or not output.metadata.get("name"):
                raise _malformed()
            index = self.identities[output.item_id]
            if call_id in self.calls and self.calls[call_id] != index:
                raise _malformed()
            self.calls[call_id] = index

    def part(self, output: _Output, kind: str, index: object) -> _StreamText:
        index = _index(index)
        key = (kind, index)
        namespace = "summary" if kind == "summary_text" else "content"
        if any(
            other_index == index
            and other_kind != kind
            and ("summary" if other_kind == "summary_text" else "content") == namespace
            for other_kind, other_index in output.parts
        ):
            raise _malformed()
        if key not in output.parts:
            if output.done:
                raise _malformed()
            output.parts[key] = _StreamText(
                save=kind == "summary_text" and not self.discarded
            )
        if kind == "refusal":
            self.discard()
        return output.parts[key]

    def _part_snapshot(
        self,
        output: _Output,
        index: int,
        raw: object,
        *,
        final: bool,
        summary: bool = False,
    ) -> list[ProviderEvent]:
        raw = _object(raw)
        kind = _string(raw.get("type"), nonempty=True)
        if kind not in _PART_FIELDS:
            if any(
                part_index == index and (part_kind == "summary_text") == summary
                for part_kind, part_index in output.parts
            ):
                raise _malformed()
            self.discard()
            return []
        if (output.kind == "message" and kind not in ("output_text", "refusal")) or (
            output.kind == "reasoning"
            and kind != ("summary_text" if summary else "reasoning_text")
        ):
            raise _malformed()
        if (
            set(raw) - _PART_FIELDS[kind]
            or raw.get("annotations")
            or raw.get("logprobs")
        ):
            self.discard()
        value = _string(raw.get("refusal" if kind == "refusal" else "text"))
        part = self.part(output, kind, index)
        if final:
            text = part.complete(value)
        else:
            # Initial snapshots may seed text, but must match later deltas/done.
            if part.length or part.done:
                raise _malformed()
            text = part.delta(value)
        self.check_budget()
        return self._visible(kind, text)

    @staticmethod
    def _visible(kind: str, text: str) -> list[ProviderEvent]:
        if not text:
            return []
        return [
            ProviderReasoningDelta(text)
            if kind in ("summary_text", "reasoning_text")
            else TextDelta(text)
        ]

    def _arguments(self, output: _Output, value: str) -> None:
        if (
            output.arguments_done or output.arguments_delta
        ) and output.arguments != value:
            raise _malformed()
        output.arguments = value
        output.arguments_done = True
        self.tool_call(output)

    @staticmethod
    def tool_call(output: _Output) -> ToolCall:
        try:
            call = ToolCall(
                output.metadata["call_id"],
                output.metadata["name"],
                _object(_json(output.arguments)),
            )
            json.dumps(
                to_mutable_json(call.arguments), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            ToolCallItem(call.call_id)
            return call
        except (KeyError, TypeError, ValueError, RecursionError):
            raise _malformed() from None

    def snapshot(self, index: int, raw: object, *, final: bool) -> list[ProviderEvent]:
        raw = _object(raw)
        kind = _string(raw.get("type"), nonempty=True)
        output = self.output(index, raw.get("id"), kind)
        if not final and output.done:
            raise _malformed()
        if kind not in _ITEM_FIELDS:
            self.discard()
            output.done = final
            return []
        if set(raw) - _ITEM_FIELDS[kind]:
            self.discard()
        self._metadata(output, raw)
        if final and raw.get("status") not in (None, "completed"):
            raise _malformed()
        events: list[ProviderEvent] = []
        if kind == "function_call":
            value = _string(raw.get("arguments"))
            if final:
                self._arguments(output, value)
            elif value:
                if output.arguments:
                    raise _malformed()
                output.arguments = value
                output.arguments_delta = True
        else:
            fields = ("content",) if kind == "message" else ("summary", "content")
            for name in fields:
                parts = raw.get(name, [])
                if not isinstance(parts, list):
                    raise _malformed()
                namespace = "summary" if name == "summary" else "content"
                if final and any(
                    part_index >= len(parts)
                    for part_kind, part_index in output.parts
                    if ("summary" if part_kind == "summary_text" else "content")
                    == namespace
                ):
                    raise _malformed()
                for part_index, part in enumerate(parts):
                    if (
                        name == "summary"
                        and _object(part).get("type") != "summary_text"
                    ):
                        if ("summary_text", part_index) in output.parts:
                            raise _malformed()
                        self.discard()
                        continue
                    events.extend(
                        self._part_snapshot(
                            output,
                            part_index,
                            part,
                            final=final,
                            summary=name == "summary",
                        )
                    )
            if kind == "reasoning" and final:
                encrypted = raw.get("encrypted_content")
                if encrypted is not None:
                    encrypted = _string(encrypted)
                if encrypted:
                    digest = hashlib.sha256(encrypted.encode("utf-8")).digest()
                    if (
                        output.encrypted_digest is not None
                        and output.encrypted_digest != digest
                    ):
                        raise _malformed()
                    output.encrypted_digest = digest
                if encrypted and not self.discarded:
                    try:
                        ResponsesReasoningItem(output.item_id, (), encrypted)
                    except (TypeError, ValueError):
                        self.discard()
                    else:
                        output.encrypted = encrypted
        output.done = final
        self.check_budget()
        return events

    def _items(self, *, provisional: bool) -> tuple:
        items = []
        text_parts = 0
        for index in sorted(self.outputs):
            output = self.outputs[index]
            if output.kind == "reasoning":
                # A done item may omit the ciphertext later supplied by completed.
                if not provisional and output.encrypted is None:
                    self.discard()
                    return ()
                summary = tuple(
                    part.saved or ""
                    for (kind, _), part in sorted(
                        output.parts.items(), key=lambda entry: entry[0][1]
                    )
                    if kind == "summary_text"
                )
                items.append(
                    ResponsesReasoningItem(
                        output.item_id, summary, output.encrypted or "x"
                    )
                )
            elif output.kind == "message":
                parts = [
                    part
                    for (kind, _), part in output.parts.items()
                    if kind == "output_text"
                ]
                text_parts += len(parts)
                if any(part.length for part in parts):
                    phase = output.metadata.get("phase")
                    items.append(
                        TextItem(TextPhase(phase) if phase is not None else None)
                    )
            elif output.kind == "function_call" and output.metadata.get("call_id"):
                items.append(ToolCallItem(output.metadata["call_id"]))
            elif output.kind is not None:
                self.discard()
                return ()
        if text_parts > 1 or sum(isinstance(item, TextItem) for item in items) > 1:
            self.discard()
            return ()
        if not provisional and any(not output.done for output in self.outputs.values()):
            raise _malformed()
        return tuple(items)

    def record(self, *, provisional: bool = False) -> ModelStepContinuation | None:
        if self.request.provider_id is None:
            return None
        if not self.discarded:
            try:
                items = self._items(provisional=provisional)
            except (TypeError, ValueError):
                self.discard()
            else:
                if not self.discarded:
                    if not items:
                        return None
                    return ModelStepContinuation(
                        self.request.provider_id,
                        self.request.model,
                        self.request.api,
                        ContinuationStatus.REPLAYABLE,
                        items,
                    )
        return ModelStepContinuation(
            self.request.provider_id,
            self.request.model,
            self.request.api,
            ContinuationStatus.UNREPLAYABLE,
        )

    def check_budget(self) -> None:
        if not self.discarded:
            record = self.record(provisional=True)
            if (
                record is not None
                and continuation_size(record) > MAX_TURN_CONTINUATION_BYTES
            ):
                self.discard()

    def check_event_fields(self, event: dict[str, Any], allowed: set[str]) -> None:
        wrappers = {"type", "sequence_number", "response_id", "obfuscation"}
        if set(event) - wrappers - allowed:
            self.discard()

    def consume(self, event: dict[str, Any]) -> list[ProviderEvent]:
        kind = event["type"]
        response = event.get("response")
        response_id = event.get("response_id")
        if isinstance(response, dict) and "id" in response:
            response_id = response["id"]
        if response_id is not None:
            response_id = _string(response_id, nonempty=True)
            if self.response_id is not None and response_id != self.response_id:
                raise _malformed()
            self.response_id = response_id
        if kind in ("response.failed", "response.incomplete", "error"):
            if kind == "response.incomplete":
                raise ProviderError("Responses output is incomplete")
            raw_error = event if kind == "error" else _object(response).get("error")
            code = raw_error.get("code") if isinstance(raw_error, dict) else None
            raise _code_error(code)
        if kind == "response.completed":
            self.check_event_fields(event, {"response"})
            response = _object(response)
            if response.get("status") != "completed" or not isinstance(
                response.get("output"), list
            ):
                raise _malformed()
            raw_outputs = response["output"]
            if any(index >= len(raw_outputs) for index in self.outputs):
                raise _malformed()
            events: list[ProviderEvent] = []
            for index, raw in enumerate(raw_outputs):
                events.extend(self.snapshot(index, raw, final=True))
            calls = tuple(
                self.tool_call(self.outputs[index])
                for index in sorted(self.outputs)
                if self.outputs[index].kind == "function_call"
            )
            events.append(Finish("completed", calls, self.record()))
            return events
        if kind in ("response.output_item.added", "response.output_item.done"):
            self.check_event_fields(event, {"output_index", "item"})
            return self.snapshot(
                _index(event.get("output_index")),
                event.get("item"),
                final=kind.endswith(".done"),
            )
        text_events = {
            "response.output_text": ("message", "output_text", "content_index", "text"),
            "response.refusal": ("message", "refusal", "content_index", "refusal"),
            "response.reasoning_summary_text": (
                "reasoning",
                "summary_text",
                "summary_index",
                "text",
            ),
            "response.reasoning_text": (
                "reasoning",
                "reasoning_text",
                "content_index",
                "text",
            ),
        }
        prefix, _, ending = kind.rpartition(".")
        if prefix in text_events and ending in ("delta", "done"):
            item_kind, part_kind, index_field, value_field = text_events[prefix]
            self.check_event_fields(
                event,
                {
                    "output_index",
                    "item_id",
                    index_field,
                    "delta",
                    value_field,
                    "logprobs",
                },
            )
            output = self.output(
                event.get("output_index"), event.get("item_id"), item_kind
            )
            part = self.part(output, part_kind, event.get(index_field))
            value = _string(event.get("delta" if ending == "delta" else value_field))
            text = part.delta(value) if ending == "delta" else part.complete(value)
            self.check_budget()
            return self._visible(part_kind, text)
        if prefix == "response.function_call_arguments" and ending in ("delta", "done"):
            self.check_event_fields(
                event,
                {"output_index", "item_id", "delta", "arguments", "name", "call_id"},
            )
            output = self.output(
                event.get("output_index"), event.get("item_id"), "function_call"
            )
            if "name" in event or "call_id" in event:
                self._metadata(output, event)
            value = _string(event.get("delta" if ending == "delta" else "arguments"))
            if ending == "done":
                self._arguments(output, value)
            else:
                if output.done or output.arguments_done:
                    raise _malformed()
                output.arguments += value
                output.arguments_delta = True
            return []
        if prefix in (
            "response.content_part",
            "response.reasoning_summary_part",
        ) and ending in ("added", "done"):
            item_kind = "message" if prefix == "response.content_part" else "reasoning"
            output = self.output(
                event.get("output_index"), event.get("item_id"), item_kind
            )
            if event.get("status") == "incomplete":
                raise _malformed()
            index_field = "content_index" if item_kind == "message" else "summary_index"
            self.check_event_fields(
                event, {"output_index", "item_id", index_field, "part", "status"}
            )
            return self._part_snapshot(
                output,
                _index(event.get(index_field)),
                event.get("part"),
                final=ending == "done",
                summary=prefix == "response.reasoning_summary_part",
            )
        if kind not in ("response.created", "response.in_progress", "response.queued"):
            # Unknown semantic events cannot silently yield a replayable step.
            if "output_index" in event and "item_id" in event:
                self.output(event["output_index"], event["item_id"])
            self.discard()
        return []


def _code_error(code: object) -> ProviderError:
    if isinstance(code, str):
        if code in (
            "invalid_api_key",
            "authentication_error",
            "permission_denied",
            "invalid_authentication",
        ):
            return AuthenticationError("authentication failed")
        if code in ("rate_limit_exceeded", "rate_limit_error", "insufficient_quota"):
            return RateLimitError("rate limit exceeded")
        if code in ("timeout", "request_timeout", "timeout_error"):
            return ProviderTimeoutError("request timed out")
        if code in ("server_error", "internal_server_error", "service_unavailable"):
            return TemporaryServerError("temporary server error")
    return ProviderError("Responses request failed")


class OpenAIResponsesProvider:
    """API-key Responses provider; callers close the provider when no longer needed.

    Only response.completed commits a model step. Established streams are never
    retried, and private replay data has a sticky compact-JSON byte budget.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        *,
        timeout: httpx.Timeout | float = 30.0,
        max_retries: int = 1,
        retry_backoff: float = 1.0,
        sleep: SleepFn | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        thinking_level_map: Mapping[ReasoningEffort, str] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty, non-blank string")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if (
            not isinstance(retry_backoff, (int, float))
            or not math.isfinite(retry_backoff)
            or retry_backoff < 0
        ):
            raise ValueError("retry_backoff must be a non-negative finite number")
        # URL userinfo would make httpx override the explicit Bearer header.
        self._endpoint = str(
            httpx.URL(resolve_endpoint(base_url)).copy_with(username="", password="")
        )
        self._api_key = api_key
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._thinking_level_map = _thinking_map(thinking_level_map)
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    def __repr__(self) -> str:
        return f"OpenAIResponsesProvider(max_retries={self._max_retries})"

    async def aclose(self) -> None:
        """Release the owned HTTP client; repeated calls are safe."""
        await self._client.aclose()

    def _build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        if request.api is not ProviderApi.OPENAI_RESPONSES:
            raise ProviderError("request API is not Responses")
        inputs: list[dict[str, Any]] = []
        records = iter(request.continuations)
        calls: set[str] = set()
        results: set[str] = set()
        for message in request.messages:
            if message.role is MessageRole.TOOL:
                if message.tool_call_id not in calls or message.tool_call_id in results:
                    raise ProviderError("history has invalid tool associations")
                results.add(message.tool_call_id)
                inputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content,
                    }
                )
                continue
            if message.role is not MessageRole.ASSISTANT:
                inputs.append({"role": message.role.value, "content": message.content})
                continue
            encoded_calls = {}
            for call in message.tool_calls:
                if call.call_id in calls:
                    raise ProviderError("history has invalid tool associations")
                calls.add(call.call_id)
                try:
                    ToolCallItem(call.call_id)
                    encoded_calls[call.call_id] = {
                        "type": "function_call",
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": json.dumps(
                            to_mutable_json(call.arguments),
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ),
                    }
                except (TypeError, ValueError, UnicodeError):
                    raise ProviderError(
                        "history cannot be encoded as Responses"
                    ) from None
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
            if compatible:
                for item in record.items:
                    if isinstance(item, ResponsesReasoningItem):
                        inputs.append(
                            {
                                "type": "reasoning",
                                "id": item.id,
                                "summary": [
                                    {"type": "summary_text", "text": text}
                                    for text in item.summary
                                ],
                                "encrypted_content": item.encrypted_content,
                            }
                        )
                    elif isinstance(item, TextItem):
                        inputs.append(
                            {
                                "role": "assistant",
                                "content": message.content,
                                **(
                                    {"phase": item.phase.value}
                                    if item.phase is not None
                                    else {}
                                ),
                            }
                        )
                    elif isinstance(item, ToolCallItem):
                        inputs.append(encoded_calls[item.call_id])
            else:
                if message.content or not message.tool_calls:
                    inputs.append({"role": "assistant", "content": message.content})
                inputs.extend(encoded_calls.values())
        payload = {
            "model": request.model,
            "input": inputs,
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": request.max_tokens
            if request.max_tokens is not None
            else DEFAULT_MAX_TOKENS,
        }
        if request.reasoning_effort is not ReasoningEffort.OFF:
            mapping = (
                self._thinking_level_map
                if request.thinking_level_map is None
                else _thinking_map(request.thinking_level_map)
            )
            if request.reasoning_effort not in mapping:
                raise ProviderError("unsupported Responses reasoning effort")
            payload["reasoning"] = {
                "effort": mapping[request.reasoning_effort],
                "summary": "auto",
            }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": to_mutable_json(spec.input_schema),
                    "strict": False,
                }
                for spec in request.tools
            ]
        return payload

    @staticmethod
    def _status_error(status: int) -> ProviderError:
        if status in (401, 403):
            return AuthenticationError("authentication failed")
        if status == 429:
            return RateLimitError("rate limit exceeded")
        if status == 408:
            return ProviderTimeoutError("request timed out")
        if status in (500, 502, 503, 504):
            return TemporaryServerError("temporary server error")
        return ProviderError("unexpected HTTP status")

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        """Perform one model step, emitting exactly one normal or error terminal."""
        try:
            payload = self._build_payload(request)
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
                    headers={"Authorization": f"Bearer {self._api_key}"},
                ) as response:
                    if not 200 <= response.status_code < 300:
                        error = self._status_error(response.status_code)
                    else:
                        established = True
                        step = _ResponseStep(request)
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
