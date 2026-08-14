"""AgentEngine：单次 Run 的异步编排（P2-03）。

Engine 只依赖 Provider 公共契约（``stream`` 异步流），不读取
环境变量、配置或 CLI 状态；把 Provider 事件映射为带 ``run_id``
与严格 ``seq`` 的 Engine 事件，并保证恰好一个终态。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable

from .events import (
    EngineEvent,
    ErrorCategory,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
)
from .providers import (
    AuthenticationError,
    Finish,
    MalformedResponseError,
    Provider,
    ProviderError,
    ProviderErrorEvent,
    ProviderRequest,
    ProviderTimeoutError,
    RateLimitError,
    TemporaryServerError,
)
from .providers import TextDelta as ProviderTextDelta

RunIdFactory = Callable[[], str]


def _default_run_id() -> str:
    """默认 Run ID：标准库生成的非空唯一标识。"""
    return uuid.uuid4().hex


class AgentEngine:
    """消费 Provider 流并输出有序 Engine 事件。

    每次 :meth:`run` 产出一个 Run：``RunStarted(seq=0)`` 开头，
    随后是文本增量与恰好一个终态（``RunCompleted`` /
    ``RunFailed`` / ``RunCancelled``）；``seq`` 严格递增。
    终态之后立即停止消费 Provider 流，不产生第二个终态。
    """

    def __init__(
        self,
        provider: Provider,
        *,
        run_id_factory: RunIdFactory | None = None,
    ) -> None:
        self._provider = provider
        self._run_id_factory = run_id_factory or _default_run_id

    async def run(self, request: ProviderRequest) -> AsyncIterator[EngineEvent]:
        run_id = self._run_id_factory()
        seq = 0
        yield RunStarted(run_id=run_id)
        seq += 1
        try:
            async for event in self._provider.stream(request):
                if isinstance(event, ProviderTextDelta):
                    if not isinstance(event.text, str):
                        yield RunFailed(
                            run_id=run_id,
                            seq=seq,
                            category=ErrorCategory.MALFORMED,
                            message="provider produced a non-string text delta",
                        )
                        return
                    yield TextDelta(run_id=run_id, seq=seq, text=event.text)
                    seq += 1
                elif isinstance(event, Finish):
                    yield RunCompleted(run_id=run_id, seq=seq)
                    return
                elif isinstance(event, ProviderErrorEvent):
                    yield RunFailed(
                        run_id=run_id,
                        seq=seq,
                        category=self._map_error(event.error),
                        message=self._error_message(event.error),
                    )
                    return
                else:
                    # 未知事件对象：安全失败，不尝试渲染。
                    yield RunFailed(
                        run_id=run_id,
                        seq=seq,
                        category=ErrorCategory.UNKNOWN,
                        message="provider produced an unknown event",
                    )
                    return
            # Provider 流自然结束且从未出现终态 → 畸形响应。
            yield RunFailed(
                run_id=run_id,
                seq=seq,
                category=ErrorCategory.MALFORMED,
                message="provider stream ended without a terminal event",
            )
            return
        except asyncio.CancelledError:
            yield RunCancelled(run_id=run_id, seq=seq)
            raise
        except Exception:  # noqa: BLE001 - 任务单要求所有未预期异常转为安全 RunFailed
            # 未预期程序异常：固定、安全、简短诊断，不泄露原异常。
            yield RunFailed(
                run_id=run_id,
                seq=seq,
                category=ErrorCategory.UNKNOWN,
                message="unexpected provider error",
            )
            return

    @staticmethod
    def _map_error(error: object) -> ErrorCategory:
        """ProviderError 到统一错误类别；非 ProviderError 归 UNKNOWN。"""
        if isinstance(error, AuthenticationError):
            return ErrorCategory.AUTHENTICATION
        if isinstance(error, RateLimitError):
            return ErrorCategory.RATE_LIMIT
        if isinstance(error, ProviderTimeoutError):
            return ErrorCategory.TIMEOUT
        if isinstance(error, TemporaryServerError):
            return ErrorCategory.TEMPORARY
        if isinstance(error, MalformedResponseError):
            return ErrorCategory.MALFORMED
        return ErrorCategory.UNKNOWN

    @staticmethod
    def _error_message(error: object) -> str:
        """面向用户的诊断：只使用 ProviderError 已清洗的公开字符串。

        空字符串与纯空白字符串统一转换为固定安全文案，保证
        ``RunFailed.message`` 永远非空；错误类别不受影响。
        """
        if isinstance(error, ProviderError):
            message = str(error)
            return message if message.strip() else "provider error"
        return "provider produced an unknown error"
