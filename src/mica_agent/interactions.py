"""Minimal interaction responses shared between the engine and the frontend."""

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
    """Permission requests can only answer for the current call."""

    ALLOW_ONCE = "allow_once"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PermissionResponse:
    """One-shot response to a permission interaction."""

    interaction_id: str
    answer: PermissionAnswer

    def __post_init__(self) -> None:
        _validate_interaction_id(self.interaction_id)
        if not isinstance(self.answer, PermissionAnswer):
            raise TypeError("answer must be a PermissionAnswer")


class InteractionKind(Enum):
    """Interaction kinds the frontend must answer."""

    PERMISSION = "permission"
    USER_INPUT = "user_input"


def _validate_nonblank_string(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str")
    if not value.strip():
        raise ValueError(f"{name} must be non-empty and non-blank")


@dataclass(frozen=True, slots=True)
class AskOption:
    """One frontend-agnostic structured ask option."""

    label: str
    description: str | None = None
    recommended: bool = False

    def __post_init__(self) -> None:
        _validate_nonblank_string("label", self.label)
        if self.description is not None:
            _validate_nonblank_string("description", self.description)
        if type(self.recommended) is not bool:
            raise TypeError("recommended must be a bool")


@dataclass(frozen=True, slots=True)
class AskPrompt:
    """Frontend-agnostic structured ask request content."""

    question: str
    title: str | None = None
    options: tuple[AskOption, ...] = ()
    allow_custom: bool = False

    def __post_init__(self) -> None:
        _validate_nonblank_string("question", self.question)
        if self.title is not None:
            _validate_nonblank_string("title", self.title)
        if not isinstance(self.options, (tuple, list)):
            raise TypeError("options must be a tuple or list")
        options = tuple(self.options)
        if not all(isinstance(option, AskOption) for option in options):
            raise TypeError("options must contain AskOption instances")
        if options and not 2 <= len(options) <= 4:
            raise ValueError("options must contain two to four items")
        if sum(option.recommended for option in options) > 1:
            raise ValueError("options may contain at most one recommended item")
        if type(self.allow_custom) is not bool:
            raise TypeError("allow_custom must be a bool")
        object.__setattr__(self, "options", options)


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    """Frontend-renderable interaction request snapshot without runtime objects."""

    interaction_id: str
    kind: InteractionKind
    call_id: str
    tool_name: str
    message: str
    ask_prompt: AskPrompt | None = None

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
        if self.kind is InteractionKind.USER_INPUT:
            if self.ask_prompt is None:
                object.__setattr__(self, "ask_prompt", AskPrompt(self.message))
            elif not isinstance(self.ask_prompt, AskPrompt):
                raise TypeError("ask_prompt must be an AskPrompt or None")
            elif self.ask_prompt.question != self.message:
                raise ValueError("ask_prompt question must match message")
        elif self.ask_prompt is not None:
            raise ValueError("permission requests must not include ask_prompt")


@dataclass(slots=True)
class _PendingInteraction:
    request: InteractionRequest
    future: asyncio.Future[PermissionResponse | UserInputResponse]


class InteractionBroker:
    """Frontend-agnostic entry point for waiting on and answering single interactions."""

    def __init__(self, interaction_id_factory: Callable[[], str] | None = None) -> None:
        self._interaction_id_factory = interaction_id_factory or (
            lambda: uuid.uuid4().hex
        )
        self._pending: dict[str, _PendingInteraction] = {}
        self._used_interaction_ids: set[str] = set()
        self._requests: asyncio.Queue[InteractionRequest] = asyncio.Queue()

    @property
    def pending_count(self) -> int:
        """Number of interactions still awaiting a response, used for cleanup and diagnostics."""
        return len(self._pending)

    async def next_request(self) -> InteractionRequest:
        """Get the next still-active frontend interaction request."""
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
        """Register a permission request and await the matching ``PermissionResponse``."""
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
        prompt: AskPrompt | str,
        cancellation: CancellationToken,
    ) -> UserInputResponse:
        """Implement the ``UserInputRequester`` protocol and await the matching answer."""
        if isinstance(prompt, str):
            prompt = AskPrompt(prompt)
        elif not isinstance(prompt, AskPrompt):
            raise TypeError("prompt must be an AskPrompt or str")
        request_interaction_id = self._new_interaction_id()
        response = await self._request(
            InteractionRequest(
                request_interaction_id,
                InteractionKind.USER_INPUT,
                interaction_id,
                "ask",
                prompt.question,
                prompt,
            ),
            cancellation,
        )
        if not isinstance(response, UserInputResponse):
            raise TypeError("user input response type mismatch")
        return UserInputResponse(
            interaction_id,
            response.answer,
            response.selected_option_index,
            response.notes,
        )

    async def __call__(
        self,
        interaction_id: str,
        prompt: AskPrompt | str,
        cancellation: CancellationToken,
    ) -> UserInputResponse:
        return await self.request_user_input(interaction_id, prompt, cancellation)

    def respond(self, response: PermissionResponse | UserInputResponse) -> bool:
        """Submit one response; wrong type, unknown id, duplicate, and late responses all
        return False."""
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
        if isinstance(response, UserInputResponse):
            prompt = pending.request.ask_prompt
            if prompt is None:
                return False
            selected_index = response.selected_option_index
            if not prompt.options:
                if selected_index is not None:
                    return False
            elif selected_index is None:
                if not prompt.allow_custom:
                    return False
            elif selected_index >= len(prompt.options):
                return False
            else:
                response = UserInputResponse(
                    response.interaction_id,
                    prompt.options[selected_index].label,
                    selected_index,
                    response.notes,
                )
        pending.future.set_result(response)
        return True

    def clear(self) -> None:
        """Cancel and remove all waiters of the current run; late responses stop matching."""
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
    """Text response to one user-input interaction."""

    interaction_id: str
    answer: str
    selected_option_index: int | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        _validate_interaction_id(self.interaction_id)
        if not isinstance(self.answer, str):
            raise TypeError("answer must be a str")
        if self.selected_option_index is not None:
            if type(self.selected_option_index) is not int:
                raise TypeError("selected_option_index must be an int or None")
            if self.selected_option_index < 0:
                raise ValueError("selected_option_index must be non-negative")
        if self.notes is not None and not isinstance(self.notes, str):
            raise TypeError("notes must be a str or None")
