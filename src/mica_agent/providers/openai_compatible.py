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
        """把供应商无关请求转换为 Chat Completions JSON 载荷。

        角色使用 ``MessageRole.value`` 作为 wire 值，不依赖枚举名称。
        """
        return {
            "model": request.model,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            "stream": True,
        }

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
    def _parse_chat_chunk(data: str) -> tuple[str, str] | None:
        """解析单个 SSE ``data`` 载荷为事件标记。

        返回 ``("text", content)``、``("finish", reason)`` 或
        ``None``（可忽略的事件：无文本的 delta 块等）；结构不符合
        Chat Completions 流式协议（含缺失、为空的 ``choices``）时
        抛 :class:`MalformedResponseError`。
        """
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            raise MalformedResponseError("invalid JSON in SSE data", cause=exc) from exc
        if not isinstance(obj, dict):
            raise MalformedResponseError("SSE data must be a JSON object")
        choices = obj.get("choices")
        if choices is None:
            # 缺失 choices：补充事件（usage 等）不按协议出现在流中间。
            raise MalformedResponseError("missing 'choices' field in SSE data")
        if not isinstance(choices, list):
            raise MalformedResponseError("'choices' must be a list")
        if not choices:
            # 空 choices：流式协议要求至少一个 choice，出现即畸形。
            raise MalformedResponseError("'choices' must not be empty")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise MalformedResponseError("each choice must be a JSON object")
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            if not isinstance(finish_reason, str):
                raise MalformedResponseError("'finish_reason' must be a string")
            return ("finish", finish_reason)
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise MalformedResponseError("'delta' must be a JSON object")
        content = delta.get("content")
        if content is None:
            # delta 无文本（角色块等）→ 忽略，不输出 "None"。
            return None
        if not isinstance(content, str):
            raise MalformedResponseError("'delta.content' must be a string")
        return ("text", content)

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        payload = self._build_payload(request)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        produced = False
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
                            yield Finish()
                            return
                        try:
                            parsed = self._parse_chat_chunk(data)
                        except MalformedResponseError as exc:
                            yield ProviderErrorEvent(error=exc)
                            return
                        if parsed is None:
                            continue
                        kind, value = parsed
                        if kind == "text":
                            produced = True
                            yield TextDelta(text=value)
                        else:  # finish：正常结束，不再消费后续行。
                            yield Finish(reason=value)
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
