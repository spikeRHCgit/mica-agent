"""供应商无关的 Provider 公共契约（阶段计划 4.2）。

本模块只定义契约，不实现任何网络请求；协议适配和重试由
后续 Provider 实现（P2-02）在传输边界完成。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import Message


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """一次模型请求的供应商无关输入。

    ``model`` 必须是非空、非纯空白字符串；``messages`` 必须是非空
    元组且每项都是 ``Message``。使用元组承载：Provider 或调用方都
    无法在创建后篡改消息历史。连接信息（URL、Key 等）属于 Provider
    装配层，不属于单次请求数据。
    """

    model: str
    messages: tuple[Message, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.model, str):
            raise TypeError(f"model must be a str, got {type(self.model).__name__}")
        if not self.model.strip():
            raise ValueError("model must be a non-empty, non-blank string")
        if not isinstance(self.messages, tuple):
            raise TypeError(
                f"messages must be a tuple, got {type(self.messages).__name__}"
            )
        if not self.messages:
            raise ValueError("messages must not be empty")
        if not all(isinstance(message, Message) for message in self.messages):
            raise TypeError("every message in messages must be a Message")


_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization: Bearer <token>（保留头部前缀）
    re.compile(r"(authorization\s*:\s*bearer\s+)\S+", re.IGNORECASE),
    # sk- 前缀的 API Key（OpenAI/Anthropic 风格，整体替换）
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b"),
    # api_key= / api-key: <token>（保留键名前缀）
    re.compile(r"(api[_-]?key\s*[=:]\s*)\S+", re.IGNORECASE),
)


def _redact(message: str) -> str:
    """把常见凭据形态替换为 ``[redacted]``；带捕获组的模式保留前缀。"""
    redacted = message
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(
            lambda match: (
                f"{match.group(1)}[redacted]" if match.lastindex else "[redacted]"
            ),
            redacted,
        )
    return redacted


class ProviderError(Exception):
    """模型请求错误的供应商无关基类；也是未知错误的兜底类别。

    公开诊断安全约束：``str()`` 与 ``repr()`` 只输出脱敏后的
    面向用户诊断，不包含 API Key、Authorization 头值或原始响应体。
    原始异常可通过 ``cause`` 保留在内部，但永不进入 ``str``/``repr``。
    """

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        if not isinstance(message, str):
            public = "<non-string diagnostic>"
        else:
            public = _redact(message)
        super().__init__(public)
        self._cause = cause

    @property
    def cause(self) -> BaseException | None:
        """内部保留的原始异常（用于调试），不进入 ``str``/``repr``。"""
        return self._cause


class AuthenticationError(ProviderError):
    """鉴权失败：凭证缺失、无效或过期。"""


class RateLimitError(ProviderError):
    """限流：请求被服务端频率限制拒绝。"""


class ProviderTimeoutError(ProviderError):
    """请求超时（区别于内置 TimeoutError，语义限定为模型请求）。"""


class TemporaryServerError(ProviderError):
    """临时服务端错误：可安全重试的瞬时故障。"""


class MalformedResponseError(ProviderError):
    """畸形响应：流式内容无法按协议解析。"""


@dataclass(frozen=True, slots=True)
class TextDelta:
    """流式文本增量片段。"""

    text: str


@dataclass(frozen=True, slots=True)
class Finish:
    """流正常结束。``reason`` 为供应商结束原因（如 ``stop``），
    默认 ``None`` 表示无特定原因；当前仅作信息承载，不驱动语义。"""

    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderErrorEvent:
    """流内错误信号：携带错误对象，供调用方在同一迭代中处理。"""

    error: ProviderError


ProviderEvent = TextDelta | Finish | ProviderErrorEvent
"""Provider 流式输出的统一事件类型。"""


@runtime_checkable
class Provider(Protocol):
    """Provider 能力契约：以异步流消费请求并产出标准化事件。

    唯一公共调用方式是 ``async for``：

    .. code-block:: python

        async for event in provider.stream(request):
            ...

    ``stream`` 是普通方法，返回一次性的异步迭代器（实现类以异步
    生成器实现，即 ``async def stream(...): yield ...``）；调用返回
    后即可被 ``async for`` 消费，产出 ``TextDelta`` 文本增量、
    ``Finish`` 正常结束或 ``ProviderErrorEvent`` 流内错误。

    唯一错误通道：

    - 已分类、可预期的 Provider 失败（鉴权、限流、超时、临时服务端
      错误、畸形响应等）必须以 ``ProviderErrorEvent`` 作为流内
      **最后一个事件** 产出；其后不得再出现 ``TextDelta`` 或
      ``Finish``。实现不得对同一种已分类错误自由选择产出或抛出。
    - 只有未预期的程序错误（bug、断言失败等）才允许作为异常从
      ``stream`` 传播。

    实现类通过鸭子类型符合本协议，无需继承；Engine 只依赖此
    契约，不接触任何供应商私有对象。
    """

    def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]: ...
