"""MicaApp：Session Service 驱动的最小应用装配入口。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from datetime import UTC, datetime
from uuid import uuid4

from .context_builder import ContextBuilder
from .engine import AgentEngine, RunIdFactory
from .events import EngineEvent, ErrorCategory, RunCompleted, RunFailed, TextDelta
from .models import Message, MessageRole
from .providers import Provider, ProviderRequest
from .session_contracts import TurnEntry
from .session_service import SessionService

TurnIdFactory = Callable[[], str]
Clock = Callable[[], datetime]
_STORAGE_FAILURE_MESSAGE = "session persistence failed"


class MicaAppError(Exception):
    """MicaApp 应用层错误基类。"""


class AppBusyError(MicaAppError):
    """同一 App 已存在活动 Run 时启动新 Run。"""


class AppClosedError(MicaAppError):
    """App 已关闭后再次启动 Run。"""


class MicaApp:
    """连接 Session Service、Context Builder、Engine 与 Provider。"""

    def __init__(
        self,
        provider: Provider,
        model: str,
        *,
        session_service: SessionService,
        session_id: str,
        context_builder: ContextBuilder,
        system_prompt: str | None = None,
        run_id_factory: RunIdFactory | None = None,
        turn_id_factory: TurnIdFactory | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(model, str):
            raise TypeError(f"model must be a str, got {type(model).__name__}")
        if not model.strip():
            raise ValueError("model must be a non-empty, non-blank string")
        if not isinstance(session_service, SessionService):
            raise TypeError("session_service must be a SessionService")
        if not isinstance(session_id, str):
            raise TypeError(
                f"session_id must be a str, got {type(session_id).__name__}"
            )
        if not session_id.strip():
            raise ValueError("session_id must be a non-empty, non-blank string")
        if not isinstance(context_builder, ContextBuilder):
            raise TypeError("context_builder must be a ContextBuilder")
        if system_prompt is not None and not isinstance(system_prompt, str):
            raise TypeError(
                f"system_prompt must be a str or None, got {type(system_prompt).__name__}"
            )
        self._model = model
        self._provider = provider
        self._session_service = session_service
        self._session_id = session_id
        self._context_builder = context_builder
        self._system_prompt = system_prompt
        self._turn_id_factory = turn_id_factory or (lambda: uuid4().hex)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._engine = AgentEngine(provider=provider, run_id_factory=run_id_factory)
        self._active_run = False
        self._closed = False

    @property
    def session_id(self) -> str:
        """当前 App 绑定的会话 ID。"""
        return self._session_id

    @property
    def history(self) -> tuple[Message, ...]:
        """从 Session Service 重新取得的不可变持久化历史快照。"""
        return self._session_service.context_snapshot(self._session_id)

    async def aclose(self) -> None:
        """关闭 Provider 并标记 App 不可再运行；重复调用安全。"""
        if self._closed:
            return
        self._closed = True
        await self._provider.aclose()

    async def run(self, prompt: str) -> AsyncIterator[EngineEvent]:
        """启动一次 Run，并仅在 RunCompleted 后提交完整 TurnEntry。"""
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
        parts: list[str] = []
        try:
            session_messages = self._session_service.context_snapshot(self._session_id)
            context = self._context_builder.build(session_messages)
            prefix = (
                (Message(role=MessageRole.SYSTEM, content=self._system_prompt),)
                if self._system_prompt is not None
                else ()
            )
            request = ProviderRequest(
                model=self._model,
                messages=prefix + context.messages + (user_message,),
            )
            async with aclosing(self._engine.run(request)) as events:
                async for event in events:
                    if isinstance(event, TextDelta):
                        parts.append(event.text)
                    elif isinstance(
                        event, RunCompleted
                    ) and not self._commit_completed_turn(
                        event, user_message, "".join(parts)
                    ):
                        yield RunFailed(
                            run_id=event.run_id,
                            seq=event.seq,
                            category=ErrorCategory.STORAGE,
                            message=_STORAGE_FAILURE_MESSAGE,
                        )
                        return
                    yield event
        finally:
            self._active_run = False

    def _commit_completed_turn(
        self,
        event: RunCompleted,
        user_message: Message,
        assistant_text: str,
    ) -> bool:
        try:
            turn = TurnEntry(
                turn_id=self._turn_id_factory(),
                run_id=event.run_id,
                user_message=user_message,
                assistant_message=Message(
                    role=MessageRole.ASSISTANT,
                    content=assistant_text,
                ),
                completed_at=self._clock(),
            )
            self._session_service.commit_turn(self._session_id, turn)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - persistence boundary is intentionally safe
            return False
        return True


__all__ = [
    "AppBusyError",
    "AppClosedError",
    "MicaApp",
    "MicaAppError",
]
