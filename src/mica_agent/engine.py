"""AgentEngine：单次 Run 的异步编排（P2-03）。

Engine 只依赖 Provider 与可选的 ToolExecutor 公共契约，不读取
环境变量、配置或 CLI 状态；把 Provider 事件映射为带 ``run_id``
与严格 ``seq`` 的 Engine 事件，并保证恰好一个终态。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing

from .cancellation import CancellationRequested, CancellationToken
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
    ToolPrepared,
    ToolStarted,
    UserInputRequested,
)
from .interactions import InteractionKind, InteractionRequest
from .message_sanitizer import sanitize_tool_message
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
CompletedToolPresentationsSink = Callable[[tuple[ToolPresentationSnapshot, ...]], None]
DEFAULT_MAX_MODEL_STEPS = 8
DEFAULT_MAX_TOOL_CALLS = 32
_SIDE_EFFECT_TOOL_NAMES = frozenset({"write", "edit", "bash"})
_SIDE_EFFECT_WARNING = "tool changes may have occurred; check the workspace"


class _ToolSignal:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: object) -> None:
        self.kind = kind
        self.value = value


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
    ) -> AsyncIterator[EngineEvent]:
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
        token = cancellation or CancellationToken()
        completed_tool_presentations: list[ToolPresentationSnapshot] = []
        yield RunStarted(run_id=run_id)
        seq += 1
        messages = request.messages
        completed_messages: list[Message] = []
        side_effect_tool_started = False
        model_steps = 0
        tool_calls_total = 0
        try:
            token.raise_if_cancelled()
            while True:
                token.raise_if_cancelled()
                if model_steps >= self._max_model_steps:
                    yield RunFailed(
                        run_id=run_id,
                        seq=seq,
                        category=ErrorCategory.LIMIT,
                        message=self._failure_message(
                            "model step limit reached", side_effect_tool_started
                        ),
                    )
                    return
                model_steps += 1
                step_text: list[str] = []
                finish: Finish | None = None
                current_request = ProviderRequest(
                    model=request.model,
                    messages=messages,
                    tools=request.tools,
                    reasoning_effort=request.reasoning_effort,
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
                                yield RunFailed(
                                    run_id=run_id,
                                    seq=seq,
                                    category=ErrorCategory.MALFORMED,
                                    message=self._failure_message(
                                        "provider produced a non-string text delta",
                                        side_effect_tool_started,
                                    ),
                                )
                                return
                            step_text.append(provider_event.text)
                            yield TextDelta(
                                run_id=run_id, seq=seq, text=provider_event.text
                            )
                            seq += 1
                        elif isinstance(provider_event, Finish):
                            finish = provider_event
                            break
                        elif isinstance(provider_event, ProviderErrorEvent):
                            yield RunFailed(
                                run_id=run_id,
                                seq=seq,
                                category=self._map_error(provider_event.error),
                                message=self._failure_message(
                                    self._error_message(provider_event.error),
                                    side_effect_tool_started,
                                ),
                            )
                            return
                        else:
                            yield RunFailed(
                                run_id=run_id,
                                seq=seq,
                                category=ErrorCategory.UNKNOWN,
                                message=self._failure_message(
                                    "provider produced an unknown event",
                                    side_effect_tool_started,
                                ),
                            )
                            return
                if finish is None:
                    yield RunFailed(
                        run_id=run_id,
                        seq=seq,
                        category=ErrorCategory.MALFORMED,
                        message=self._failure_message(
                            "provider stream ended without a terminal event",
                            side_effect_tool_started,
                        ),
                    )
                    return
                if not finish.tool_calls:
                    completed_messages.append(
                        Message(
                            role=MessageRole.ASSISTANT,
                            content="".join(step_text),
                        )
                    )
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
                    yield RunCompleted(run_id=run_id, seq=seq)
                    return
                if self._tool_executor is None:
                    yield RunFailed(
                        run_id=run_id,
                        seq=seq,
                        category=ErrorCategory.UNKNOWN,
                        message=self._failure_message(
                            "tool execution is unavailable", side_effect_tool_started
                        ),
                    )
                    return
                batch_size = len(finish.tool_calls)
                if tool_calls_total + batch_size > self._max_tool_calls:
                    yield RunFailed(
                        run_id=run_id,
                        seq=seq,
                        category=ErrorCategory.LIMIT,
                        message=self._failure_message(
                            "tool call limit reached", side_effect_tool_started
                        ),
                    )
                    return
                tool_calls_total += batch_size
                assistant_message = sanitize_tool_message(
                    Message(
                        role=MessageRole.ASSISTANT,
                        content="".join(step_text),
                        tool_calls=finish.tool_calls,
                    )
                )
                messages = messages + (assistant_message,)
                completed_messages.append(assistant_message)
                for call in finish.tool_calls:
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
                                    yield UserInputRequested(
                                        run_id=run_id,
                                        seq=seq,
                                        interaction_id=interaction.interaction_id,
                                        question=interaction.message,
                                    )
                                seq += 1
                            elif signal.kind == "started":
                                started = signal.value
                                assert isinstance(started, ToolRequest)
                                if started.name in _SIDE_EFFECT_TOOL_NAMES:
                                    side_effect_tool_started = True
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
                        yield RunFailed(
                            run_id=run_id,
                            seq=seq,
                            category=ErrorCategory.UNKNOWN,
                            message=self._failure_message(
                                "tool execution failed", side_effect_tool_started
                            ),
                        )
                        return
                    if result.status is ToolResultStatus.CANCELLED:
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
        except CancellationRequested:
            yield RunCancelled(run_id=run_id, seq=seq)
            return
        except asyncio.CancelledError:
            yield RunCancelled(run_id=run_id, seq=seq)
            raise
        except Exception:  # noqa: BLE001 - safe Engine boundary
            yield RunFailed(
                run_id=run_id,
                seq=seq,
                category=ErrorCategory.UNKNOWN,
                message=self._failure_message(
                    "unexpected engine error", side_effect_tool_started
                ),
            )
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

        task = asyncio.create_task(
            self._tool_executor.execute(
                request,
                cancellation,
                on_started=started,
                on_finished=finished,
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
