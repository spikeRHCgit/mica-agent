"""``MicaApp``: minimal application assembly entry driven by the session service."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from datetime import UTC, datetime
from uuid import uuid4

from .cancellation import CancellationToken
from .checkpoint_contracts import RunCheckpoint, RunCheckpointKind
from .context_builder import ContextBuilder, ContextBuildResult
from .engine import AgentEngine, RunIdFactory, RunProgressSnapshot
from .events import (
    EngineEvent,
    ErrorCategory,
    RunCancelled,
    RunCompleted,
    RunFailed,
)
from .interactions import PermissionResponse, UserInputResponse
from .models import Message, MessageRole
from .providers import Provider, ProviderRequest
from .reasoning import ReasoningEffort
from .session_contracts import TurnEntry
from .session_service import SessionService
from .system_prompt import build_system_prompt
from .tool_executor import ToolExecutor
from .tool_presentation import ToolPresentationSnapshot

TurnIdFactory = Callable[[], str]
CheckpointIdFactory = Callable[[], str]
Clock = Callable[[], datetime]
_STORAGE_FAILURE_MESSAGE = "session persistence failed"
_STORAGE_FAILURE_AFTER_TOOL_MESSAGE = (
    "session persistence failed; tool changes may have occurred; check the workspace"
)
_SIDE_EFFECT_TOOL_NAMES = frozenset({"write", "edit", "bash"})
_RECOVERABLE_ERROR_CATEGORIES = frozenset(
    {
        ErrorCategory.AUTHENTICATION,
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.TIMEOUT,
        ErrorCategory.TEMPORARY,
        ErrorCategory.MALFORMED,
    }
)


class MicaAppError(Exception):
    """Base class for ``MicaApp`` application-layer errors."""


class AppBusyError(MicaAppError):
    """Starting a new run while this app already has an active run."""


class AppClosedError(MicaAppError):
    """Starting a run after the app has been closed."""


class MicaApp:
    """Wire the session service, context builder, engine, and provider together."""

    def __init__(
        self,
        provider: Provider,
        model: str,
        *,
        session_service: SessionService,
        session_id: str,
        context_builder: ContextBuilder,
        system_prompt: str | None = None,
        context_snapshot: ContextBuildResult | None = None,
        run_id_factory: RunIdFactory | None = None,
        turn_id_factory: TurnIdFactory | None = None,
        checkpoint_id_factory: CheckpointIdFactory | None = None,
        clock: Clock | None = None,
        tool_executor: ToolExecutor | None = None,
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
        if context_snapshot is not None and not isinstance(
            context_snapshot, ContextBuildResult
        ):
            raise TypeError("context_snapshot must be a ContextBuildResult or None")
        resolved_context = (
            context_builder.build() if context_snapshot is None else context_snapshot
        )
        if any(
            message.role is not MessageRole.SYSTEM
            for message in resolved_context.messages
        ):
            raise ValueError("context_snapshot must contain only system messages")
        resolved_system_prompt = (
            build_system_prompt(context_builder.workspace)
            if system_prompt is None
            else system_prompt
        )
        self._model = model
        self._provider = provider
        self._session_service = session_service
        self._session_id = session_id
        self._header_messages = (
            Message(role=MessageRole.SYSTEM, content=resolved_system_prompt),
            *resolved_context.messages,
        )
        self._turn_id_factory = turn_id_factory or (lambda: uuid4().hex)
        self._checkpoint_id_factory = checkpoint_id_factory or (lambda: uuid4().hex)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tool_executor = tool_executor
        self._engine = AgentEngine(
            provider=provider,
            run_id_factory=run_id_factory,
            tool_executor=tool_executor,
            presentation_workspace=context_builder.workspace,
        )
        self._active_run = False
        self._closed = False

    @property
    def session_id(self) -> str:
        """Session id bound to the current app instance."""
        return self._session_id

    @property
    def history(self) -> tuple[Message, ...]:
        """Immutable persisted-history snapshot freshly read from the session service."""
        return self._session_service.context_snapshot(self._session_id)

    def respond_interaction(
        self, response: PermissionResponse | UserInputResponse
    ) -> bool:
        """Submit one frontend-agnostic response to the current tool interaction."""
        if self._tool_executor is None:
            return False
        return self._tool_executor.interaction_broker.respond(response)

    async def aclose(self) -> None:
        """Close the provider and mark the app unusable for runs; repeated calls are safe."""
        if self._closed:
            return
        self._closed = True
        await self._provider.aclose()

    async def run(
        self,
        prompt: str,
        *,
        reasoning_effort: ReasoningEffort = ReasoningEffort.OFF,
        cancellation: CancellationToken | None = None,
    ) -> AsyncIterator[EngineEvent]:
        """Start one run; success commits a ``TurnEntry``, cancellation or recoverable
        failure commits a checkpoint."""
        if self._closed:
            raise AppClosedError("app is closed; cannot start a new run")
        if not isinstance(prompt, str):
            raise TypeError(f"prompt must be a str, got {type(prompt).__name__}")
        if not prompt.strip():
            raise ValueError("prompt must be a non-empty, non-blank string")
        if not isinstance(reasoning_effort, ReasoningEffort):
            raise TypeError("reasoning_effort must be a ReasoningEffort")
        if cancellation is not None and not isinstance(cancellation, CancellationToken):
            raise TypeError("cancellation must be a CancellationToken or None")
        if self._active_run:
            raise AppBusyError("another run is already active")

        self._active_run = True
        user_message = Message(role=MessageRole.USER, content=prompt)
        completed_messages: list[tuple[Message, ...]] = []
        completed_tool_presentations: list[tuple[ToolPresentationSnapshot, ...]] = []
        progress_snapshots: list[RunProgressSnapshot] = []
        submitted = False
        try:
            session_messages = self._session_service.context_snapshot(self._session_id)
            request = ProviderRequest(
                model=self._model,
                messages=self._header_messages + session_messages + (user_message,),
                tools=(
                    self._tool_executor.tool_specs
                    if self._tool_executor is not None
                    else ()
                ),
                reasoning_effort=reasoning_effort,
            )
            async with aclosing(
                self._engine.run(
                    request,
                    cancellation=cancellation,
                    completed_messages_sink=completed_messages.append,
                    completed_tool_presentations_sink=completed_tool_presentations.append,
                    progress_sink=progress_snapshots.append,
                )
            ) as events:
                async for event in events:
                    if isinstance(event, RunCompleted):
                        if not self._commit_completed_turn(
                            event,
                            user_message,
                            completed_messages,
                            completed_tool_presentations,
                        ):
                            yield RunFailed(
                                run_id=event.run_id,
                                seq=event.seq,
                                category=ErrorCategory.STORAGE,
                                message=self._storage_failure_message(
                                    completed_messages, progress_snapshots
                                ),
                            )
                            return
                        submitted = True
                    elif isinstance(event, (RunCancelled, RunFailed)):
                        kind = self._checkpoint_kind(event)
                        if kind is not None:
                            if not self._commit_checkpoint(
                                event, user_message, progress_snapshots, kind
                            ):
                                yield RunFailed(
                                    run_id=event.run_id,
                                    seq=event.seq,
                                    category=ErrorCategory.STORAGE,
                                    message=self._storage_failure_message(
                                        completed_messages, progress_snapshots
                                    ),
                                )
                                return
                            submitted = True
                    yield event
        except (asyncio.CancelledError, GeneratorExit):
            if not submitted and progress_snapshots:
                snapshot = progress_snapshots[-1]
                self._commit_checkpoint(
                    RunCancelled(run_id=snapshot.run_id, seq=0),
                    user_message,
                    progress_snapshots,
                    RunCheckpointKind.CANCELLED,
                )
            raise
        finally:
            self._active_run = False

    @staticmethod
    def _checkpoint_kind(event: RunCancelled | RunFailed) -> RunCheckpointKind | None:
        if isinstance(event, RunCancelled):
            return RunCheckpointKind.CANCELLED
        if event.category in _RECOVERABLE_ERROR_CATEGORIES:
            return RunCheckpointKind.RECOVERABLE_FAILURE
        return None

    @staticmethod
    def _storage_failure_message(
        completed_messages: list[tuple[Message, ...]],
        progress_snapshots: list[RunProgressSnapshot] | None = None,
    ) -> str:
        if completed_messages:
            for message in completed_messages[0]:
                if any(
                    call.name in _SIDE_EFFECT_TOOL_NAMES for call in message.tool_calls
                ):
                    return _STORAGE_FAILURE_AFTER_TOOL_MESSAGE
        if progress_snapshots and progress_snapshots[-1].side_effect_tool_started:
            return _STORAGE_FAILURE_AFTER_TOOL_MESSAGE
        return _STORAGE_FAILURE_MESSAGE

    def _commit_completed_turn(
        self,
        event: RunCompleted,
        user_message: Message,
        completed_messages: list[tuple[Message, ...]],
        completed_tool_presentations: list[tuple[ToolPresentationSnapshot, ...]],
    ) -> bool:
        try:
            if (
                len(completed_messages) != 1
                or not completed_messages[0]
                or len(completed_tool_presentations) != 1
            ):
                return False
            transcript = completed_messages[0]
            presentations = completed_tool_presentations[0]
            turn = TurnEntry(
                turn_id=self._turn_id_factory(),
                run_id=event.run_id,
                user_message=user_message,
                assistant_message=transcript[-1],
                completed_at=self._clock(),
                intermediate_messages=transcript[:-1],
                tool_presentations=presentations,
            )
            self._session_service.commit_turn(self._session_id, turn)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - persistence boundary is intentionally safe
            return False
        return True

    def _commit_checkpoint(
        self,
        event: RunCancelled | RunFailed,
        user_message: Message,
        progress_snapshots: list[RunProgressSnapshot],
        kind: RunCheckpointKind,
    ) -> bool:
        try:
            snapshot = progress_snapshots[-1] if progress_snapshots else None
            if snapshot is None or snapshot.run_id != event.run_id:
                snapshot = RunProgressSnapshot(event.run_id, ())
            checkpoint = RunCheckpoint(
                checkpoint_id=self._checkpoint_id_factory(),
                run_id=event.run_id,
                kind=kind,
                user_message=user_message,
                recorded_at=self._clock(),
                progress=snapshot.progress,
                partial_assistant_message=snapshot.partial_assistant_message,
            )
            self._session_service.commit_checkpoint(self._session_id, checkpoint)
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
