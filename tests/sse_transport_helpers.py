"""Provider 传输边界测试的 HTTP transport 替身与 SSE 响应构造辅助。

只模拟 HTTP 传输层：替身返回的 ``httpx.Response`` 与真实网络响应
同构，Provider 无法区分；不实现任何 FakeProvider。
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable

import httpx

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]
BodyContent = AsyncIterable[bytes] | bytes | None


def sse_event(payload: str) -> str:
    """构造一条 ``data: <payload>`` 的 SSE 事件文本（含尾随空行）。"""
    return f"data: {payload}\n\n"


def sse_body(chunks: list[str]) -> bytes:
    """把 SSE 事件、空行、心跳片段拼接为响应体。"""
    return "".join(chunks).encode("utf-8")


def sse_response(
    chunks: list[str] | None = None,
    *,
    status: int = 200,
    content: BodyContent = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """构造模拟 SSE 的 ``httpx.Response``。

    ``content`` 非空时按原样使用（可传异步字节迭代器模拟慢速
    或中途断开的流）；否则把 ``chunks`` 拼接为字节响应体。
    """
    merged_headers = {"content-type": "text/event-stream"}
    if headers:
        merged_headers.update(headers)
    if content is None:
        content = sse_body(chunks or [])
    return httpx.Response(status_code=status, headers=merged_headers, content=content)


class RecordingTransport(httpx.AsyncBaseTransport):
    """记录请求与响应并转发给 handler 的 transport 替身。

    - ``requests``：按发送顺序记录每个请求，供断言方法、URL、
      请求体和鉴权头。
    - ``responses``：按顺序记录 handler 返回的每个响应，供断言
      响应资源是否被关闭（``Response.is_closed``）。
    - ``aclose_count``：记录 ``aclose`` 调用次数，用于观察承载该
      transport 的 ``httpx.AsyncClient`` 是否被关闭。
    """

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []
        self.aclose_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = await self._handler(request)
        self.responses.append(response)
        return response

    async def aclose(self) -> None:
        self.aclose_count += 1
