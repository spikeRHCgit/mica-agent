"""MicaApp：进程内装配入口（P2-03）。

App 负责校验输入、维护当前进程的消息历史、通过 AgentEngine
运行单次 Run，并在 Run 成功时原子提交用户消息与完整助手回复。
Engine 与 Provider 都通过公共契约使用，不读取 Provider 私有状态。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from .engine import AgentEngine, RunIdFactory
from .events import EngineEvent, RunCompleted, TextDelta
from .models import Message, MessageRole
from .providers import Provider, ProviderRequest


class MicaAppError(Exception):
    """MicaApp 应用层错误基类。"""


class AppBusyError(MicaAppError):
    """同一 App 已存在活动 Run 时启动新 Run。"""


class AppClosedError(MicaAppError):
    """App 已关闭后再次启动 Run。"""


class MicaApp:
    """连接 CLI、Engine、Provider 与进程内历史的最小装配。"""

    def __init__(
        self,
        provider: Provider,
        model: str,
        *,
        system_prompt: str | None = None,
        run_id_factory: RunIdFactory | None = None,
    ) -> None:
        if not isinstance(model, str):
            raise TypeError(f"model must be a str, got {type(model).__name__}")
        if not model.strip():
            raise ValueError("model must be a non-empty, non-blank string")
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise TypeError(
                f"system_prompt must be a str or None, got {type(system_prompt).__name__}"
            )
        self._model = model
        self._provider = provider
        self._engine = AgentEngine(provider=provider, run_id_factory=run_id_factory)
        self._history: list[Message] = []
        if system_prompt is not None:
            self._history.append(
                Message(role=MessageRole.SYSTEM, content=system_prompt)
            )
        self._active_run = False
        self._closed = False

    @property
    def history(self) -> tuple[Message, ...]:
        """当前已提交历史的不可变快照。"""
        return tuple(self._history)

    async def aclose(self) -> None:
        """关闭 Provider 并标记 App 不可再运行；重复调用安全。"""
        if self._closed:
            return
        self._closed = True
        await self._provider.aclose()

    async def run(self, prompt: str) -> AsyncIterator[EngineEvent]:
        if self._closed:
            raise AppClosedError("app is closed; cannot start a new run")
        if not isinstance(prompt, str):
            raise TypeError(f"prompt must be a str, got {type(prompt).__name__}")
        if not prompt.strip():
            raise ValueError("prompt must be a non-empty, non-blank string")
        if self._active_run:
            raise AppBusyError("another run is already active")
        self._active_run = True
        user_message = Message(role=MessageRole.USER, content=prompt)
        request = ProviderRequest(
            model=self._model,
            messages=tuple(self._history) + (user_message,),
        )
        parts: list[str] = []
        try:
            async for event in self._engine.run(request):
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                elif isinstance(event, RunCompleted):
                    # 原子提交：只有正常完成后同时提交用户与完整助手消息。
                    self._history.append(user_message)
                    self._history.append(
                        Message(role=MessageRole.ASSISTANT, content="".join(parts))
                    )
                yield event
        finally:
            self._active_run = False
