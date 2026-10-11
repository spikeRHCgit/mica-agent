"""OpenAI-compatible Chat Completions streaming provider.

Protocol adaptation, SSE parsing, error classification, and bounded retries all stay
inside this module; the outside sees only
:data:`~mica_agent.providers.base.ProviderEvent` and never an ``httpx.Response`` or any
provider-private object.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any

import httpx

from ..continuation_contracts import (
    MAX_TURN_CONTINUATION_BYTES,
    ChatReasoningItem,
    ContinuationStatus,
    ModelStepContinuation,
    ProviderApi,
    ReasoningDetail,
    ReasoningDetailType,
    continuation_size,
)
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

SleepFn = Callable[[float], Awaitable[None]]
DEFAULT_MAX_TOKENS = 4096
DEFAULT_THINKING_LEVEL_MAP = MappingProxyType(
    {
        ReasoningEffort.MINIMAL: "minimal",
        ReasoningEffort.LOW: "low",
        ReasoningEffort.MEDIUM: "medium",
        ReasoningEffort.HIGH: "high",
        ReasoningEffort.XHIGH: "xhigh",
        ReasoningEffort.MAX: "max",
    }
)

_DETAIL_CONTENT_FIELDS = {
    ReasoningDetailType.SUMMARY: ("summary",),
    ReasoningDetailType.ENCRYPTED: ("data",),
    ReasoningDetailType.TEXT: ("text", "signature"),
}


def _parse_reasoning_detail(value: object) -> ReasoningDetail:
    if not isinstance(value, dict):
        raise TypeError("reasoning detail must be an object")
    kind = ReasoningDetailType(value.get("type"))
    allowed = {"type", "id", "format", "index", *_DETAIL_CONTENT_FIELDS[kind]}
    if set(value) - allowed:
        raise ValueError("reasoning detail contains unexpected fields")
    return ReasoningDetail(**{**value, "type": kind})


def _encode_reasoning_detail(detail: ReasoningDetail) -> dict[str, Any]:
    return {
        "type": detail.type.value,
        **{
            name: getattr(detail, name)
            for name in ("id", "format", "index", *_DETAIL_CONTENT_FIELDS[detail.type])
            if getattr(detail, name) is not None
        },
    }


class _ChatContinuationAccumulator:
    """Bound replay data during streaming, independently of visible reasoning."""

    def __init__(self, request: ProviderRequest, channel: str | None) -> None:
        self.request = request
        self.enabled = (
            channel is not None
            and request.provider_id is not None
            and request.api is ProviderApi.OPENAI_COMPLETIONS
        )
        self.reasoning_content: str | None = None
        self.reasoning: str | None = None
        self.details: tuple[ReasoningDetail, ...] = ()
        self.discarded = False

    def discard(self) -> None:
        self.discarded = True
        self.reasoning_content = None
        self.reasoning = None
        self.details = ()

    def record(self) -> ModelStepContinuation | None:
        if not self.enabled:
            return None
        if self.discarded:
            return ModelStepContinuation(
                self.request.provider_id,
                self.request.model,
                self.request.api,
                ContinuationStatus.UNREPLAYABLE,
            )
        if not (self.reasoning_content or self.reasoning or self.details):
            return None
        return ModelStepContinuation(
            self.request.provider_id,
            self.request.model,
            self.request.api,
            ContinuationStatus.REPLAYABLE,
            (ChatReasoningItem(self.reasoning_content, self.reasoning, self.details),),
        )

    def add(self, parsed: dict[str, Any], channel: str | None) -> tuple[str, ...]:
        content = parsed["reasoning_content"]
        reasoning = parsed["reasoning"] if channel == "openrouter" else None
        details: list[ReasoningDetail] = []
        if channel == "openrouter" and parsed["reasoning_details"] is not None:
            raw_details = parsed["reasoning_details"]
            if not isinstance(raw_details, list):
                if self.enabled:
                    self.discard()
            else:
                for value in raw_details:
                    try:
                        detail = _parse_reasoning_detail(value)
                    except (TypeError, ValueError):
                        if self.enabled:
                            self.discard()
                        continue
                    details.append(detail)
        if self.enabled and not self.discarded:
            if content is not None:
                self.reasoning_content = (self.reasoning_content or "") + content
            if reasoning is not None:
                self.reasoning = (self.reasoning or "") + reasoning
            self.details += tuple(details)
            record = self.record()
            if (
                record is not None
                and continuation_size(record) > MAX_TURN_CONTINUATION_BYTES
            ):
                self.discard()
        if content is not None:
            return (content,) if content else ()
        if reasoning is not None:
            return (reasoning,) if reasoning else ()
        return tuple(
            text for detail in details for text in (detail.summary, detail.text) if text
        )


def _validated_thinking_level_map(
    value: Mapping[ReasoningEffort, str] | None,
) -> Mapping[ReasoningEffort, str]:
    if value is None:
        return DEFAULT_THINKING_LEVEL_MAP
    if not isinstance(value, Mapping):
        raise TypeError("thinking_level_map must be a mapping or None")
    if not value:
        return DEFAULT_THINKING_LEVEL_MAP
    copied: dict[ReasoningEffort, str] = {}
    for effort, target in value.items():
        if not isinstance(effort, ReasoningEffort) or effort is ReasoningEffort.OFF:
            raise TypeError("thinking_level_map contains an invalid effort")
        if (
            not isinstance(target, str)
            or not target.strip()
            or not target.isprintable()
        ):
            raise ValueError("thinking_level_map contains an invalid target")
        copied[effort] = target
    return MappingProxyType(copied)


def resolve_endpoint(base_url: str) -> str:
    """Resolve a user-supplied URL to a Chat Completions endpoint.

    Convention:

    - A URL path already ending in ``/chat/completions`` is a complete endpoint and is
      returned as-is (only trailing slashes are removed).
    - Otherwise the URL is treated as a base URL and ``/chat/completions`` is appended
      to its path.

    This convention guarantees ``/chat/completions/chat/completions`` is never
    produced.
    """
    url = httpx.URL(base_url)
    path = url.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return str(url).rstrip("/")
    return str(url.copy_with(path=f"{path}/chat/completions"))


class OpenAICompatibleProvider:
    """Production provider over the OpenAI Chat Completions streaming protocol.

    ``base_url`` may be a complete endpoint that already includes
    ``/chat/completions``, or a base URL whose endpoint is appended; see
    :func:`resolve_endpoint` for the URL rules. The API key enters only the request
    headers, never ``repr``, exception messages, or events.

    ``continuation_channel`` explicitly enables DeepSeek or OpenRouter replay
    encoding. It defaults to disabled and never supplies the request's source ID.

    Retries: by default only ``ProviderTimeoutError`` and ``TemporaryServerError`` are
    retried, and only while the event stream has produced no text yet, up to
    ``max_retries`` times; ``RateLimitError`` is never retried automatically (that
    decision belongs to the caller). A transport interruption after the HTTP response
    was established is treated as a malformed response (``MalformedResponseError``): it
    is not retried and the request is not sent again.
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
        continuation_channel: str | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty, non-blank string")
        if (
            not isinstance(max_retries, int)
            or isinstance(max_retries, bool)
            or max_retries < 0
        ):
            raise ValueError("max_retries must be a non-negative integer")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be a non-negative number")
        if continuation_channel not in (None, "deepseek", "openrouter"):
            raise ValueError("continuation_channel is invalid")
        self._continuation_channel = continuation_channel
        self._api_key = api_key
        self._endpoint = resolve_endpoint(base_url)
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        self._thinking_level_map = _validated_thinking_level_map(thinking_level_map)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport
            if transport is not None
            else httpx.AsyncHTTPTransport(),
        )

    def __repr__(self) -> str:
        # Never includes the API key.
        return (
            f"OpenAICompatibleProvider(endpoint={self._endpoint!r}, "
            f"max_retries={self._max_retries})"
        )

    async def aclose(self) -> None:
        """Close the internal HTTP client; idempotent and safe once no longer needed."""
        await self._client.aclose()

    def _build_payload(self, request: ProviderRequest) -> dict[str, Any]:
        """Convert a provider-agnostic request into a Chat Completions JSON payload."""
        messages: list[dict[str, Any]] = []
        continuations = iter(request.continuations)
        for message in request.messages:
            item: dict[str, Any] = {
                "role": message.role.value,
                "content": message.content,
            }
            if message.role.value == "assistant" and message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                to_mutable_json(call.arguments),
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                    for call in message.tool_calls
                ]
            elif message.role.value == "tool":
                item["tool_call_id"] = message.tool_call_id
            if message.role.value == "assistant":
                record = next(continuations)
                if (
                    self._continuation_channel is not None
                    and request.provider_id is not None
                    and request.api is ProviderApi.OPENAI_COMPLETIONS
                    and record is not None
                    and record.status is ContinuationStatus.REPLAYABLE
                    and record.provider_id == request.provider_id
                    and record.model_id == request.model
                    and record.api is request.api
                ):
                    reasoning_item = record.items[0]
                    assert isinstance(reasoning_item, ChatReasoningItem)
                    if self._continuation_channel == "deepseek":
                        if (
                            request.tools
                            and reasoning_item.reasoning_content is not None
                        ):
                            item["reasoning_content"] = reasoning_item.reasoning_content
                    else:
                        for name in ("reasoning_content", "reasoning"):
                            value = getattr(reasoning_item, name)
                            if value:
                                item[name] = value
                        if reasoning_item.reasoning_details:
                            item["reasoning_details"] = [
                                _encode_reasoning_detail(detail)
                                for detail in reasoning_item.reasoning_details
                            ]
            messages.append(item)

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "stream": True,
            "max_tokens": request.max_tokens
            if request.max_tokens is not None
            else DEFAULT_MAX_TOKENS,
        }
        if request.reasoning_effort is not ReasoningEffort.OFF:
            payload["reasoning_effort"] = (
                self._thinking_level_map
                if request.thinking_level_map is None
                else request.thinking_level_map
            )[request.reasoning_effort]
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": to_mutable_json(spec.input_schema),
                    },
                }
                for spec in request.tools
            ]
        return payload

    @staticmethod
    def _map_status_error(status: int) -> ProviderError:
        if status in (401, 403):
            return AuthenticationError("authentication failed")
        if status == 408:
            return ProviderTimeoutError("request timed out")
        if status == 429:
            return RateLimitError("rate limit exceeded")
        if status in (500, 502, 503, 504):
            return TemporaryServerError("temporary server error")
        return ProviderError(f"unexpected HTTP status {status}")

    @staticmethod
    def _should_retry(
        error: ProviderError,
        attempt: int,
        max_retries: int,
        produced: bool,
    ) -> bool:
        """Retry only transient failures that produced no text yet; never retry rate limits."""
        return (
            attempt < max_retries
            and not produced
            and isinstance(error, (ProviderTimeoutError, TemporaryServerError))
        )

    def _parse_chat_chunk(self, data: str) -> dict[str, Any] | None:
        """Parse one SSE chunk into text, tool-call fragments, and the finish reason."""
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            raise MalformedResponseError("invalid JSON in SSE data", cause=exc) from exc
        if not isinstance(obj, dict):
            raise MalformedResponseError("SSE data must be a JSON object")
        choices = obj.get("choices")
        if choices is None:
            raise MalformedResponseError("missing 'choices' field in SSE data")
        if not isinstance(choices, list):
            raise MalformedResponseError("'choices' must be a list")
        if not choices:
            raise MalformedResponseError("'choices' must not be empty")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise MalformedResponseError("each choice must be a JSON object")
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise MalformedResponseError("'finish_reason' must be a string")
        if finish_reason == "":
            finish_reason = None
        if "delta" not in choice:
            if finish_reason is None:
                raise MalformedResponseError("missing 'delta' field in choice")
            delta: dict[str, Any] = {}
        else:
            delta = choice["delta"]
            if not isinstance(delta, dict):
                raise MalformedResponseError("'delta' must be a JSON object")

        reasoning = delta.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise MalformedResponseError("'delta.reasoning_content' must be a string")
        alternate_reasoning = None
        details = None
        if self._continuation_channel == "openrouter":
            alternate_reasoning = delta.get("reasoning")
            if alternate_reasoning is not None and not isinstance(
                alternate_reasoning, str
            ):
                raise MalformedResponseError("'delta.reasoning' must be a string")
            details = delta.get("reasoning_details")
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise MalformedResponseError("'delta.content' must be a string")

        fragments: list[dict[str, Any]] = []
        if "tool_calls" in delta:
            raw_calls = delta["tool_calls"]
            if not isinstance(raw_calls, list):
                raise MalformedResponseError("'delta.tool_calls' must be a list")
            for raw_call in raw_calls:
                if not isinstance(raw_call, dict):
                    raise MalformedResponseError("each tool call must be an object")
                index = raw_call.get("index")
                if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                    raise MalformedResponseError(
                        "tool call index must be a non-negative integer"
                    )
                fragment: dict[str, Any] = {"index": index}
                for field in ("id", "type"):
                    if field in raw_call:
                        value = raw_call[field]
                        if not isinstance(value, str):
                            raise MalformedResponseError(
                                f"tool call {field} must be a string"
                            )
                        if field == "type" and value != "function":
                            raise MalformedResponseError(
                                "tool call type must be function"
                            )
                        fragment[field] = value
                if "function" in raw_call:
                    function = raw_call["function"]
                    if not isinstance(function, dict):
                        raise MalformedResponseError(
                            "tool call function must be an object"
                        )
                    function_fragment: dict[str, str] = {}
                    if "name" in function:
                        if not isinstance(function["name"], str):
                            raise MalformedResponseError(
                                "tool function name must be a string"
                            )
                        function_fragment["name"] = function["name"]
                    if "arguments" in function:
                        if not isinstance(function["arguments"], str):
                            raise MalformedResponseError(
                                "tool function arguments must be a string"
                            )
                        function_fragment["arguments"] = function["arguments"]
                    fragment["function"] = function_fragment
                fragments.append(fragment)

        if (
            reasoning is None
            and alternate_reasoning is None
            and details is None
            and content is None
            and not fragments
            and finish_reason is None
        ):
            return None
        return {
            "reasoning_content": reasoning,
            "reasoning": alternate_reasoning,
            "reasoning_details": details,
            "content": content,
            "tool_calls": fragments,
            "finish_reason": finish_reason,
        }

    @staticmethod
    def _assemble_tool_calls(
        accumulators: dict[int, dict[str, Any]],
    ) -> tuple[ToolCall, ...]:
        if not accumulators or sorted(accumulators) != list(range(len(accumulators))):
            raise MalformedResponseError(
                "tool call indexes must be contiguous from zero"
            )
        calls: list[ToolCall] = []
        call_ids: set[str] = set()
        for index in range(len(accumulators)):
            accumulator = accumulators[index]
            call_id = accumulator.get("id")
            name = accumulator.get("name")
            arguments_text = accumulator.get("arguments")
            if not isinstance(call_id, str) or not call_id.strip():
                raise MalformedResponseError("tool call id is required")
            if not isinstance(name, str) or not name.strip():
                raise MalformedResponseError("tool function name is required")
            if not isinstance(arguments_text, str):
                raise MalformedResponseError("tool function arguments are required")
            if call_id in call_ids:
                raise MalformedResponseError("tool call ids must be unique")
            call_ids.add(call_id)
            try:
                arguments = json.loads(arguments_text)
                if not isinstance(arguments, dict):
                    raise TypeError("tool call arguments must be an object")
                calls.append(ToolCall(call_id, name, arguments))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise MalformedResponseError(
                    "tool call arguments must be a JSON object", cause=exc
                ) from exc
        return tuple(calls)

    def _finish_completion(
        self,
        reason: str | None,
        accumulators: dict[int, dict[str, Any]],
        has_text: bool,
        continuation: ModelStepContinuation | None = None,
    ) -> Finish:
        if accumulators:
            if reason != "tool_calls":
                raise MalformedResponseError(
                    "tool calls require a tool_calls finish reason"
                )
            return Finish(
                reason=reason,
                tool_calls=self._assemble_tool_calls(accumulators),
                continuation=continuation,
            )
        if reason == "tool_calls":
            raise MalformedResponseError("tool_calls finish reason requires tool calls")
        if not has_text and continuation is None:
            raise MalformedResponseError("completion contained no text or tool calls")
        return Finish(reason=reason, continuation=continuation)

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        try:
            payload = self._build_payload(request)
        except KeyError:
            yield ProviderErrorEvent(
                error=ProviderError("reasoning effort is not supported")
            )
            return
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        produced = False
        has_text = False
        tool_accumulators: dict[int, dict[str, Any]] = {}
        continuation = _ChatContinuationAccumulator(request, self._continuation_channel)
        for attempt in range(self._max_retries + 1):
            established = False
            try:
                async with self._client.stream(
                    "POST", self._endpoint, json=payload, headers=headers
                ) as response:
                    established = True
                    if response.status_code >= 400:
                        error = self._map_status_error(response.status_code)
                        if self._should_retry(
                            error, attempt, self._max_retries, produced
                        ):
                            await self._sleep(self._retry_backoff)
                            continue
                        yield ProviderErrorEvent(error=error)
                        return
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            # Blank lines, comments, and other SSE fields are ignored.
                            continue
                        data = line[len("data:") :].lstrip()
                        if data == "[DONE]":
                            try:
                                finish = self._finish_completion(
                                    "tool_calls" if tool_accumulators else None,
                                    tool_accumulators,
                                    has_text,
                                    continuation.record(),
                                )
                            except MalformedResponseError as exc:
                                yield ProviderErrorEvent(error=exc)
                                return
                            yield finish
                            return
                        try:
                            parsed = self._parse_chat_chunk(data)
                        except MalformedResponseError as exc:
                            yield ProviderErrorEvent(error=exc)
                            return
                        if parsed is None:
                            continue
                        for reasoning in continuation.add(
                            parsed, self._continuation_channel
                        ):
                            produced = True
                            yield ProviderReasoningDelta(text=reasoning)
                        content = parsed["content"]
                        if content:
                            produced = True
                            has_text = has_text or bool(content.strip())
                            yield TextDelta(text=content)
                        for fragment in parsed["tool_calls"]:
                            produced = True
                            index = fragment["index"]
                            accumulator = tool_accumulators.setdefault(index, {})
                            for field in ("id", "type"):
                                if field in fragment:
                                    value = fragment[field]
                                    previous = accumulator.get(field)
                                    if previous is not None and previous != value:
                                        error = MalformedResponseError(
                                            f"tool call {field} conflicts"
                                        )
                                        yield ProviderErrorEvent(error=error)
                                        return
                                    accumulator[field] = value
                            function = fragment.get("function", {})
                            for field in ("name", "arguments"):
                                if field in function:
                                    value = function[field]
                                    if field == "arguments":
                                        accumulator[field] = (
                                            accumulator.get(field, "") + value
                                        )
                                    else:
                                        previous = accumulator.get(field)
                                        if previous is not None and previous != value:
                                            error = MalformedResponseError(
                                                f"tool function {field} conflicts"
                                            )
                                            yield ProviderErrorEvent(error=error)
                                            return
                                        accumulator[field] = value
                        finish_reason = parsed["finish_reason"]
                        if finish_reason is None:
                            continue
                        try:
                            finish = self._finish_completion(
                                finish_reason,
                                tool_accumulators,
                                has_text,
                                continuation.record(),
                            )
                        except MalformedResponseError as exc:
                            yield ProviderErrorEvent(error=exc)
                            return
                        yield finish
                        return
                    # The stream drained naturally without a finish signal: malformed response.
                    yield ProviderErrorEvent(
                        error=MalformedResponseError(
                            "stream ended before a finish signal"
                        )
                    )
                    return
            except httpx.TimeoutException as exc:
                if established:
                    # Timeout while consuming SSE after the response was established:
                    # incomplete content, so malformed and not retried (emitted text
                    # deltas are preserved).
                    yield ProviderErrorEvent(
                        error=MalformedResponseError(
                            "SSE stream interrupted", cause=exc
                        )
                    )
                    return
                error = ProviderTimeoutError("request timed out", cause=exc)
                if self._should_retry(error, attempt, self._max_retries, produced):
                    await self._sleep(self._retry_backoff)
                    continue
                yield ProviderErrorEvent(error=error)
                return
            except httpx.TransportError as exc:
                if established:
                    # Transport interruption while consuming SSE after the response was
                    # established: incomplete content, so malformed and not retried
                    # (emitted text deltas are preserved).
                    yield ProviderErrorEvent(
                        error=MalformedResponseError(
                            "SSE stream interrupted", cause=exc
                        )
                    )
                    return
                error = TemporaryServerError("transport error", cause=exc)
                if self._should_retry(error, attempt, self._max_retries, produced):
                    await self._sleep(self._retry_backoff)
                    continue
                yield ProviderErrorEvent(error=error)
                return
