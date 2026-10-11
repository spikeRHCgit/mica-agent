"""``AgentEngine``: asynchronous orchestration of a single run.

The engine depends only on the provider and the optional ``ToolExecutor`` public
contracts; it reads no environment variables, configuration, or CLI state. It maps
provider events into engine events carrying ``run_id`` and a strict ``seq``, and
guarantees exactly one terminal event.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, replace

from .cancellation import CancellationRequested, CancellationToken
from .checkpoint_contracts import (
    RunCheckpointProgress,
    RunCheckpointToolProgress,
    RunCheckpointToolStatus,
)
from .continuation_contracts import (
    ContinuationStatus,
    ModelStepContinuation,
    apply_turn_size_limit,
    check_step_association,
)
from .events import (
    EngineEvent,
    ErrorCategory,
    PermissionRequested,
    ReasoningDelta,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
    ToolFinished,
    ToolOutputDelta,
    ToolPrepared,
    ToolStarted,
    UserInputRequested,
)
from .interactions import InteractionKind, InteractionRequest
from .message_sanitizer import sanitize_step_continuation, sanitize_tool_message
from .models import Message, MessageRole
from .providers import (
    AuthenticationError,
    Finish,
    MalformedResponseError,
    Provider,
    ProviderError,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderReasoningDelta,
    ProviderRequest,
    ProviderTimeoutError,
    RateLimitError,
    TemporaryServerError,
)
from .providers import TextDelta as ProviderTextDelta
from .tool_contracts import ToolRequest, ToolResult, ToolResultStatus, to_mutable_json
from .tool_executor import ToolExecutor
from .tool_presentation import (
    ToolPresentationSnapshot,
    complete_tool_presentation,
    prepare_tool_presentation,
)
from .workspace import WorkspaceIdentity

RunIdFactory = Callable[[], str]
CompletedMessagesSink = Callable[[tuple[Message, ...]], None]
CompletedContinuationsSink = Callable[[tuple[ModelStepContinuation | None, ...]], None]
CompletedToolPresentationsSink = Callable[[tuple[ToolPresentationSnapshot, ...]], None]
DEFAULT_MAX_MODEL_STEPS = 8
DEFAULT_MAX_TOOL_CALLS = 32
_SIDE_EFFECT_TOOL_NAMES = frozenset({"write", "edit", "bash"})
_SIDE_EFFECT_WARNING = "tool changes may have occurred; check the workspace"
_RECOVERABLE_ERROR_CATEGORIES = frozenset(
    {
        ErrorCategory.AUTHENTICATION,
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.TIMEOUT,
        ErrorCategory.TEMPORARY,
        ErrorCategory.MALFORMED,
    }
)


@dataclass(frozen=True, slots=True)
class RunProgressSnapshot:
    """True progress snapshot of an unfinished run; not a successful turn."""

    run_id: str
    progress: tuple[RunCheckpointProgress, ...]
    partial_assistant_message: Message | None = None
    side_effect_tool_started: bool = False


ProgressSink = Callable[[RunProgressSnapshot], None]


class _ToolSignal:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: object) -> None:
        self.kind = kind
        self.value = value


def _default_run_id() -> str:
    """Default run id: a non-empty unique identifier from the standard library."""
    return uuid.uuid4().hex


class AgentEngine:
    """Consume the provider stream and emit ordered engine events.

    Each :meth:`run` produces one run: it starts with ``RunStarted(seq=0)``, then text
    deltas, and exactly one terminal event (``RunCompleted`` / ``RunFailed`` /
    ``RunCancelled``); ``seq`` increases strictly. Consumption of the provider stream
    stops immediately after the terminal event, so no second terminal event appears.
    """

    def __init__(
        self,
        provider: Provider,
        *,
        run_id_factory: RunIdFactory | None = None,
        tool_executor: ToolExecutor | None = None,
        presentation_workspace: WorkspaceIdentity | None = None,
        max_model_steps: int = DEFAULT_MAX_MODEL_STEPS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    ) -> None:
        if isinstance(max_model_steps, bool) or not isinstance(max_model_steps, int):
            raise TypeError("max_model_steps must be a positive integer")
        if max_model_steps <= 0:
            raise ValueError("max_model_steps must be a positive integer")
        if isinstance(max_tool_calls, bool) or not isinstance(max_tool_calls, int):
            raise TypeError("max_tool_calls must be a positive integer")
        if max_tool_calls <= 0:
            raise ValueError("max_tool_calls must be a positive integer")
        if tool_executor is not None and not isinstance(tool_executor, ToolExecutor):
            raise TypeError("tool_executor must be a ToolExecutor or None")
        if presentation_workspace is not None and not isinstance(
            presentation_workspace, WorkspaceIdentity
        ):
            raise TypeError(
                "presentation_workspace must be a WorkspaceIdentity or None"
            )
        self._provider = provider
        self._run_id_factory = run_id_factory or _default_run_id
        self._tool_executor = tool_executor
        self._presentation_workspace = presentation_workspace
        self._interaction_broker = (
            tool_executor.interaction_broker if tool_executor is not None else None
        )
        self._max_model_steps = max_model_steps
        self._max_tool_calls = max_tool_calls

    async def run(
        self,
        request: ProviderRequest,
        *,
        cancellation: CancellationToken | None = None,
        completed_messages_sink: CompletedMessagesSink | None = None,
        completed_tool_presentations_sink: CompletedToolPresentationsSink | None = None,
        progress_sink: ProgressSink | None = None,
        completed_continuations_sink: CompletedContinuationsSink | None = None,
    ) -> AsyncIterator[EngineEvent]:
        """Run the loop, submitting only this turn's safe continuation slots.

        Completion callbacks run synchronously before the successful terminal
        event. Interrupted progress includes finished steps, never replay data
        for an unfinished provider stream.
        """
        run_id = self._run_id_factory()
        seq = 0
        if completed_messages_sink is not None and not callable(
            completed_messages_sink
        ):
            raise TypeError("completed_messages_sink must be callable or None")
        if completed_tool_presentations_sink is not None and not callable(
            completed_tool_presentations_sink
        ):
            raise TypeError(
                "completed_tool_presentations_sink must be callable or None"
            )
        if progress_sink is not None and not callable(progress_sink):
            raise TypeError("progress_sink must be callable or None")
        if completed_continuations_sink is not None and not callable(
            completed_continuations_sink
        ):
            raise TypeError("completed_continuations_sink must be callable or None")
        token = cancellation or CancellationToken()
        completed_tool_presentations: list[ToolPresentationSnapshot] = []
        yield RunStarted(run_id=run_id)
        seq += 1
        messages = request.messages
        completed_messages: list[Message] = []
        history_continuations = tuple(
            record
            if record is not None
            and request.provider_id is not None
            and record.provider_id == request.provider_id
            and record.model_id == request.model
            and record.api is request.api
            and record.status is ContinuationStatus.REPLAYABLE
            else None
            for record in request.continuations
        )
        completed_continuations: tuple[ModelStepContinuation | None, ...] = ()
        turn_continuations_discarded = False
        side_effect_tool_started = False
        model_steps = 0
        tool_calls_total = 0
        progress_steps: list[RunCheckpointProgress] = []
        current_step_text: list[str] = []
        current_step_tools: list[RunCheckpointToolProgress] = []
        current_step_started = False
        current_call_index = -1
        progress_emitted = False

        def current_partial() -> Message | None:
            if current_step_started or not current_step_text:
                return None
            return Message(MessageRole.ASSISTANT, "".join(current_step_text))

        def snapshot() -> RunProgressSnapshot:
            return RunProgressSnapshot(
                run_id=run_id,
                progress=tuple(progress_steps),
                partial_assistant_message=current_partial(),
                side_effect_tool_started=side_effect_tool_started,
            )

        def emit_progress() -> EngineEvent | None:
            nonlocal progress_emitted
            if progress_sink is None or progress_emitted:
                return None
            progress_emitted = True
            try:
                progress_sink(snapshot())
            except Exception:  # noqa: BLE001 - safe progress boundary
                return RunFailed(
                    run_id=run_id,
                    seq=seq,
                    category=ErrorCategory.UNKNOWN,
                    message=self._failure_message(
                        "progress snapshot handling failed",
                        side_effect_tool_started,
                    ),
                )
            return None

        def fail(category: ErrorCategory, message: str) -> EngineEvent:
            if category in _RECOVERABLE_ERROR_CATEGORIES:
                extra = emit_progress()
                if extra is not None:
                    return extra
            return RunFailed(
                run_id=run_id,
                seq=seq,
                category=category,
                message=self._failure_message(message, side_effect_tool_started),
            )

        try:
            token.raise_if_cancelled()
            while True:
                token.raise_if_cancelled()
                if model_steps >= self._max_model_steps:
                    yield fail(ErrorCategory.LIMIT, "model step limit reached")
                    return
                model_steps += 1
                current_step_text = []
                current_step_tools = []
                current_step_started = False
                current_call_index = -1
                finish: Finish | None = None
                current_request = ProviderRequest(
                    model=request.model,
                    messages=messages,
                    tools=request.tools,
                    reasoning_effort=request.reasoning_effort,
                    max_tokens=request.max_tokens,
                    thinking_level_map=request.thinking_level_map,
                    provider_id=request.provider_id,
                    api=request.api,
                    continuations=history_continuations + completed_continuations,
                )
                async with aclosing(
                    self._provider_events(current_request, token)
                ) as provider_events:
                    async for provider_event in provider_events:
                        if isinstance(provider_event, ProviderReasoningDelta):
                            yield ReasoningDelta(
                                run_id=run_id, seq=seq, text=provider_event.text
                            )
                            seq += 1
                        elif isinstance(provider_event, ProviderTextDelta):
                            if not isinstance(provider_event.text, str):
                                yield fail(
                                    ErrorCategory.MALFORMED,
                                    "provider produced a non-string text delta",
                                )
                                return
                            current_step_text.append(provider_event.text)
                            yield TextDelta(
                                run_id=run_id, seq=seq, text=provider_event.text
                            )
                            seq += 1
                        elif isinstance(provider_event, Finish):
                            finish = provider_event
                            break
                        elif isinstance(provider_event, ProviderErrorEvent):
                            yield fail(
                                self._map_error(provider_event.error),
                                self._error_message(provider_event.error),
                            )
                            return
                        else:
                            yield fail(
                                ErrorCategory.UNKNOWN,
                                "provider produced an unknown event",
                            )
                            return
                if finish is None:
                    yield fail(
                        ErrorCategory.MALFORMED,
                        "provider stream ended without a terminal event",
                    )
                    return
                original_assistant = Message(
                    MessageRole.ASSISTANT,
                    "".join(current_step_text),
                    finish.tool_calls,
                )
                continuation = finish.continuation
                if continuation is not None:
                    try:
                        if (
                            request.provider_id is None
                            or continuation.provider_id != request.provider_id
                            or continuation.model_id != request.model
                            or continuation.api is not request.api
                        ):
                            raise ValueError("continuation binding mismatch")
                        safe_continuation = sanitize_step_continuation(
                            continuation, original_assistant
                        )
                        check_step_association(continuation, original_assistant)
                        continuation = safe_continuation
                    except (TypeError, ValueError):
                        yield fail(
                            ErrorCategory.MALFORMED,
                            "provider produced an invalid continuation",
                        )
                        return
                    if turn_continuations_discarded:
                        continuation = replace(
                            continuation,
                            status=ContinuationStatus.UNREPLAYABLE,
                            items=(),
                        )
                candidates = completed_continuations + (continuation,)
                completed_continuations = apply_turn_size_limit(candidates)
                if completed_continuations != candidates:
                    turn_continuations_discarded = True
                for index, step in enumerate(progress_steps):
                    progress_steps[index] = replace(
                        step, continuation=completed_continuations[index]
                    )
                if not finish.tool_calls:
                    completed_messages.append(original_assistant)
                    if completed_messages_sink is not None:
                        try:
                            completed_messages_sink(tuple(completed_messages))
                        except Exception:  # noqa: BLE001 - safe completion boundary
                            yield RunFailed(
                                run_id=run_id,
                                seq=seq,
                                category=ErrorCategory.UNKNOWN,
                                message=self._failure_message(
                                    "completed message handling failed",
                                    side_effect_tool_started,
                                ),
                            )
                            return
                    if completed_tool_presentations_sink is not None:
                        try:
                            completed_tool_presentations_sink(
                                tuple(completed_tool_presentations)
                            )
                        except Exception:  # noqa: BLE001 - safe completion boundary
                            yield RunFailed(
                                run_id=run_id,
                                seq=seq,
                                category=ErrorCategory.UNKNOWN,
                                message=self._failure_message(
                                    "completed tool presentation handling failed",
                                    side_effect_tool_started,
                                ),
                            )
                            return
                    if completed_continuations_sink is not None:
                        try:
                            completed_continuations_sink(completed_continuations)
                        except Exception:  # noqa: BLE001 - safe completion boundary
                            yield RunFailed(
                                run_id=run_id,
                                seq=seq,
                                category=ErrorCategory.UNKNOWN,
                                message=self._failure_message(
                                    "completed continuation handling failed",
                                    side_effect_tool_started,
                                ),
                            )
                            return
                    yield RunCompleted(run_id=run_id, seq=seq)
                    return
                if self._tool_executor is None:
                    yield fail(ErrorCategory.UNKNOWN, "tool execution is unavailable")
                    return
                batch_size = len(finish.tool_calls)
                if tool_calls_total + batch_size > self._max_tool_calls:
                    yield fail(ErrorCategory.LIMIT, "tool call limit reached")
                    return
                tool_calls_total += batch_size
                assistant_message = sanitize_tool_message(original_assistant)
                messages = messages + (assistant_message,)
                completed_messages.append(assistant_message)
                current_step_started = True
                current_step_tools = [
                    RunCheckpointToolProgress(
                        sanitized_call, RunCheckpointToolStatus.NOT_STARTED
                    )
                    for sanitized_call in assistant_message.tool_calls
                ]
                progress_steps.append(
                    RunCheckpointProgress(
                        assistant_message=Message(
                            MessageRole.ASSISTANT, assistant_message.content
                        )
                        if assistant_message.content
                        else None,
                        tools=tuple(current_step_tools),
                        continuation=completed_continuations[-1],
                    )
                )
                for current_call_index, (call, sanitized_call) in enumerate(
                    zip(
                        original_assistant.tool_calls,
                        assistant_message.tool_calls,
                        strict=True,
                    )
                ):
                    tool_request = ToolRequest(call.call_id, call.name, call.arguments)
                    yield ToolPrepared(
                        run_id=run_id,
                        seq=seq,
                        presentation=prepare_tool_presentation(
                            tool_request, workspace=self._presentation_workspace
                        ),
                    )
                    seq += 1
                    result: ToolResult | None = None
                    final_presentation: ToolPresentationSnapshot | None = None
                    async with aclosing(
                        self._tool_execution_signals(tool_request, token)
                    ) as tool_signals:
                        async for signal in tool_signals:
                            if signal.kind == "interaction":
                                interaction = signal.value
                                assert isinstance(interaction, InteractionRequest)
                                if interaction.kind is InteractionKind.PERMISSION:
                                    yield PermissionRequested(
                                        run_id=run_id,
                                        seq=seq,
                                        interaction_id=interaction.interaction_id,
                                        call_id=interaction.call_id,
                                        tool_name=interaction.tool_name,
                                        message=interaction.message,
                                    )
                                else:
                                    assert interaction.ask_prompt is not None
                                    yield UserInputRequested(
                                        run_id=run_id,
                                        seq=seq,
                                        interaction_id=interaction.interaction_id,
                                        question=interaction.message,
                                        prompt=interaction.ask_prompt,
                                    )
                                seq += 1
                            elif signal.kind == "output":
                                call_id, tool_name, text = signal.value
                                yield ToolOutputDelta(
                                    run_id=run_id,
                                    seq=seq,
                                    call_id=call_id,
                                    tool_name=tool_name,
                                    text=text,
                                )
                                seq += 1
                            elif signal.kind == "started":
                                started = signal.value
                                assert isinstance(started, ToolRequest)
                                if started.name in _SIDE_EFFECT_TOOL_NAMES:
                                    side_effect_tool_started = True
                                current_step_tools[current_call_index] = (
                                    RunCheckpointToolProgress(
                                        sanitized_call,
                                        RunCheckpointToolStatus.IN_PROGRESS,
                                    )
                                )
                                progress_steps[-1] = RunCheckpointProgress(
                                    assistant_message=progress_steps[
                                        -1
                                    ].assistant_message,
                                    tools=tuple(current_step_tools),
                                    continuation=progress_steps[-1].continuation,
                                )
                                yield ToolStarted(
                                    run_id=run_id,
                                    seq=seq,
                                    call_id=started.call_id,
                                    tool_name=started.name,
                                )
                                seq += 1
                            elif signal.kind == "finished":
                                finished = signal.value
                                assert isinstance(finished, ToolResult)
                                final_presentation = complete_tool_presentation(
                                    tool_request,
                                    finished,
                                    detail=(
                                        finished.presentation
                                        if isinstance(finished.presentation, Mapping)
                                        else None
                                    ),
                                    workspace=self._presentation_workspace,
                                )
                                completed_tool_presentations.append(final_presentation)
                                yield ToolFinished(
                                    run_id=run_id,
                                    seq=seq,
                                    call_id=finished.call_id,
                                    tool_name=finished.tool_name,
                                    status=finished.status,
                                    presentation=final_presentation,
                                )
                                seq += 1
                            elif signal.kind == "result":
                                result = signal.value
                                assert isinstance(result, ToolResult)
                    if result is None:
                        yield fail(ErrorCategory.UNKNOWN, "tool execution failed")
                        return
                    if result.status is ToolResultStatus.CANCELLED:
                        extra = emit_progress()
                        if extra is not None:
                            yield extra
                            return
                        yield RunCancelled(run_id=run_id, seq=seq)
                        return
                    tool_message = sanitize_tool_message(
                        Message(
                            role=MessageRole.TOOL,
                            content=self._tool_result_message(result),
                            tool_call_id=call.call_id,
                        )
                    )
                    messages = messages + (tool_message,)
                    completed_messages.append(tool_message)
                    status = (
                        RunCheckpointToolStatus.SUCCESS
                        if result.status is ToolResultStatus.SUCCESS
                        else RunCheckpointToolStatus.FAILED
                    )
                    current_step_tools[current_call_index] = RunCheckpointToolProgress(
                        sanitized_call,
                        status,
                        tool_message,
                        final_presentation,
                    )
                    progress_steps[-1] = RunCheckpointProgress(
                        assistant_message=progress_steps[-1].assistant_message,
                        tools=tuple(current_step_tools),
                        continuation=progress_steps[-1].continuation,
                    )
        except CancellationRequested:
            extra = emit_progress()
            if extra is not None:
                yield extra
                return
            yield RunCancelled(run_id=run_id, seq=seq)
            return
        except asyncio.CancelledError:
            extra = emit_progress()
            if extra is not None:
                yield extra
                raise
            yield RunCancelled(run_id=run_id, seq=seq)
            raise
        except GeneratorExit:
            emit_progress()
            raise
        except Exception:  # noqa: BLE001 - safe Engine boundary
            yield fail(ErrorCategory.UNKNOWN, "unexpected engine error")
            return
        finally:
            if self._interaction_broker is not None:
                self._interaction_broker.clear()

    async def _provider_events(
        self,
        request: ProviderRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ProviderEvent]:
        stream = self._provider.stream(request)
        try:
            while True:
                cancellation.raise_if_cancelled()
                next_task = asyncio.create_task(stream.__anext__())
                cancellation_task = asyncio.create_task(cancellation.wait())
                try:
                    done, _ = await asyncio.wait(
                        {next_task, cancellation_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancellation_task in done:
                        next_task.cancel()
                        await asyncio.gather(next_task, return_exceptions=True)
                        raise CancellationRequested("operation cancelled")
                    try:
                        event = next_task.result()
                    except StopAsyncIteration:
                        return
                    yield event
                finally:
                    if not next_task.done():
                        next_task.cancel()
                    await asyncio.gather(next_task, return_exceptions=True)
                    if not cancellation_task.done():
                        cancellation_task.cancel()
                    await asyncio.gather(cancellation_task, return_exceptions=True)
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    async def _tool_execution_signals(
        self,
        request: ToolRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[_ToolSignal]:
        assert self._tool_executor is not None
        signals: asyncio.Queue[_ToolSignal] = asyncio.Queue()

        def started(value: ToolRequest) -> None:
            signals.put_nowait(_ToolSignal("started", value))

        def finished(value: ToolResult) -> None:
            signals.put_nowait(_ToolSignal("finished", value))

        def output(call_id: str, tool_name: str, text: str) -> None:
            signals.put_nowait(_ToolSignal("output", (call_id, tool_name, text)))

        task = asyncio.create_task(
            self._tool_executor.execute(
                request,
                cancellation,
                on_started=started,
                on_finished=finished,
                on_output=output,
            )
        )
        interaction_task: asyncio.Task[InteractionRequest] | None = None
        signal_task: asyncio.Task[_ToolSignal] | None = None
        try:
            while True:
                if signal_task is None:
                    signal_task = asyncio.create_task(signals.get())
                if interaction_task is None and self._interaction_broker is not None:
                    interaction_task = asyncio.create_task(
                        self._interaction_broker.next_request()
                    )
                waiting: set[asyncio.Task[object]] = {task}  # type: ignore[type-var]
                waiting.add(signal_task)  # type: ignore[arg-type]
                if interaction_task is not None:
                    waiting.add(interaction_task)  # type: ignore[arg-type]
                done, _ = await asyncio.wait(
                    waiting, return_when=asyncio.FIRST_COMPLETED
                )
                if signal_task in done:
                    signal = signal_task.result()
                    signal_task = None
                    yield signal
                if interaction_task is not None and interaction_task in done:
                    interaction = interaction_task.result()
                    interaction_task = None
                    yield _ToolSignal("interaction", interaction)
                if task in done:
                    result = task.result()
                    while True:
                        try:
                            yield signals.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    yield _ToolSignal("result", result)
                    return
        finally:
            for pending in (signal_task, interaction_task):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(
                *(pending for pending in (signal_task, interaction_task) if pending),
                return_exceptions=True,
            )
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self._interaction_broker is not None and task.cancelled():
                self._interaction_broker.clear()

    @staticmethod
    def _tool_result_message(result: ToolResult) -> str:
        data = to_mutable_json(result.data) if result.data is not None else None
        return json.dumps(
            {
                "status": result.status.value,
                "message": result.message,
                "data": data,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _failure_message(message: str, side_effect_tool_started: bool) -> str:
        if side_effect_tool_started:
            return f"{message}; {_SIDE_EFFECT_WARNING}"
        return message

    @staticmethod
    def _map_error(error: object) -> ErrorCategory:
        """Map a ``ProviderError`` to a unified error category; anything else is UNKNOWN."""
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
        """User-facing diagnostic built only from the sanitized public string of a
        ``ProviderError``.

        Empty and blank strings become a fixed safe message so that
        ``RunFailed.message`` is never blank; the error category is unaffected.
        """
        if isinstance(error, ProviderError):
            message = str(error)
            return message if message.strip() else "provider error"
        return "provider produced an unknown error"
