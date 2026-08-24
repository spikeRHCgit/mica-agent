"""Engine 与前端之间共享的最小交互响应。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .cancellation import CancellationRequested, CancellationToken


def _validate_interaction_id(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("interaction_id must be a str")
    if not value.strip():
        raise ValueError("interaction_id must be non-empty and non-blank")


class PermissionAnswer(Enum):
    """权限请求只允许对本次调用作答。"""

    ALLOW_ONCE = "allow_once"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PermissionResponse:
    """对一个权限交互的一次性响应。"""

    interaction_id: str
    answer: PermissionAnswer

    def __post_init__(self) -> None:
        _validate_interaction_id(self.interaction_id)
        if not isinstance(self.answer, PermissionAnswer):
            raise TypeError("answer must be a PermissionAnswer")


class InteractionKind(Enum):
    """前端需要回答的交互类型。"""

    PERMISSION = "permission"
    USER_INPUT = "user_input"


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    """不含运行时对象的、可供前端展示的交互请求快照。"""

    interaction_id: str
    kind: InteractionKind
    call_id: str
    tool_name: str
    message: str

    def __post_init__(self) -> None:
        _validate_interaction_id(self.interaction_id)
        if not isinstance(self.kind, InteractionKind):
            raise TypeError("kind must be an InteractionKind")
        if not isinstance(self.call_id, str) or not self.call_id.strip():
            raise ValueError("call_id must be non-empty and non-blank")
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("tool_name must be non-empty and non-blank")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be non-empty and non-blank")


@dataclass(slots=True)
class _PendingInteraction:
    request: InteractionRequest
    future: asyncio.Future[PermissionResponse | UserInputResponse]


class InteractionBroker:
    """前端无关的单次交互等待与响应入口。"""

    def __init__(self, interaction_id_factory: Callable[[], str] | None = None) -> None:
        self._interaction_id_factory = interaction_id_factory or (
            lambda: uuid.uuid4().hex
        )
        self._pending: dict[str, _PendingInteraction] = {}
        self._used_interaction_ids: set[str] = set()
        self._requests: asyncio.Queue[InteractionRequest] = asyncio.Queue()

    @property
    def pending_count(self) -> int:
        """当前仍等待响应的交互数，供清理和诊断测试使用。"""
        return len(self._pending)

    async def next_request(self) -> InteractionRequest:
        """取得下一个仍然活动的前端交互请求。"""
        while True:
            request = await self._requests.get()
            if request.interaction_id in self._pending:
                return request

    async def request_permission(
        self,
        call_id: str,
        tool_name: str,
        message: str,
        cancellation: CancellationToken,
    ) -> PermissionResponse:
        """登记权限请求并等待匹配的 PermissionResponse。"""
        interaction_id = self._new_interaction_id()
        response = await self._request(
            InteractionRequest(
                interaction_id,
                InteractionKind.PERMISSION,
                call_id,
                tool_name,
                message,
            ),
            cancellation,
        )
        if not isinstance(response, PermissionResponse):
            raise TypeError("permission response type mismatch")
        return response

    async def request_user_input(
        self,
        interaction_id: str,
        question: str,
        cancellation: CancellationToken,
    ) -> UserInputResponse:
        """实现 UserInputRequester 协议并等待匹配回答。"""
        request_interaction_id = self._new_interaction_id()
        response = await self._request(
            InteractionRequest(
                request_interaction_id,
                InteractionKind.USER_INPUT,
                interaction_id,
                "ask",
                question,
            ),
            cancellation,
        )
        if not isinstance(response, UserInputResponse):
            raise TypeError("user input response type mismatch")
        return UserInputResponse(interaction_id, response.answer)

    async def __call__(
        self,
        interaction_id: str,
        question: str,
        cancellation: CancellationToken,
    ) -> UserInputResponse:
        return await self.request_user_input(interaction_id, question, cancellation)

    def respond(self, response: PermissionResponse | UserInputResponse) -> bool:
        """提交一次响应；错误类型、ID、重复和迟到响应均返回 False。"""
        if not isinstance(response, (PermissionResponse, UserInputResponse)):
            return False
        pending = self._pending.get(response.interaction_id)
        if pending is None or pending.future.done():
            return False
        if pending.request.kind is InteractionKind.PERMISSION and not isinstance(
            response, PermissionResponse
        ):
            return False
        if pending.request.kind is InteractionKind.USER_INPUT and not isinstance(
            response, UserInputResponse
        ):
            return False
        pending.future.set_result(response)
        return True

    def clear(self) -> None:
        """取消并移除当前 Run 的所有等待者，迟到响应自动失效。"""
        pending = tuple(self._pending.values())
        self._pending.clear()
        for item in pending:
            if not item.future.done():
                item.future.cancel()
        while True:
            try:
                self._requests.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _request(
        self,
        request: InteractionRequest,
        cancellation: CancellationToken,
    ) -> PermissionResponse | UserInputResponse:
        cancellation.raise_if_cancelled()
        if request.interaction_id in self._pending:
            raise RuntimeError("interaction id is already pending")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PermissionResponse | UserInputResponse] = (
            loop.create_future()
        )
        self._pending[request.interaction_id] = _PendingInteraction(request, future)
        self._requests.put_nowait(request)
        cancellation_task = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait(
                {future, cancellation_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation_task in done:
                future.cancel()
                raise CancellationRequested("operation cancelled")
            return future.result()
        finally:
            cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)
            self._pending.pop(request.interaction_id, None)

    def _new_interaction_id(self) -> str:
        for _ in range(100):
            interaction_id = self._interaction_id_factory()
            _validate_interaction_id(interaction_id)
            if (
                interaction_id not in self._pending
                and interaction_id not in self._used_interaction_ids
            ):
                self._used_interaction_ids.add(interaction_id)
                return interaction_id
        raise RuntimeError("unable to allocate interaction id")


@dataclass(frozen=True, slots=True)
class UserInputResponse:
    """对一个用户输入交互的文本响应。"""

    interaction_id: str
    answer: str

    def __post_init__(self) -> None:
        _validate_interaction_id(self.interaction_id)
        if not isinstance(self.answer, str):
            raise TypeError("answer must be a str")
