"""AgentEngine 公共行为测试（P2-03）。

最小 Provider 只存在于本测试文件内，用于固定事件序列、
错误映射和精确取消时机；真实跨层链路在 test_app.py 验证。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from mica_agent.cancellation import CancellationToken
from mica_agent.engine import AgentEngine
from mica_agent.events import (
    EngineEvent,
    ErrorCategory,
    PermissionRequested,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
    ToolFinished,
    ToolStarted,
    UserInputRequested,
)
from mica_agent.interactions import (
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
    ProviderRequest,
    ProviderTimeoutError,
    RateLimitError,
    TemporaryServerError,
)
from mica_agent.providers import (
    TextDelta as ProviderTextDelta,
)
from mica_agent.tool_contracts import (
    ToolCall,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_registry import ToolRegistry


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
        response = await broker(
            request.call_id, request.arguments["question"], cancellation
        )
        return ToolResult(
            request.call_id,
            request.name,
            ToolResultStatus.SUCCESS,
            "answer received",
            response.answer,
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
                assert broker.respond(UserInputResponse(event.interaction_id, "yes"))
        return events

    events = asyncio.run(scenario())

    assert any(isinstance(event, UserInputRequested) for event in events)
    assert isinstance(events[-1], RunCompleted)
    assert provider.requests[1].messages[-1].content == (
        '{"status":"success","message":"answer received","data":"yes"}'
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
        ToolStarted("run-tool", 1, "call-1", "lookup"),
        ToolFinished("run-tool", 2, "call-1", "lookup", ToolResultStatus.SUCCESS),
        TextDelta("run-tool", "final answer", 3),
        RunCompleted("run-tool", 4),
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
