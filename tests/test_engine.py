"""AgentEngine 公共行为测试（P2-03）。

最小 Provider 只存在于本测试文件内，用于固定事件序列、
错误映射和精确取消时机；真实跨层链路在 test_app.py 验证。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mica_agent.cancellation import CancellationToken
from mica_agent.engine import AgentEngine
from mica_agent.events import (
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
from mica_agent.interactions import (
    AskOption,
    AskPrompt,
    InteractionBroker,
    PermissionAnswer,
    PermissionResponse,
    UserInputResponse,
)
from mica_agent.models import Message, MessageRole
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine, PolicyInput, PolicyOperation
from mica_agent.providers import (
    AuthenticationError,
    Finish,
    MalformedResponseError,
    ProviderError,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderReasoningDelta,
    ProviderRequest,
    ProviderTimeoutError,
    RateLimitError,
    TemporaryServerError,
)
from mica_agent.providers import (
    TextDelta as ProviderTextDelta,
)
from mica_agent.reasoning import ReasoningEffort
from mica_agent.runtime_tools import register_runtime_tools
from mica_agent.tool_contracts import (
    ToolCall,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationStatus,
    complete_tool_presentation,
    prepare_tool_presentation,
)
from mica_agent.tool_registry import ToolRegistry
from mica_agent.workspace import resolve_workspace


def _request() -> ProviderRequest:
    return ProviderRequest(
        model="test-model",
        messages=(Message(role=MessageRole.USER, content="hi"),),
    )


class _StreamingProvider:
    """按给定列表产出 Provider 事件的最小实现（本文件专用）。"""

    def __init__(self, events: list[ProviderEvent]) -> None:
        self._events = events
        self.consumed = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        for event in self._events:
            self.consumed += 1
            yield event


def _engine(events: list[ProviderEvent], *, run_id: str = "run-test") -> AgentEngine:
    return AgentEngine(
        provider=_StreamingProvider(events),
        run_id_factory=lambda: run_id,
    )


async def _collect(
    engine: AgentEngine,
    request: ProviderRequest,
    collected: list[EngineEvent] | None = None,
) -> list[EngineEvent]:
    if collected is None:
        collected = []
    async for event in engine.run(request):
        collected.append(event)
    return collected


def _run_collect(engine: AgentEngine, request: ProviderRequest) -> list[EngineEvent]:
    return asyncio.run(_collect(engine, request))


class _ScriptedProvider:
    def __init__(self, responses: list[list[ProviderEvent]]) -> None:
        self._responses = responses
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        for event in self._responses[len(self.requests) - 1]:
            yield event


def _tool_spec() -> ToolSpec:
    return ToolSpec(
        "lookup",
        "Look up a value",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )


def _lookup_executor() -> tuple[ToolExecutor, ToolRegistry]:
    registry = ToolRegistry()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        cancellation.raise_if_cancelled()
        return ToolResult(
            request.call_id,
            request.name,
            ToolResultStatus.SUCCESS,
            "lookup complete",
            {"value": "found"},
        )

    registry.register(_tool_spec(), handler)
    return (
        ToolExecutor(
            registry,
            PolicyEngine(PermissionMode.DEFAULT),
            policy_input_factory=lambda request, spec: PolicyInput(
                request.name, PolicyOperation.READ, False, False
            ),
        ),
        registry,
    )


def _runtime_bash_executor(workspace: Path) -> ToolExecutor:
    registry = ToolRegistry()
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT, allow_rules=[lambda _: True]),
    )

    async def requester(
        interaction_id: str, question: str, cancellation: CancellationToken
    ) -> UserInputResponse:
        raise AssertionError("ask must not run in the runtime bash test")

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        requester,
        output_reporter=executor.output_reporter,
    )
    return executor


def test_engine_emits_prepared_before_started_and_final_presentation() -> None:
    call = ToolCall("call-1", "lookup", {"query": "mica"})
    provider = _ScriptedProvider(
        [[Finish(tool_calls=(call,))], [ProviderTextDelta("done"), Finish()]]
    )
    executor, _ = _lookup_executor()
    engine = AgentEngine(
        provider, tool_executor=executor, run_id_factory=lambda: "run-presentation"
    )

    events = _run_collect(
        engine,
        ProviderRequest("test-model", (Message(MessageRole.USER, "inspect"),)),
    )
    tool_events = [
        event
        for event in events
        if isinstance(event, (ToolPrepared, ToolStarted, ToolFinished))
    ]

    assert [type(event) for event in tool_events] == [
        ToolPrepared,
        ToolStarted,
        ToolFinished,
    ]
    prepared = tool_events[0]
    finished = tool_events[-1]
    assert isinstance(prepared, ToolPrepared)
    assert prepared.presentation.kind is ToolPresentationKind.GENERIC
    assert prepared.presentation.status is ToolPresentationStatus.PENDING
    assert isinstance(finished, ToolFinished)
    assert finished.presentation is not None
    assert finished.presentation.status is ToolPresentationStatus.SUCCESS
    assert finished.presentation.call_id == finished.call_id
    assert finished.presentation.tool_name == finished.tool_name


def test_completed_tool_presentations_sink_receives_only_final_snapshots() -> None:
    call = ToolCall("call-1", "lookup", {"query": "mica"})
    provider = _ScriptedProvider(
        [[Finish(tool_calls=(call,))], [ProviderTextDelta("done"), Finish()]]
    )
    executor, _ = _lookup_executor()
    engine = AgentEngine(
        provider, tool_executor=executor, run_id_factory=lambda: "run-sink"
    )
    received: list[tuple[object, ...]] = []

    # Observe the public completion sink at the same boundary as RunCompleted.
    async def collect() -> None:
        async for _ in engine.run(
            ProviderRequest("test-model", (Message(MessageRole.USER, "inspect"),)),
            completed_tool_presentations_sink=received.append,
        ):
            pass

    asyncio.run(collect())
    assert len(received) == 1
    assert len(received[0]) == 1
    assert received[0][0].status is ToolPresentationStatus.SUCCESS


def test_completion_sink_receives_final_assistant_once_before_completion() -> None:
    engine = _engine([ProviderTextDelta("final answer"), Finish()])
    completed: list[tuple[Message, ...]] = []

    async def collect() -> list[EngineEvent]:
        events: list[EngineEvent] = []
        async for event in engine.run(
            _request(), completed_messages_sink=completed.append
        ):
            if isinstance(event, RunCompleted):
                assert completed == [(Message(MessageRole.ASSISTANT, "final answer"),)]
            events.append(event)
        return events

    events = asyncio.run(collect())

    assert isinstance(events[-1], RunCompleted)
    assert completed == [(Message(MessageRole.ASSISTANT, "final answer"),)]


def test_tool_messages_are_redacted_once_for_provider_and_completion_sink() -> None:
    secret_key = "sk-very-sensitive-credential"
    bearer = "Bearer private-access-token"
    call = ToolCall(
        "call-secret",
        "lookup",
        {"query": "mica", "api_key": secret_key, "nested": {"authorization": bearer}},
    )
    spec = ToolSpec("lookup", "Lookup", {"type": "object"})
    registry = ToolRegistry()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return ToolResult(
            request.call_id,
            request.name,
            ToolResultStatus.SUCCESS,
            f"received {secret_key}",
            {"token": bearer},
            {"presentation_only": "presentation-only-marker"},
        )

    registry.register(spec, handler)
    provider = _ScriptedProvider(
        [[Finish(tool_calls=(call,))], [ProviderTextDelta("done"), Finish()]]
    )
    engine = AgentEngine(
        provider,
        tool_executor=ToolExecutor(
            registry,
            PolicyEngine(PermissionMode.DEFAULT),
            policy_input_factory=lambda request, spec: PolicyInput(
                request.name, PolicyOperation.READ, False, False
            ),
        ),
        run_id_factory=lambda: "run-redacted",
    )
    completed: list[tuple[Message, ...]] = []
    request = ProviderRequest(
        "test-model", (Message(MessageRole.USER, "inspect"),), (spec,)
    )

    async def collect() -> list[EngineEvent]:
        events: list[EngineEvent] = []
        async for event in engine.run(
            request, completed_messages_sink=completed.append
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())

    assert isinstance(events[-1], RunCompleted)
    assert len(completed) == 1
    assert provider.requests[1].messages[1:] == completed[0][:-1]
    observed = repr((provider.requests[1], completed))
    assert secret_key not in observed
    assert bearer not in observed
    assert "presentation-only-marker" not in observed
    assert "[REDACTED]" in observed
    assert call.arguments["api_key"] == secret_key


def test_completion_sink_failure_is_one_safe_failed_terminal() -> None:
    engine = _engine([ProviderTextDelta("answer"), Finish()])

    def fail_sink(messages: tuple[Message, ...]) -> None:
        raise RuntimeError("sk-private transcript traceback")

    async def collect() -> list[EngineEvent]:
        return [
            event
            async for event in engine.run(_request(), completed_messages_sink=fail_sink)
        ]

    events = asyncio.run(collect())

    assert isinstance(events[-1], RunFailed)
    assert events[-1].message == "completed message handling failed"
    assert not any(isinstance(event, RunCompleted) for event in events)
    assert (
        len([event for event in events if isinstance(event, (RunFailed, RunCancelled))])
        == 1
    )


def test_failed_and_closed_runs_do_not_call_completion_sink() -> None:
    completed: list[tuple[Message, ...]] = []

    async def scenario() -> list[EngineEvent]:
        failed = [
            event
            async for event in _engine(
                [ProviderErrorEvent(AuthenticationError("failed"))]
            ).run(_request(), completed_messages_sink=completed.append)
        ]
        stream = _engine([ProviderTextDelta("partial"), Finish()]).run(
            _request(), completed_messages_sink=completed.append
        )
        await anext(stream)
        await stream.aclose()
        return failed

    failed = asyncio.run(scenario())

    assert isinstance(failed[-1], RunFailed)
    assert completed == []


def test_engine_executes_multiple_tool_calls_in_model_order() -> None:
    order: list[str] = []
    first_spec = ToolSpec("read", "Read", {"type": "object"})
    second_spec = ToolSpec("ls", "List", {"type": "object"})
    registry = ToolRegistry()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        order.append(request.name)
        return ToolResult(
            request.call_id,
            request.name,
            ToolResultStatus.SUCCESS,
            f"{request.name} complete",
        )

    registry.register(first_spec, handler)
    registry.register(second_spec, handler)
    provider = _ScriptedProvider(
        [
            [
                Finish(
                    reason="tool_calls",
                    tool_calls=(
                        ToolCall("call-1", "read", {}),
                        ToolCall("call-2", "ls", {}),
                    ),
                )
            ],
            [ProviderTextDelta("done"), Finish()],
        ]
    )
    request = ProviderRequest(
        "test-model",
        (Message(MessageRole.USER, "inspect"),),
        (first_spec, second_spec),
    )
    engine = AgentEngine(
        provider,
        tool_executor=ToolExecutor(
            registry,
            PolicyEngine(PermissionMode.DEFAULT),
            policy_input_factory=lambda request, spec: PolicyInput(
                request.name, PolicyOperation.READ, False, False
            ),
        ),
        run_id_factory=lambda: "run-order",
    )

    events = _run_collect(engine, request)

    tool_events = [
        event for event in events if isinstance(event, (ToolStarted, ToolFinished))
    ]
    assert [(type(event).__name__, event.call_id) for event in tool_events] == [
        ("ToolStarted", "call-1"),
        ("ToolFinished", "call-1"),
        ("ToolStarted", "call-2"),
        ("ToolFinished", "call-2"),
    ]
    assert order == ["read", "ls"]
    assert isinstance(events[-1], RunCompleted)


def test_provider_failure_after_side_effect_tool_warns_to_check_workspace() -> None:
    spec = ToolSpec("write", "Write", {"type": "object"})
    registry = ToolRegistry()
    side_effect_started = False

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal side_effect_started
        side_effect_started = True
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "write complete"
        )

    registry.register(spec, handler)
    provider = _ScriptedProvider(
        [
            [Finish(tool_calls=(ToolCall("call-write", "write", {}),))],
            [ProviderErrorEvent(AuthenticationError("sk-private-provider-error"))],
        ]
    )
    engine = AgentEngine(
        provider,
        tool_executor=ToolExecutor(
            registry,
            PolicyEngine(PermissionMode.ACCEPT_EDITS),
            policy_input_factory=lambda request, spec: PolicyInput(
                request.name, PolicyOperation.WRITE, False, False
            ),
        ),
        run_id_factory=lambda: "run-side-effect-failure",
    )
    request = ProviderRequest(
        "test-model", (Message(MessageRole.USER, "write"),), (spec,)
    )

    events = _run_collect(engine, request)

    assert side_effect_started is True
    assert isinstance(events[-1], RunFailed)
    assert "check the workspace" in events[-1].message
    assert "sk-private" not in events[-1].message


def test_model_limit_after_side_effect_tool_warns_to_check_workspace() -> None:
    spec = ToolSpec("bash", "Run", {"type": "object"})
    registry = ToolRegistry()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "command complete"
        )

    registry.register(spec, handler)
    engine = AgentEngine(
        _ScriptedProvider([[Finish(tool_calls=(ToolCall("call-bash", "bash", {}),))]]),
        tool_executor=ToolExecutor(
            registry,
            PolicyEngine(PermissionMode.SOLO),
            policy_input_factory=lambda request, spec: PolicyInput(
                request.name, PolicyOperation.COMMAND, False, True
            ),
        ),
        max_model_steps=1,
        run_id_factory=lambda: "run-side-effect-limit",
    )

    events = _run_collect(
        engine,
        ProviderRequest("test-model", (Message(MessageRole.USER, "run"),), (spec,)),
    )

    assert isinstance(events[-1], RunFailed)
    assert events[-1].category is ErrorCategory.LIMIT
    assert "check the workspace" in events[-1].message


def test_tool_failure_is_returned_to_model_without_engine_failure() -> None:
    spec = ToolSpec("read", "Read", {"type": "object"})
    registry = ToolRegistry()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return ToolResult(
            request.call_id,
            request.name,
            ToolResultStatus.EXECUTION_FAILED,
            "safe tool failure",
        )

    registry.register(spec, handler)
    provider = _ScriptedProvider(
        [[Finish(tool_calls=(ToolCall("call-1", "read", {}),))], [Finish()]]
    )
    request = ProviderRequest(
        "test-model", (Message(MessageRole.USER, "inspect"),), (spec,)
    )
    engine = AgentEngine(
        provider,
        tool_executor=ToolExecutor(registry, PolicyEngine(PermissionMode.DEFAULT)),
        run_id_factory=lambda: "run-failure-result",
    )

    events = _run_collect(engine, request)

    assert isinstance(events[-1], RunCompleted)
    assert provider.requests[1].messages[-1].content == (
        '{"status":"execution_failed","message":"safe tool failure","data":null}'
    )


def test_engine_waits_for_permission_response_before_starting_handler() -> None:
    spec = ToolSpec("write", "Write", {"type": "object"})
    registry = ToolRegistry()
    calls: list[str] = []

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        calls.append(request.call_id)
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "saved"
        )

    registry.register(spec, handler)
    broker = InteractionBroker(interaction_id_factory=iter(["permission-1"]).__next__)
    provider = _ScriptedProvider(
        [[Finish(tool_calls=(ToolCall("call-1", "write", {}),))], [Finish()]]
    )
    request = ProviderRequest(
        "test-model", (Message(MessageRole.USER, "save"),), (spec,)
    )
    executor = ToolExecutor(
        registry, PolicyEngine(PermissionMode.DEFAULT), interaction_broker=broker
    )
    engine = AgentEngine(
        provider, tool_executor=executor, run_id_factory=lambda: "run-permission"
    )

    async def scenario() -> list[EngineEvent]:
        events: list[EngineEvent] = []
        async for event in engine.run(request):
            events.append(event)
            if isinstance(event, PermissionRequested):
                assert calls == []
                assert broker.respond(
                    PermissionResponse(
                        event.interaction_id, PermissionAnswer.ALLOW_ONCE
                    )
                )
        return events

    events = asyncio.run(scenario())

    assert calls == ["call-1"]
    permission_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, PermissionRequested)
    )
    started_index = next(
        index for index, event in enumerate(events) if isinstance(event, ToolStarted)
    )
    assert permission_index < started_index
    assert isinstance(events[-1], RunCompleted)


def test_engine_ask_uses_user_input_requester_and_appends_answer() -> None:
    spec = ToolSpec("ask", "Ask", {"type": "object", "required": ["question"]})
    registry = ToolRegistry()
    broker = InteractionBroker()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        prompt = AskPrompt(
            request.arguments["question"],
            "Continue",
            (AskOption("Yes", recommended=True), AskOption("No")),
            allow_custom=True,
        )
        response = await broker(request.call_id, prompt, cancellation)
        return ToolResult(
            request.call_id,
            request.name,
            ToolResultStatus.SUCCESS,
            "answer received",
            {
                "answer": response.answer,
                "selected_option_index": response.selected_option_index,
                "notes": response.notes,
            },
        )

    registry.register(spec, handler)
    provider = _ScriptedProvider(
        [
            [
                Finish(
                    tool_calls=(ToolCall("call-ask", "ask", {"question": "Continue?"}),)
                )
            ],
            [Finish()],
        ]
    )
    request = ProviderRequest(
        "test-model", (Message(MessageRole.USER, "ask"),), (spec,)
    )
    executor = ToolExecutor(
        registry, PolicyEngine(PermissionMode.DEFAULT), interaction_broker=broker
    )
    engine = AgentEngine(
        provider, tool_executor=executor, run_id_factory=lambda: "run-ask"
    )

    async def scenario() -> list[EngineEvent]:
        events: list[EngineEvent] = []
        async for event in engine.run(request):
            events.append(event)
            if isinstance(event, UserInputRequested):
                assert event.question == "Continue?"
                assert event.prompt == AskPrompt(
                    "Continue?",
                    "Continue",
                    (AskOption("Yes", recommended=True), AskOption("No")),
                    allow_custom=True,
                )
                assert broker.respond(
                    UserInputResponse(event.interaction_id, "yes", 0, "confirmed")
                )
        return events

    events = asyncio.run(scenario())

    assert any(isinstance(event, UserInputRequested) for event in events)
    assert isinstance(events[-1], RunCompleted)
    assert provider.requests[1].messages[-1].content == (
        '{"status":"success","message":"answer received","data":'
        '{"answer":"Yes","selected_option_index":0,"notes":"confirmed"}}'
    )


def test_engine_discards_late_interaction_response_after_cancelled_run() -> None:
    spec = ToolSpec("ask", "Ask", {"type": "object"})
    registry = ToolRegistry()
    broker = InteractionBroker()
    provider_calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        response = await broker(request.call_id, "Continue?", cancellation)
        return ToolResult(
            request.call_id,
            request.name,
            ToolResultStatus.SUCCESS,
            "answer received",
            response.answer,
        )

    class Provider:
        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            nonlocal provider_calls
            provider_calls += 1
            if provider_calls <= 2:
                yield Finish(tool_calls=(ToolCall("reused-call", "ask", {}),))
            else:
                yield Finish()

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry, PolicyEngine(PermissionMode.DEFAULT), interaction_broker=broker
    )
    engine = AgentEngine(
        Provider(), tool_executor=executor, run_id_factory=lambda: "run-late-response"
    )
    request = ProviderRequest(
        "test-model", (Message(MessageRole.USER, "ask"),), (spec,)
    )

    async def scenario() -> None:
        cancellation = CancellationToken()
        first_events: list[EngineEvent] = []
        first_interaction_id: str | None = None
        async for event in engine.run(request, cancellation=cancellation):
            first_events.append(event)
            if isinstance(event, UserInputRequested):
                first_interaction_id = event.interaction_id
                cancellation.cancel()
        assert isinstance(first_events[-1], RunCancelled)
        assert first_interaction_id is not None
        assert (
            broker.respond(UserInputResponse(first_interaction_id, "late-old")) is False
        )

        second_events: list[EngineEvent] = []
        second_interaction_id: str | None = None
        async for event in engine.run(request):
            second_events.append(event)
            if isinstance(event, UserInputRequested):
                second_interaction_id = event.interaction_id
                assert second_interaction_id != first_interaction_id
                assert (
                    broker.respond(
                        UserInputResponse(first_interaction_id, "late-old-again")
                    )
                    is False
                )
                assert broker.respond(UserInputResponse(second_interaction_id, "new"))
        assert isinstance(second_events[-1], RunCompleted)

    asyncio.run(scenario())

    call = ToolCall("call-1", "lookup", {"query": "mica"})
    provider = _ScriptedProvider(
        [
            [Finish(reason="tool_calls", tool_calls=(call,))],
            [ProviderTextDelta("final answer"), Finish(reason="stop")],
        ]
    )
    executor, _ = _lookup_executor()
    request = ProviderRequest(
        model="test-model",
        messages=(Message(MessageRole.USER, "find it"),),
        tools=(_tool_spec(),),
    )
    engine = AgentEngine(
        provider,
        tool_executor=executor,
        run_id_factory=lambda: "run-tool",
    )

    events = _run_collect(engine, request)

    assert events == [
        RunStarted("run-tool"),
        ToolPrepared(
            "run-tool",
            1,
            prepare_tool_presentation(
                ToolRequest("call-1", "lookup", {"query": "mica"})
            ),
        ),
        ToolStarted("run-tool", 2, "call-1", "lookup"),
        ToolFinished(
            "run-tool",
            3,
            "call-1",
            "lookup",
            ToolResultStatus.SUCCESS,
            complete_tool_presentation(
                ToolRequest("call-1", "lookup", {"query": "mica"}),
                ToolResult(
                    "call-1",
                    "lookup",
                    ToolResultStatus.SUCCESS,
                    "lookup complete",
                    {"value": "found"},
                ),
            ),
        ),
        TextDelta("run-tool", "final answer", 4),
        RunCompleted("run-tool", 5),
    ]
    assert len(provider.requests) == 2
    assert provider.requests[1].messages == (
        Message(MessageRole.USER, "find it"),
        Message(MessageRole.ASSISTANT, "", (call,)),
        Message(
            MessageRole.TOOL,
            '{"status":"success","message":"lookup complete","data":{"value":"found"}}',
            tool_call_id="call-1",
        ),
    )
    assert provider.requests[1].tools == (_tool_spec(),)


def test_engine_preserves_text_before_tool_call_in_assistant_message() -> None:
    call = ToolCall("call-1", "lookup", {"query": "mica"})
    provider = _ScriptedProvider(
        [[ProviderTextDelta("checking"), Finish(tool_calls=(call,))], [Finish()]]
    )
    executor, _ = _lookup_executor()
    request = ProviderRequest(
        "test-model", (Message(MessageRole.USER, "find"),), (_tool_spec(),)
    )
    engine = AgentEngine(
        provider, tool_executor=executor, run_id_factory=lambda: "run-mixed"
    )

    events = _run_collect(engine, request)

    assert isinstance(events[1], TextDelta)
    assert isinstance(events[-1], RunCompleted)
    assert provider.requests[1].messages[1] == Message(
        MessageRole.ASSISTANT, "checking", (call,)
    )


def test_engine_model_step_limit_stops_before_next_provider_request() -> None:
    spec = ToolSpec("read", "Read", {"type": "object"})
    registry = ToolRegistry()
    calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    registry.register(spec, handler)
    provider = _ScriptedProvider(
        [[Finish(tool_calls=(ToolCall("call-1", "read", {}),))]]
    )
    request = ProviderRequest(
        "test-model", (Message(MessageRole.USER, "read"),), (spec,)
    )
    engine = AgentEngine(
        provider,
        tool_executor=ToolExecutor(registry, PolicyEngine(PermissionMode.DEFAULT)),
        max_model_steps=1,
        run_id_factory=lambda: "run-limit",
    )

    events = _run_collect(engine, request)

    assert len(provider.requests) == 1
    assert calls == 1
    assert isinstance(events[-1], RunFailed)
    assert events[-1].category is ErrorCategory.LIMIT


def test_handler_cancellation_ends_run_without_next_provider_request() -> None:
    spec = ToolSpec("read", "Read", {"type": "object"})
    registry = ToolRegistry()
    entered = asyncio.Event()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        entered.set()
        await cancellation.wait()
        cancellation.raise_if_cancelled()
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "bad"
        )

    registry.register(spec, handler)
    provider = _ScriptedProvider(
        [[Finish(tool_calls=(ToolCall("call-1", "read", {}),))]]
    )
    token = CancellationToken()
    request = ProviderRequest(
        "test-model", (Message(MessageRole.USER, "read"),), (spec,)
    )
    engine = AgentEngine(
        provider,
        tool_executor=ToolExecutor(registry, PolicyEngine(PermissionMode.DEFAULT)),
        run_id_factory=lambda: "run-handler-cancel",
    )

    async def scenario() -> list[EngineEvent]:
        events: list[EngineEvent] = []
        async for event in engine.run(request, cancellation=token):
            events.append(event)
            if isinstance(event, ToolStarted):
                await asyncio.wait_for(entered.wait(), timeout=0.1)
                token.cancel()
        return events

    events = asyncio.run(scenario())

    assert len(provider.requests) == 1
    assert isinstance(events[-1], RunCancelled)
    assert not any(isinstance(event, RunCompleted) for event in events)


def test_engine_tool_batch_limit_rejects_batch_before_any_handler() -> None:
    first = ToolSpec("read", "Read", {"type": "object"})
    second = ToolSpec("ls", "List", {"type": "object"})
    registry = ToolRegistry()
    calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    registry.register(first, handler)
    registry.register(second, handler)
    provider = _ScriptedProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall("call-1", "read", {}),
                        ToolCall("call-2", "ls", {}),
                    )
                )
            ]
        ]
    )
    request = ProviderRequest(
        "test-model", (Message(MessageRole.USER, "read"),), (first, second)
    )
    engine = AgentEngine(
        provider,
        tool_executor=ToolExecutor(registry, PolicyEngine(PermissionMode.DEFAULT)),
        max_tool_calls=1,
        run_id_factory=lambda: "run-tool-limit",
    )

    events = _run_collect(engine, request)

    assert calls == 0
    assert not any(isinstance(event, (ToolStarted, ToolFinished)) for event in events)
    assert isinstance(events[-1], RunFailed)
    assert events[-1].category is ErrorCategory.LIMIT


@pytest.mark.parametrize("field", ["max_model_steps", "max_tool_calls"])
def test_engine_rejects_invalid_limits(field: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        AgentEngine(_StreamingProvider([Finish()]), **{field: 0})
    with pytest.raises((TypeError, ValueError)):
        AgentEngine(_StreamingProvider([Finish()]), **{field: True})


def test_provider_stream_closes_before_normal_finish_returns() -> None:
    closed = False

    class Provider:
        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            nonlocal closed
            try:
                yield Finish(reason="stop")
                await asyncio.sleep(30)
            finally:
                closed = True

    events = _run_collect(
        AgentEngine(Provider(), run_id_factory=lambda: "run-close-normal"), _request()
    )

    assert events[-1] == RunCompleted("run-close-normal", 1)
    assert closed is True


def test_provider_stream_closes_before_tool_loop_starts_next_request() -> None:
    first_closed = False
    handler_started = False

    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            nonlocal first_closed, handler_started
            self.calls += 1
            if self.calls == 2:
                assert first_closed is True
                assert handler_started is True
            try:
                if self.calls == 1:
                    yield Finish(
                        reason="tool_calls",
                        tool_calls=(ToolCall("call-close", "read", {}),),
                    )
                else:
                    yield Finish()
            finally:
                if self.calls == 1:
                    first_closed = True

    spec = ToolSpec("read", "Read", {"type": "object"})
    registry = ToolRegistry()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal handler_started
        handler_started = True
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    registry.register(spec, handler)
    request = ProviderRequest(
        "test-model", (Message(MessageRole.USER, "read"),), (spec,)
    )
    events = _run_collect(
        AgentEngine(
            Provider(),
            tool_executor=ToolExecutor(registry, PolicyEngine(PermissionMode.DEFAULT)),
            run_id_factory=lambda: "run-close-tool",
        ),
        request,
    )

    assert isinstance(events[-1], RunCompleted)
    assert first_closed is True
    assert handler_started is True


def test_provider_stream_closes_before_provider_error_returns() -> None:
    closed = False

    class Provider:
        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            nonlocal closed
            try:
                yield ProviderErrorEvent(MalformedResponseError("malformed"))
                await asyncio.sleep(30)
            finally:
                closed = True

    events = _run_collect(
        AgentEngine(Provider(), run_id_factory=lambda: "run-close-error"), _request()
    )

    assert isinstance(events[-1], RunFailed)
    assert closed is True


def test_reasoning_is_sequenced_but_excluded_from_completed_messages() -> None:
    completed: list[tuple[Message, ...]] = []
    engine = _engine(
        [
            ProviderReasoningDelta("first thought"),
            ProviderReasoningDelta("second thought"),
            ProviderTextDelta("answer"),
            Finish(),
        ]
    )

    async def collect() -> list[EngineEvent]:
        return [
            event
            async for event in engine.run(
                _request(), completed_messages_sink=completed.append
            )
        ]

    events = asyncio.run(collect())

    assert events == [
        RunStarted("run-test"),
        ReasoningDelta("run-test", "first thought", 1),
        ReasoningDelta("run-test", "second thought", 2),
        TextDelta("run-test", "answer", 3),
        RunCompleted("run-test", 4),
    ]
    assert completed == [(Message(MessageRole.ASSISTANT, "answer"),)]


def test_reasoning_then_provider_error_has_one_failed_terminal() -> None:
    completed: list[tuple[Message, ...]] = []
    engine = _engine(
        [
            ProviderReasoningDelta("partial thought"),
            ProviderErrorEvent(MalformedResponseError("bad response")),
            ProviderTextDelta("must not appear"),
            Finish(),
        ]
    )

    async def collect() -> list[EngineEvent]:
        return [
            event
            async for event in engine.run(
                _request(), completed_messages_sink=completed.append
            )
        ]

    events = asyncio.run(collect())

    assert events == [
        RunStarted("run-test"),
        ReasoningDelta("run-test", "partial thought", 1),
        RunFailed("run-test", "bad response", ErrorCategory.MALFORMED, 2),
    ]
    assert completed == []


def test_tool_loop_exposes_each_reasoning_step_without_message_pollution() -> None:
    executor, registry = _lookup_executor()
    call = ToolCall("call-1", "lookup", {"query": "mica"})
    provider = _ScriptedProvider(
        [
            [ProviderReasoningDelta("plan tool"), Finish(tool_calls=(call,))],
            [
                ProviderReasoningDelta("use result"),
                ProviderTextDelta("done"),
                Finish(),
            ],
        ]
    )
    completed: list[tuple[Message, ...]] = []
    request = ProviderRequest(
        "test-model",
        (Message(MessageRole.USER, "inspect"),),
        registry.specs(),
        ReasoningEffort.HIGH,
    )
    engine = AgentEngine(
        provider,
        tool_executor=executor,
        run_id_factory=lambda: "run-reasoning-tools",
    )

    async def collect() -> list[EngineEvent]:
        return [
            event
            async for event in engine.run(
                request, completed_messages_sink=completed.append
            )
        ]

    events = asyncio.run(collect())

    assert [event.text for event in events if isinstance(event, ReasoningDelta)] == [
        "plan tool",
        "use result",
    ]
    assert [request.reasoning_effort for request in provider.requests] == [
        ReasoningEffort.HIGH,
        ReasoningEffort.HIGH,
    ]
    assert [message.role for message in provider.requests[1].messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
    ]
    assert all(
        "plan tool" not in message.content and "use result" not in message.content
        for message in provider.requests[1].messages
    )
    assert completed and [message.role for message in completed[0]] == [
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    assert completed[0][-1].content == "done"


def test_reasoning_then_cancellation_closes_provider_stream() -> None:
    class Provider:
        def __init__(self) -> None:
            self.closed = False

        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            try:
                yield ProviderReasoningDelta("thinking")
                await asyncio.sleep(30)
                yield Finish()
            finally:
                self.closed = True

    async def scenario() -> tuple[list[EngineEvent], bool]:
        provider = Provider()
        token = CancellationToken()
        stream = AgentEngine(
            provider, run_id_factory=lambda: "run-reasoning-cancel"
        ).run(_request(), cancellation=token)
        events = [await anext(stream), await anext(stream)]
        token.cancel()
        events.extend([event async for event in stream])
        return events, provider.closed

    events, closed = asyncio.run(scenario())

    assert events == [
        RunStarted("run-reasoning-cancel"),
        ReasoningDelta("run-reasoning-cancel", "thinking", 1),
        RunCancelled("run-reasoning-cancel", 2),
    ]
    assert closed is True


def test_consumer_closing_after_reasoning_closes_provider_stream() -> None:
    class Provider:
        def __init__(self) -> None:
            self.closed = False

        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            try:
                yield ProviderReasoningDelta("thinking")
                await asyncio.sleep(30)
                yield Finish()
            finally:
                self.closed = True

    async def scenario() -> tuple[list[EngineEvent], bool]:
        provider = Provider()
        stream = AgentEngine(
            provider, run_id_factory=lambda: "run-reasoning-close"
        ).run(_request())
        events = [await anext(stream), await anext(stream)]
        await stream.aclose()
        return events, provider.closed

    events, closed = asyncio.run(scenario())

    assert events == [
        RunStarted("run-reasoning-close"),
        ReasoningDelta("run-reasoning-close", "thinking", 1),
    ]
    assert closed is True


def test_normal_text_run_yields_strictly_sequenced_events() -> None:
    engine = _engine(
        [
            ProviderTextDelta(text="hello"),
            ProviderTextDelta(text=" world"),
            Finish(),
        ]
    )

    events = _run_collect(engine, _request())

    assert events == [
        RunStarted(run_id="run-test"),  # seq 固定 0
        TextDelta(run_id="run-test", text="hello", seq=1),
        TextDelta(run_id="run-test", text=" world", seq=2),
        RunCompleted(run_id="run-test", seq=3),
    ]
    assert [event.seq for event in events] == [0, 1, 2, 3]


def test_run_without_text_goes_straight_from_started_to_completed() -> None:
    engine = _engine([Finish()])

    events = _run_collect(engine, _request())

    assert events == [
        RunStarted(run_id="run-test"),
        RunCompleted(run_id="run-test", seq=1),
    ]


def test_engine_stops_consuming_provider_after_terminal_event() -> None:
    provider = _StreamingProvider(
        [
            ProviderTextDelta(text="done"),
            Finish(),
            # 终态之后的事件必须不被消费。
            ProviderTextDelta(text="ghost"),
        ]
    )
    engine = AgentEngine(provider=provider, run_id_factory=lambda: "run-test")

    events = _run_collect(engine, _request())

    assert provider.consumed == 2  # 只消费到 Finish
    assert events == [
        RunStarted(run_id="run-test"),
        TextDelta(run_id="run-test", text="done", seq=1),
        RunCompleted(run_id="run-test", seq=2),
    ]


def test_every_run_has_exactly_one_terminal_event() -> None:
    scenarios: list[list[ProviderEvent]] = [
        [Finish()],
        [ProviderTextDelta(text="a"), Finish()],
        [ProviderErrorEvent(error=AuthenticationError("authentication failed"))],
    ]

    for events in scenarios:
        collected = _run_collect(_engine(events), _request())
        terminal = [
            event
            for event in collected
            if isinstance(event, (RunCompleted, RunFailed, RunCancelled))
        ]
        assert len(terminal) == 1


def test_cancellation_token_stops_provider_and_emits_one_run_cancelled() -> None:
    class CancellableProvider:
        closed = False

        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            try:
                yield ProviderTextDelta("partial")
                await asyncio.sleep(30)
                yield Finish()
            finally:
                self.closed = True

    provider = CancellableProvider()
    token = CancellationToken()
    engine = AgentEngine(provider, run_id_factory=lambda: "run-token")
    collected: list[EngineEvent] = []
    completed: list[tuple[Message, ...]] = []

    async def scenario() -> None:
        async def consume() -> None:
            async for event in engine.run(
                _request(),
                cancellation=token,
                completed_messages_sink=completed.append,
            ):
                collected.append(event)
                if isinstance(event, TextDelta):
                    token.cancel()

        await asyncio.wait_for(consume(), timeout=0.2)

    asyncio.run(scenario())

    assert [type(event).__name__ for event in collected] == [
        "RunStarted",
        "TextDelta",
        "RunCancelled",
    ]
    assert provider.closed is True
    assert completed == []


def test_permission_wait_cancellation_does_not_start_handler() -> None:
    spec = ToolSpec("write", "Write", {"type": "object"})
    registry = ToolRegistry()
    calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "bad"
        )

    registry.register(spec, handler)
    broker = InteractionBroker()
    token = CancellationToken()
    provider = _ScriptedProvider(
        [[Finish(tool_calls=(ToolCall("call-1", "write", {}),))]]
    )
    engine = AgentEngine(
        provider,
        tool_executor=ToolExecutor(
            registry, PolicyEngine(PermissionMode.DEFAULT), interaction_broker=broker
        ),
        run_id_factory=lambda: "run-permission-cancel",
    )
    request = ProviderRequest(
        "test-model", (Message(MessageRole.USER, "write"),), (spec,)
    )

    async def scenario() -> list[EngineEvent]:
        events: list[EngineEvent] = []
        async for event in engine.run(request, cancellation=token):
            events.append(event)
            if isinstance(event, PermissionRequested):
                token.cancel()
        return events

    events = asyncio.run(scenario())

    assert calls == 0
    assert isinstance(events[-1], RunCancelled)
    assert broker.pending_count == 0


def test_consumer_closing_engine_cleans_provider_stream() -> None:
    closed = False

    async def stream(request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        nonlocal closed
        try:
            yield ProviderTextDelta("partial")
            await asyncio.sleep(30)
        finally:
            closed = True

    class Provider:
        def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
            return stream(request)

    engine = AgentEngine(Provider(), run_id_factory=lambda: "run-close")

    async def scenario() -> None:
        events = engine.run(_request())
        assert isinstance(await anext(events), RunStarted)
        assert isinstance(await anext(events), TextDelta)
        await events.aclose()

    asyncio.run(scenario())

    assert closed is True


@pytest.mark.parametrize(
    ("provider_error", "category"),
    [
        (AuthenticationError("authentication failed"), ErrorCategory.AUTHENTICATION),
        (RateLimitError("rate limited"), ErrorCategory.RATE_LIMIT),
        (ProviderTimeoutError("timed out"), ErrorCategory.TIMEOUT),
        (TemporaryServerError("server hiccup"), ErrorCategory.TEMPORARY),
        (MalformedResponseError("bad chunk"), ErrorCategory.MALFORMED),
    ],
)
def test_classified_provider_errors_map_to_categories(
    provider_error: ProviderError, category: ErrorCategory
) -> None:
    engine = _engine([ProviderErrorEvent(error=provider_error)])

    events = _run_collect(engine, _request())

    assert [type(event).__name__ for event in events] == [
        "RunStarted",
        "RunFailed",
    ]
    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is category
    assert failed.seq == 1
    assert failed.message == str(provider_error)  # 使用已清洗的公开诊断


def test_unknown_provider_error_maps_to_unknown_category() -> None:
    engine = _engine([ProviderErrorEvent(error=ProviderError("mystery"))])

    events = _run_collect(engine, _request())

    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.UNKNOWN
    assert failed.message == "mystery"


def test_non_provider_error_object_maps_to_safe_unknown() -> None:
    engine = _engine([ProviderErrorEvent(error="not an error")])  # type: ignore[arg-type]

    events = _run_collect(engine, _request())

    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.UNKNOWN
    assert "not an error" not in failed.message


def test_credentials_never_leak_through_run_failed_message() -> None:
    error = AuthenticationError(
        "request rejected: Authorization: Bearer sk-live-secret-token"
    )
    engine = _engine([ProviderErrorEvent(error=error)])

    events = _run_collect(engine, _request())

    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert "sk-live-secret-token" not in failed.message
    assert "Bearer" in failed.message  # 保留清洗后的可读诊断


@pytest.mark.parametrize("message", ["", "   ", "\t\n"])
def test_blank_provider_error_message_keeps_original_category(
    message: str,
) -> None:
    # 空/纯空白诊断统一转为固定安全文案，但类别由错误类型决定。
    engine = _engine([ProviderErrorEvent(error=AuthenticationError(message))])

    events = _run_collect(engine, _request())

    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.AUTHENTICATION
    assert failed.message.strip()  # 非空、非纯空白的公开诊断


# ---------------------------------------------------------------------------
# 畸形与未知事件
# ---------------------------------------------------------------------------


def test_unknown_event_object_maps_to_safe_unknown_failure() -> None:
    class WeirdProvider:
        async def stream(self, request: ProviderRequest) -> AsyncIterator[object]:
            yield "not-a-provider-event"

    engine = AgentEngine(provider=WeirdProvider(), run_id_factory=lambda: "run-test")

    events = _run_collect(engine, _request())

    assert [type(event).__name__ for event in events] == [
        "RunStarted",
        "RunFailed",
    ]
    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.UNKNOWN
    assert "not-a-provider-event" not in failed.message


def test_non_string_text_delta_maps_to_malformed() -> None:
    class OddDeltaProvider:
        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            yield ProviderTextDelta(text=123)  # type: ignore[arg-type]
            yield Finish()

    engine = AgentEngine(provider=OddDeltaProvider(), run_id_factory=lambda: "run-test")

    events = _run_collect(engine, _request())

    # 非字符串文本不得构造非法 Engine TextDelta。
    assert [type(event).__name__ for event in events] == [
        "RunStarted",
        "RunFailed",
    ]
    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.MALFORMED
    assert "123" not in failed.message


def test_provider_stream_ending_without_terminal_maps_to_malformed() -> None:
    engine = _engine([ProviderTextDelta(text="partial")])

    events = _run_collect(engine, _request())

    assert [type(event).__name__ for event in events] == [
        "RunStarted",
        "TextDelta",
        "RunFailed",
    ]
    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.MALFORMED
    assert "partial" not in failed.message


# ---------------------------------------------------------------------------
# 未预期异常
# ---------------------------------------------------------------------------


def test_unexpected_provider_exception_maps_to_safe_unknown() -> None:
    class BrokenProvider:
        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            yield ProviderTextDelta(text="partial")
            raise RuntimeError("secret internal detail: sk-abc12345")

    engine = AgentEngine(provider=BrokenProvider(), run_id_factory=lambda: "run-test")

    events = _run_collect(engine, _request())

    assert [type(event).__name__ for event in events] == [
        "RunStarted",
        "TextDelta",
        "RunFailed",
    ]
    failed = events[-1]
    assert isinstance(failed, RunFailed)
    assert failed.category is ErrorCategory.UNKNOWN
    assert "secret internal detail" not in failed.message
    assert "sk-abc12345" not in failed.message
    assert "RuntimeError" not in failed.message
    assert failed.message  # 固定、安全、简短的公开诊断


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


class _HangingProvider:
    """产出首个事件后挂起，等待外部取消。"""

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        yield ProviderTextDelta(text="first")
        await asyncio.sleep(30)
        yield Finish()


def test_external_task_cancellation_after_reasoning_closes_stream() -> None:
    class Provider:
        def __init__(self) -> None:
            self.waiting = asyncio.Event()
            self.closed = False

        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            try:
                yield ProviderReasoningDelta("thinking")
                self.waiting.set()
                await asyncio.sleep(30)
                yield Finish()
            finally:
                self.closed = True

    provider = Provider()
    engine = AgentEngine(provider, run_id_factory=lambda: "run-reasoning-task-cancel")
    collected: list[EngineEvent] = []

    async def consume() -> None:
        async for event in engine.run(_request()):
            collected.append(event)

    async def scenario() -> None:
        task = asyncio.create_task(consume())
        await provider.waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert collected == [
        RunStarted("run-reasoning-task-cancel"),
        ReasoningDelta("run-reasoning-task-cancel", "thinking", 1),
        RunCancelled("run-reasoning-task-cancel", 2),
    ]
    assert provider.closed is True


def test_cancellation_maps_to_single_runcancelled() -> None:
    engine = AgentEngine(provider=_HangingProvider(), run_id_factory=lambda: "run-test")
    collected: list[EngineEvent] = []

    async def consume() -> None:
        async for event in engine.run(_request()):
            collected.append(event)

    async def main() -> None:
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    # 取消映射为唯一 RunCancelled；无 RunFailed / RunCompleted，
    # 且取消后不再追加文本事件。
    assert [type(event).__name__ for event in collected] == [
        "RunStarted",
        "TextDelta",
        "RunCancelled",
    ]
    assert not any(isinstance(event, (RunCompleted, RunFailed)) for event in collected)


def test_engine_emits_tool_output_deltas_in_strict_sequence() -> None:
    spec = _tool_spec()
    registry = ToolRegistry()
    reporter = None

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        reporter(request.call_id, request.name, "first\n")
        reporter(request.call_id, request.name, "second\n")
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.READ, False, False
        ),
    )
    reporter = executor.output_reporter
    call = ToolCall("call-output", "lookup", {"query": "mica"})
    provider = _ScriptedProvider(
        [[Finish(tool_calls=(call,))], [ProviderTextDelta("done"), Finish()]]
    )
    engine = AgentEngine(
        provider,
        tool_executor=executor,
        run_id_factory=lambda: "run-output",
    )

    events = _run_collect(
        engine,
        ProviderRequest(
            "test-model",
            (Message(MessageRole.USER, "inspect"),),
            (spec,),
        ),
    )

    assert [type(event) for event in events] == [
        RunStarted,
        ToolPrepared,
        ToolStarted,
        ToolOutputDelta,
        ToolOutputDelta,
        ToolFinished,
        TextDelta,
        RunCompleted,
    ]
    assert [event.seq for event in events] == list(range(8))
    assert [
        (event.call_id, event.tool_name, event.text)
        for event in events
        if isinstance(event, ToolOutputDelta)
    ] == [
        ("call-output", "lookup", "first\n"),
        ("call-output", "lookup", "second\n"),
    ]
    assert provider.requests[1].messages[-1] == Message(
        MessageRole.TOOL,
        '{"status":"success","message":"ok","data":null}',
        tool_call_id="call-output",
    )


def test_engine_token_cancellation_after_output_finishes_tool_and_run() -> None:
    spec = _tool_spec()
    registry = ToolRegistry()
    reporter = None
    entered = asyncio.Event()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        reporter(request.call_id, request.name, "first\n")
        entered.set()
        await cancellation.wait()
        cancellation.raise_if_cancelled()
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "bad"
        )

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.READ, False, False
        ),
    )
    reporter = executor.output_reporter
    call = ToolCall("call-token-output", "lookup", {"query": "mica"})
    provider = _ScriptedProvider([[Finish(tool_calls=(call,))]])
    engine = AgentEngine(
        provider,
        tool_executor=executor,
        run_id_factory=lambda: "run-token-output",
    )
    token = CancellationToken()
    request = ProviderRequest(
        "test-model",
        (Message(MessageRole.USER, "inspect"),),
        (spec,),
    )

    async def scenario() -> list[EngineEvent]:
        events: list[EngineEvent] = []
        async for event in engine.run(request, cancellation=token):
            events.append(event)
            if isinstance(event, ToolOutputDelta):
                await asyncio.wait_for(entered.wait(), timeout=0.2)
                token.cancel()
        return events

    events = asyncio.run(scenario())

    assert [type(event) for event in events] == [
        RunStarted,
        ToolPrepared,
        ToolStarted,
        ToolOutputDelta,
        ToolFinished,
        RunCancelled,
    ]
    assert [event.seq for event in events] == list(range(6))
    finished = events[-2]
    assert isinstance(finished, ToolFinished)
    assert finished.status is ToolResultStatus.CANCELLED
    reporter("call-token-output", "lookup", "late")
    assert not any(
        isinstance(event, ToolOutputDelta) and event.text == "late" for event in events
    )


def test_engine_external_task_cancellation_removes_output_route() -> None:
    spec = _tool_spec()
    registry = ToolRegistry()
    reporter = None
    first_delta = asyncio.Event()
    handler_cancelled = asyncio.Event()
    observed: list[tuple[str, str, str]] = []

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        reporter(request.call_id, request.name, "first\n")
        try:
            await asyncio.Event().wait()
        finally:
            handler_cancelled.set()
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "bad"
        )

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.READ, False, False
        ),
    )
    reporter = executor.output_reporter
    call = ToolCall("call-external-output", "lookup", {"query": "mica"})
    provider = _ScriptedProvider([[Finish(tool_calls=(call,))]])
    engine = AgentEngine(
        provider,
        tool_executor=executor,
        run_id_factory=lambda: "run-external-output",
    )
    request = ProviderRequest(
        "test-model",
        (Message(MessageRole.USER, "inspect"),),
        (spec,),
    )

    async def consume() -> None:
        async for event in engine.run(request):
            observed.append(
                (
                    type(event).__name__,
                    getattr(event, "call_id", ""),
                    getattr(event, "text", ""),
                )
            )
            if isinstance(event, ToolOutputDelta):
                first_delta.set()

    async def scenario() -> None:
        task = asyncio.create_task(consume())
        await asyncio.wait_for(first_delta.wait(), timeout=0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)
        await asyncio.wait_for(handler_cancelled.wait(), timeout=0.2)

    asyncio.run(scenario())

    assert [kind for kind, _call_id, _text in observed] == [
        "RunStarted",
        "ToolPrepared",
        "ToolStarted",
        "ToolOutputDelta",
        "RunCancelled",
    ]
    reporter("call-external-output", "lookup", "late")
    assert all(text != "late" for _kind, _call_id, text in observed)


def test_engine_consumer_aclose_after_output_cleans_tool_execution() -> None:
    spec = _tool_spec()
    registry = ToolRegistry()
    reporter = None
    handler_cancelled = asyncio.Event()
    observed: list[EngineEvent] = []
    completed_messages: list[tuple[Message, ...]] = []
    completed_presentations: list[tuple[object, ...]] = []

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        reporter(request.call_id, request.name, "first\n")
        try:
            await asyncio.Event().wait()
        finally:
            handler_cancelled.set()
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "bad"
        )

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.READ, False, False
        ),
    )
    reporter = executor.output_reporter
    call = ToolCall("call-close-output", "lookup", {"query": "mica"})
    engine = AgentEngine(
        _ScriptedProvider([[Finish(tool_calls=(call,))]]),
        tool_executor=executor,
        run_id_factory=lambda: "run-close-output",
    )
    request = ProviderRequest(
        "test-model",
        (Message(MessageRole.USER, "inspect"),),
        (spec,),
    )

    async def scenario() -> None:
        stream = engine.run(
            request,
            completed_messages_sink=completed_messages.append,
            completed_tool_presentations_sink=completed_presentations.append,
        )
        while True:
            event = await asyncio.wait_for(anext(stream), timeout=0.2)
            observed.append(event)
            if isinstance(event, ToolOutputDelta):
                await asyncio.wait_for(stream.aclose(), timeout=0.2)
                break
        await asyncio.wait_for(handler_cancelled.wait(), timeout=0.2)

    asyncio.run(scenario())

    assert [type(event) for event in observed] == [
        RunStarted,
        ToolPrepared,
        ToolStarted,
        ToolOutputDelta,
    ]
    assert completed_messages == []
    assert completed_presentations == []
    reporter("call-close-output", "lookup", "late")
    assert len(observed) == 4


def test_engine_nonzero_tool_result_keeps_delta_before_finished() -> None:
    spec = _tool_spec()
    registry = ToolRegistry()
    reporter = None

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        reporter(request.call_id, request.name, "nonzero output\n")
        return ToolResult(
            request.call_id,
            request.name,
            ToolResultStatus.EXECUTION_FAILED,
            "command exited with non-zero status",
            {"exit_code": 7, "output": "nonzero output\n"},
        )

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.READ, False, False
        ),
    )
    reporter = executor.output_reporter
    call = ToolCall("call-nonzero-output", "lookup", {"query": "mica"})
    provider = _ScriptedProvider([[Finish(tool_calls=(call,))], [Finish()]])
    engine = AgentEngine(
        provider,
        tool_executor=executor,
        run_id_factory=lambda: "run-nonzero-output",
    )

    events = _run_collect(
        engine,
        ProviderRequest(
            "test-model",
            (Message(MessageRole.USER, "inspect"),),
            (spec,),
        ),
    )

    assert [type(event) for event in events] == [
        RunStarted,
        ToolPrepared,
        ToolStarted,
        ToolOutputDelta,
        ToolFinished,
        RunCompleted,
    ]
    assert [event.seq for event in events] == list(range(6))
    finished = events[4]
    assert isinstance(finished, ToolFinished)
    assert finished.status is ToolResultStatus.EXECUTION_FAILED


def test_engine_tool_without_output_emits_no_output_delta() -> None:
    spec = _tool_spec()
    registry = ToolRegistry()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.READ, False, False
        ),
    )
    call = ToolCall("call-no-output", "lookup", {"query": "mica"})
    engine = AgentEngine(
        _ScriptedProvider([[Finish(tool_calls=(call,))], [Finish()]]),
        tool_executor=executor,
        run_id_factory=lambda: "run-no-output",
    )

    events = _run_collect(
        engine,
        ProviderRequest(
            "test-model",
            (Message(MessageRole.USER, "inspect"),),
            (spec,),
        ),
    )

    assert [type(event) for event in events] == [
        RunStarted,
        ToolPrepared,
        ToolStarted,
        ToolFinished,
        RunCompleted,
    ]
    assert [event.seq for event in events] == list(range(5))
    assert not any(isinstance(event, ToolOutputDelta) for event in events)


def test_reused_executor_does_not_route_late_output_between_runs() -> None:
    spec = _tool_spec()
    registry = ToolRegistry()
    reporter = None

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        if request.call_id == "call-second-run":
            reporter("call-first-run", request.name, "late old output")
        reporter(request.call_id, request.name, f"{request.call_id} output")
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.READ, False, False
        ),
    )
    reporter = executor.output_reporter
    first_call = ToolCall("call-first-run", "lookup", {"query": "first"})
    second_call = ToolCall("call-second-run", "lookup", {"query": "second"})
    request = ProviderRequest(
        "test-model",
        (Message(MessageRole.USER, "inspect"),),
        (spec,),
    )

    first_engine = AgentEngine(
        _ScriptedProvider([[Finish(tool_calls=(first_call,))], [Finish()]]),
        tool_executor=executor,
        run_id_factory=lambda: "run-first",
    )
    second_engine = AgentEngine(
        _ScriptedProvider([[Finish(tool_calls=(second_call,))], [Finish()]]),
        tool_executor=executor,
        run_id_factory=lambda: "run-second",
    )

    async def scenario() -> tuple[list[EngineEvent], list[EngineEvent]]:
        first_events = [event async for event in first_engine.run(request)]
        reporter("call-first-run", "lookup", "late after first run")
        second_events = [event async for event in second_engine.run(request)]
        return first_events, second_events

    first_events, second_events = asyncio.run(scenario())

    assert [
        (event.run_id, event.call_id, event.text)
        for event in first_events
        if isinstance(event, ToolOutputDelta)
    ] == [("run-first", "call-first-run", "call-first-run output")]
    assert [
        (event.run_id, event.call_id, event.text)
        for event in second_events
        if isinstance(event, ToolOutputDelta)
    ] == [("run-second", "call-second-run", "call-second-run output")]


def test_engine_real_git_bash_output_closes_loop_before_next_model_request(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "工作区 space"
    workspace.mkdir()
    registry = ToolRegistry()
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT, allow_rules=[lambda policy_input: True]),
    )

    async def requester(
        interaction_id: str, question: str, cancellation: CancellationToken
    ) -> UserInputResponse:
        raise AssertionError("ask must not run in the bash loop")

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        requester,
        output_reporter=executor.output_reporter,
    )
    assert {spec.name for spec in executor.tool_specs} == {"ask", "bash"}

    command = (
        "printf 'first line\\n'; "
        "while [ ! -f release.marker ]; do sleep 0.01; done; "
        "printf 'second line\\n'"
    )
    call = ToolCall("call-real-bash", "bash", {"command": command})
    provider = _ScriptedProvider(
        [[Finish(tool_calls=(call,))], [ProviderTextDelta("done"), Finish()]]
    )
    engine = AgentEngine(
        provider,
        tool_executor=executor,
        run_id_factory=lambda: "run-real-bash",
    )
    request = ProviderRequest(
        "test-model",
        (Message(MessageRole.USER, "run"),),
        executor.tool_specs,
    )
    events: list[EngineEvent] = []
    first_delta_seen = False

    async def scenario() -> None:
        nonlocal first_delta_seen
        async for event in engine.run(request):
            events.append(event)
            if isinstance(event, ToolOutputDelta) and not first_delta_seen:
                first_delta_seen = True
                assert event.text == "first line\n"
                assert not any(isinstance(item, ToolFinished) for item in events)
                assert not any(isinstance(item, RunCompleted) for item in events)
                (workspace / "release.marker").touch()

    asyncio.run(scenario())

    assert first_delta_seen is True
    assert [
        (event.call_id, event.tool_name, event.text)
        for event in events
        if isinstance(event, ToolOutputDelta)
    ] == [
        ("call-real-bash", "bash", "first line\n"),
        ("call-real-bash", "bash", "second line\n"),
    ]
    assert [event.seq for event in events] == list(range(len(events)))
    assert isinstance(events[-1], RunCompleted)
    assert len(provider.requests) == 2


def test_engine_real_bash_output_events_are_clean_and_not_persisted_as_deltas(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT, allow_rules=[lambda policy_input: True]),
    )

    async def requester(
        interaction_id: str, question: str, cancellation: CancellationToken
    ) -> UserInputResponse:
        raise AssertionError("ask must not run in the bash safety test")

    register_runtime_tools(
        registry,
        resolve_workspace(workspace),
        requester,
        output_reporter=executor.output_reporter,
    )
    secret = "engine-sensitive-marker"
    command = (
        f"secret='{secret}'; "
        "printf 'plain 中文  继续\\r\\n'; "
        'printf \'api_key=%s token=%s authorization=%s\\n\' "$secret" "$secret" "$secret"; '
        'printf \'Authorization: Bearer %s sk-%s\\n\' "$secret" "$secret"'
    )
    call = ToolCall("call-safe-bash", "bash", {"command": command})
    provider = _ScriptedProvider(
        [[Finish(tool_calls=(call,))], [ProviderTextDelta("done"), Finish()]]
    )
    engine = AgentEngine(
        provider,
        tool_executor=executor,
        run_id_factory=lambda: "run-safe-bash",
    )
    request = ProviderRequest(
        "test-model",
        (Message(MessageRole.USER, "run"),),
        executor.tool_specs,
    )
    completed_messages: list[tuple[Message, ...]] = []
    completed_presentations: list[tuple[object, ...]] = []

    async def scenario() -> list[EngineEvent]:
        return [
            event
            async for event in engine.run(
                request,
                completed_messages_sink=completed_messages.append,
                completed_tool_presentations_sink=completed_presentations.append,
            )
        ]

    events = asyncio.run(scenario())
    deltas = [event for event in events if isinstance(event, ToolOutputDelta)]
    assert deltas
    assert deltas[0].text == "plain 中文  继续\r\n"
    assert all(secret not in delta.text for delta in deltas)
    assert any("[REDACTED]" in delta.text for delta in deltas)
    assert all(secret not in str(event) for event in events)
    assert all(secret not in repr(event) for event in events)

    tool_messages = [
        message
        for message in provider.requests[1].messages
        if message.role is MessageRole.TOOL
    ]
    assert len(tool_messages) == 1
    assert secret not in tool_messages[0].content
    assert "[REDACTED]" in tool_messages[0].content
    assert completed_messages
    assert secret not in repr(completed_messages)
    assert completed_presentations
    assert secret not in repr(completed_presentations)
    assert not any(isinstance(item, ToolOutputDelta) for item in completed_messages)
    assert not any(
        isinstance(item, ToolOutputDelta) for item in completed_presentations
    )


def test_real_bash_engine_token_cancel_reaps_after_first_delta(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = _runtime_bash_executor(workspace)
    command = (
        "printf 'first line\\n'; "
        "while [ ! -f release.marker ]; do sleep 0.01; done; "
        "printf 'second line\\n'"
    )
    call = ToolCall("call-real-token-cancel", "bash", {"command": command})
    provider = _ScriptedProvider([[Finish(tool_calls=(call,))]])
    engine = AgentEngine(
        provider,
        tool_executor=executor,
        run_id_factory=lambda: "run-real-token-cancel",
    )
    request = ProviderRequest(
        "test-model",
        (Message(MessageRole.USER, "run"),),
        executor.tool_specs,
    )
    token = CancellationToken()

    async def scenario() -> list[EngineEvent]:
        events: list[EngineEvent] = []
        async for event in engine.run(request, cancellation=token):
            events.append(event)
            if isinstance(event, ToolOutputDelta):
                token.cancel()
        return events

    events = asyncio.run(scenario())

    assert [type(event) for event in events] == [
        RunStarted,
        ToolPrepared,
        ToolStarted,
        ToolOutputDelta,
        ToolFinished,
        RunCancelled,
    ]
    assert isinstance(events[-2], ToolFinished)
    assert events[-2].status is ToolResultStatus.CANCELLED
    assert [event.seq for event in events] == list(range(6))


def test_real_bash_engine_external_cancel_reaps_after_first_delta(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = _runtime_bash_executor(workspace)
    call_id = "call-real-external-cancel"
    command = (
        "printf 'first line\\n'; "
        "while [ ! -f release.marker ]; do sleep 0.01; done; "
        "printf 'second line\\n'"
    )
    call = ToolCall(call_id, "bash", {"command": command})
    provider = _ScriptedProvider([[Finish(tool_calls=(call,))]])
    engine = AgentEngine(
        provider,
        tool_executor=executor,
        run_id_factory=lambda: "run-real-external-cancel",
    )
    request = ProviderRequest(
        "test-model",
        (Message(MessageRole.USER, "run"),),
        executor.tool_specs,
    )
    observed: list[EngineEvent] = []
    first_delta = asyncio.Event()

    async def consume() -> None:
        async for event in engine.run(request):
            observed.append(event)
            if isinstance(event, ToolOutputDelta):
                first_delta.set()

    async def scenario() -> None:
        task = asyncio.create_task(consume())
        await asyncio.wait_for(first_delta.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(scenario())

    assert [type(event) for event in observed] == [
        RunStarted,
        ToolPrepared,
        ToolStarted,
        ToolOutputDelta,
        RunCancelled,
    ]
    executor.output_reporter(call_id, "bash", "late")
    assert len(observed) == 5


def test_real_bash_engine_aclose_after_first_delta_reaps_tool(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = _runtime_bash_executor(workspace)
    call_id = "call-real-stream-close"
    command = (
        "printf 'first line\\n'; "
        "while [ ! -f release.marker ]; do sleep 0.01; done; "
        "printf 'second line\\n'"
    )
    call = ToolCall(call_id, "bash", {"command": command})
    engine = AgentEngine(
        _ScriptedProvider([[Finish(tool_calls=(call,))]]),
        tool_executor=executor,
        run_id_factory=lambda: "run-real-stream-close",
    )
    request = ProviderRequest(
        "test-model",
        (Message(MessageRole.USER, "run"),),
        executor.tool_specs,
    )
    observed: list[EngineEvent] = []
    completed_messages: list[tuple[Message, ...]] = []
    completed_presentations: list[tuple[object, ...]] = []

    async def scenario() -> None:
        stream = engine.run(
            request,
            completed_messages_sink=completed_messages.append,
            completed_tool_presentations_sink=completed_presentations.append,
        )
        while True:
            event = await asyncio.wait_for(anext(stream), timeout=1.0)
            observed.append(event)
            if isinstance(event, ToolOutputDelta):
                await asyncio.wait_for(stream.aclose(), timeout=1.0)
                return

    asyncio.run(scenario())

    assert [type(event) for event in observed] == [
        RunStarted,
        ToolPrepared,
        ToolStarted,
        ToolOutputDelta,
    ]
    assert completed_messages == []
    assert completed_presentations == []
    executor.output_reporter(call_id, "bash", "late")
    assert len(observed) == 4
