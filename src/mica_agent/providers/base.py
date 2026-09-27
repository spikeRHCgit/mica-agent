"""Provider-agnostic public contract.

This module only defines contracts and performs no network requests; protocol
adaptation and retries belong to provider implementations at the transport boundary.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import Message
from ..reasoning import ReasoningEffort
from ..tool_contracts import ToolCall, ToolSpec


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Provider-agnostic input for a single model request.

    ``model`` must be a non-empty, non-blank string; ``messages`` must be a non-empty
    tuple whose items are all ``Message``; ``tools`` is a tuple of ``ToolSpec``. Tuples
    are used so that neither the provider nor the caller can mutate the message
    history or the tool snapshot after construction. Connection details (URL, key,
    ...) belong to provider assembly, not to per-request data.
    """

    model: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    reasoning_effort: ReasoningEffort = ReasoningEffort.OFF

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
        if not isinstance(self.tools, tuple):
            raise TypeError("tools must be a tuple")
        if not all(isinstance(tool, ToolSpec) for tool in self.tools):
            raise TypeError("every item in tools must be a ToolSpec")
        if not isinstance(self.reasoning_effort, ReasoningEffort):
            raise TypeError("reasoning_effort must be a ReasoningEffort")


_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization: Bearer <token> (keeps the header prefix)
    re.compile(r"(authorization\s*:\s*bearer\s+)\S+", re.IGNORECASE),
    # sk- prefixed API keys (OpenAI/Anthropic style, replaced entirely)
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b"),
    # api_key= / api-key: <token> (keeps the key-name prefix)
    re.compile(r"(api[_-]?key\s*[=:]\s*)\S+", re.IGNORECASE),
)


def _redact(message: str) -> str:
    """Replace common credential shapes with ``[redacted]``; capture-group patterns keep
    their prefix."""
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
    """Provider-agnostic base class for model request errors; also the fallback for
    unknown errors.

    Public diagnostic safety constraint: ``str()`` and ``repr()`` emit only the
    redacted user-facing diagnostic, never an API key, an Authorization header value,
    or a raw response body. The original exception may be kept internally through
    ``cause``, but never enters ``str``/``repr``.
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
        """Original exception kept internally for debugging; never enters ``str``/``repr``."""
        return self._cause


class AuthenticationError(ProviderError):
    """Authentication failed: credentials are missing, invalid, or expired."""


class RateLimitError(ProviderError):
    """Rate limited: the server rejected the request on frequency limits."""


class ProviderTimeoutError(ProviderError):
    """Request timed out (distinct from the built-in ``TimeoutError``; scoped to model
    requests)."""


class TemporaryServerError(ProviderError):
    """Temporary server error: a transient failure that is safe to retry."""


class MalformedResponseError(ProviderError):
    """Malformed response: streaming content cannot be parsed per protocol."""


@dataclass(frozen=True, slots=True)
class ProviderReasoningDelta:
    """Non-empty reasoning text delta recognized by the protocol adapter."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be a str")
        if not self.text:
            raise ValueError("text must not be empty")


@dataclass(frozen=True, slots=True)
class TextDelta:
    """Streaming text delta fragment."""

    text: str


@dataclass(frozen=True, slots=True)
class Finish:
    """The stream finished normally.

    ``reason`` is the provider finish reason (for example ``stop``); the default
    ``None`` means no specific reason. Complete tool calls are exposed through
    ``tool_calls`` only at finish and never contain provider-private fragments.
    """

    reason: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tool_calls, tuple):
            raise TypeError("tool_calls must be a tuple")
        if not all(isinstance(call, ToolCall) for call in self.tool_calls):
            raise TypeError("every item in tool_calls must be a ToolCall")


@dataclass(frozen=True, slots=True)
class ProviderErrorEvent:
    """In-stream error signal carrying the error object for the caller to handle in the
    same iteration."""

    error: ProviderError


# Unified event type emitted by provider streams.
ProviderEvent = ProviderReasoningDelta | TextDelta | Finish | ProviderErrorEvent


@runtime_checkable
class Provider(Protocol):
    """Provider capability contract: consume a request as an async stream and yield
    normalized events.

    The only public call form is ``async for``:

    .. code-block:: python

        async for event in provider.stream(request):
            ...

    ``stream`` is a plain method that returns a one-shot async iterator (implementations
    use an async generator, i.e. ``async def stream(...): yield ...``); the returned
    iterator can immediately be consumed with ``async for``, yielding ``TextDelta`` text
    deltas, ``Finish`` normal completion, or ``ProviderErrorEvent`` in-stream errors.

    The only error channel:

    - A classified, expected provider failure (authentication, rate limit, timeout,
      temporary server error, malformed response, ...) must be produced as a
      ``ProviderErrorEvent`` that is the **last event** in the stream; no ``TextDelta``
      or ``Finish`` may follow. Implementations must not choose freely between
      yielding and raising the same classified error.
    - Only unexpected program errors (bugs, assertion failures, ...) may propagate from
      ``stream`` as exceptions.

    Implementations satisfy this protocol by duck typing, without inheriting from it;
    the engine depends only on this contract and never touches provider-private
    objects.
    """

    def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]: ...

    async def aclose(self) -> None:
        """Close resources held by the provider; must be idempotent and repeatable."""
        ...
