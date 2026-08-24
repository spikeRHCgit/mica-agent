"""OpenAI-compatible Chat Completions 流式 Provider（P2-02）。

协议适配、SSE 解析、错误分类和有限重试全部限制在本模块内；
对外只通过 :data:`~mica_agent.providers.base.ProviderEvent` 与调用方
交互，不暴露 ``httpx.Response`` 或任何供应商私有对象。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from ..tool_contracts import ToolCall, to_mutable_json
from .base import (
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

SleepFn = Callable[[float], Awaitable[None]]


def resolve_endpoint(base_url: str) -> str:
    """把用户提供的 URL 解析为 Chat Completions 端点。

    约定：

    - URL 路径已经以 ``/chat/completions`` 结尾时视为完整端点，
      原样返回（仅去除结尾斜杠）。
    - 否则视为 base URL，在路径末尾拼接 ``/chat/completions``。

    该约定保证永远不会产生 ``/chat/completions/chat/completions``。
    """
    url = httpx.URL(base_url)
    path = url.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return str(url).rstrip("/")
    return str(url.copy_with(path=f"{path}/chat/completions"))


class OpenAICompatibleProvider:
    """基于 OpenAI Chat Completions 流式协议的生产 Provider。

    ``base_url`` 可以是已包含 ``/chat/completions`` 的完整端点，
    也可以是 base URL（自动拼接端点）；URL 语义见
    :func:`resolve_endpoint`。API Key 只进入请求头，永不进入
    ``repr``、异常消息或事件。

    重试：默认只对 ``ProviderTimeoutError`` 与 ``TemporaryServerError``
    且尚未产出任何文本的事件流重试，最多 ``max_retries`` 次；
    ``RateLimitError`` 不做自动重试（留给上层决策）。HTTP 响应
    成功建立后发生的传输中断视为畸形响应
    （``MalformedResponseError``），不重试、不重复发送请求。
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
        self._api_key = api_key
        self._endpoint = resolve_endpoint(base_url)
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport
            if transport is not None
            else httpx.AsyncHTTPTransport(),
        )

    def __repr__(self) -> str:
        # 永不包含 API Key。
        return (
            f"OpenAICompatibleProvider(endpoint={self._endpoint!r}, "
            f"max_retries={self._max_retries})"
        )

    async def aclose(self) -> None:
        """关闭内部 HTTP 客户端；幂等，可在不再使用时调用。"""
        await self._client.aclose()

    # -- 请求与响应 --------------------------------------------------------

    @staticmethod
    def _build_payload(request: ProviderRequest) -> dict[str, Any]:
        """把供应商无关请求转换为 Chat Completions JSON 载荷。"""
        messages: list[dict[str, Any]] = []
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
            messages.append(item)

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "stream": True,
        }
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
        """只对尚未产出文本的临时失败重试；RateLimit 不自动重试。"""
        return (
            attempt < max_retries
            and not produced
            and isinstance(error, (ProviderTimeoutError, TemporaryServerError))
        )

    @staticmethod
    def _parse_chat_chunk(data: str) -> dict[str, Any] | None:
        """解析单个 SSE chunk，返回文本、工具增量和结束原因。"""
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
        if "delta" not in choice:
            if finish_reason is None:
                raise MalformedResponseError("missing 'delta' field in choice")
            delta: dict[str, Any] = {}
        else:
            delta = choice["delta"]
            if not isinstance(delta, dict):
                raise MalformedResponseError("'delta' must be a JSON object")

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

        if content is None and not fragments and finish_reason is None:
            return None
        return {
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

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        payload = self._build_payload(request)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        produced = False
        tool_accumulators: dict[int, dict[str, Any]] = {}
        tool_finish_reason: str | None = None
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
                            # 空行、注释或其他 SSE 字段 → 忽略。
                            continue
                        data = line[len("data:") :].lstrip()
                        if data == "[DONE]":
                            if tool_accumulators:
                                try:
                                    tool_calls = self._assemble_tool_calls(
                                        tool_accumulators
                                    )
                                except MalformedResponseError as exc:
                                    yield ProviderErrorEvent(error=exc)
                                    return
                                yield Finish(
                                    reason=tool_finish_reason or "tool_calls",
                                    tool_calls=tool_calls,
                                )
                            else:
                                yield Finish()
                            return
                        try:
                            parsed = self._parse_chat_chunk(data)
                        except MalformedResponseError as exc:
                            yield ProviderErrorEvent(error=exc)
                            return
                        if parsed is None:
                            continue
                        content = parsed["content"]
                        if content is not None:
                            produced = True
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
                        tool_finish_reason = finish_reason
                        if tool_accumulators and finish_reason != "tool_calls":
                            yield ProviderErrorEvent(
                                error=MalformedResponseError(
                                    "tool calls require a tool_calls finish reason"
                                )
                            )
                            return
                        if finish_reason == "tool_calls":
                            if not tool_accumulators:
                                yield ProviderErrorEvent(
                                    error=MalformedResponseError(
                                        "tool_calls finish reason requires tool calls"
                                    )
                                )
                                return
                            try:
                                tool_calls = self._assemble_tool_calls(
                                    tool_accumulators
                                )
                            except MalformedResponseError as exc:
                                yield ProviderErrorEvent(error=exc)
                                return
                            yield Finish(reason=finish_reason, tool_calls=tool_calls)
                            return
                        yield Finish(reason=finish_reason)
                        return
                    # 流自然耗尽且从未收到结束信号 → 畸形响应。
                    yield ProviderErrorEvent(
                        error=MalformedResponseError(
                            "stream ended before a finish signal"
                        )
                    )
                    return
            except httpx.TimeoutException as exc:
                if established:
                    # 响应已建立、SSE 消费中途超时：内容不完整 → 畸形，
                    # 不重试（保留已产出的文本增量）。
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
                    # 响应已建立、SSE 消费中途传输中断：内容不完整 →
                    # 畸形，不重试（保留已产出的文本增量）。
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
