"""ToolExecutor 公共行为测试。"""

from __future__ import annotations

import asyncio

import pytest

from mica_agent.cancellation import CancellationToken
from mica_agent.interactions import (
    InteractionBroker,
    PermissionAnswer,
    PermissionResponse,
)
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine, PolicyInput, PolicyOperation
from mica_agent.tool_contracts import (
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_registry import ToolRegistry


def _spec(name: str = "read") -> ToolSpec:
    return ToolSpec(
        name,
        "Read a value",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )


def _executor(
    registry: ToolRegistry,
    *,
    operation: PolicyOperation = PolicyOperation.READ,
    policy: PolicyEngine | None = None,
    broker: InteractionBroker | None = None,
) -> ToolExecutor:
    return ToolExecutor(
        registry,
        policy or PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, operation, False, False
        ),
        interaction_broker=broker,
    )


def _request(name: str = "read", arguments: object = None) -> ToolRequest:
    if arguments is None:
        arguments = {"value": "hello"}
    return ToolRequest("call-1", name, arguments)  # type: ignore[arg-type]


def test_tool_specs_is_deterministic_public_immutable_snapshot() -> None:
    registry = ToolRegistry()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    second = _spec("write")
    first = _spec("read")
    registry.register(second, handler)
    registry.register(first, handler)
    executor = _executor(registry)

    snapshot = executor.tool_specs

    assert snapshot == (first, second)
    assert isinstance(snapshot, tuple)
    assert executor.tool_specs is not snapshot


def test_known_tool_validates_and_executes_once_when_allowed() -> None:
    calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(
            request.call_id,
            request.name,
            ToolResultStatus.SUCCESS,
            "read complete",
            {"value": "ok"},
        )

    registry = ToolRegistry()
    registry.register(_spec(), handler)

    result = asyncio.run(_executor(registry).execute(_request(), CancellationToken()))

    assert result == ToolResult(
        "call-1", "read", ToolResultStatus.SUCCESS, "read complete", {"value": "ok"}
    )
    assert calls == 1


def test_unknown_tool_returns_unknown_tool_without_handler_call() -> None:
    registry = ToolRegistry()
    calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "unexpected"
        )

    registry.register(_spec(), handler)
    result = asyncio.run(
        _executor(registry).execute(_request("missing"), CancellationToken())
    )

    assert result.status is ToolResultStatus.UNKNOWN_TOOL
    assert calls == 0
    assert result.message == "tool is not available"


def test_invalid_arguments_return_invalid_arguments_without_handler_call() -> None:
    registry = ToolRegistry()
    calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        raise AssertionError("handler must not run")

    registry.register(_spec(), handler)
    result = asyncio.run(
        _executor(registry).execute(
            _request(arguments={"value": 42}), CancellationToken()
        )
    )

    assert result.status is ToolResultStatus.INVALID_ARGUMENTS
    assert result.message == "tool arguments are invalid"
    assert calls == 0


def test_deny_returns_permission_denied_without_handler_call() -> None:
    registry = ToolRegistry()
    calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        raise AssertionError("handler must not run")

    registry.register(_spec(), handler)
    policy = PolicyEngine(PermissionMode.DEFAULT, allow_rules=[])
    executor = ToolExecutor(
        registry,
        policy,
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.WRITE, True, False
        ),
    )

    result = asyncio.run(executor.execute(_request(), CancellationToken()))

    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert result.message == "permission denied"
    assert calls == 0


def test_ask_allows_once_for_only_the_current_call() -> None:
    broker = InteractionBroker(
        interaction_id_factory=iter(["permission-1", "permission-2"]).__next__
    )
    registry = ToolRegistry()
    calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "allowed"
        )

    registry.register(_spec(), handler)
    executor = _executor(
        registry,
        operation=PolicyOperation.WRITE,
        broker=broker,
    )

    async def scenario() -> tuple[ToolResult, ToolResult]:
        first_task = asyncio.create_task(
            executor.execute(_request(), CancellationToken())
        )
        first_request = await broker.next_request()
        assert broker.respond(
            PermissionResponse(
                first_request.interaction_id, PermissionAnswer.ALLOW_ONCE
            )
        )
        first = await first_task

        second_task = asyncio.create_task(
            executor.execute(
                ToolRequest("call-2", "read", {"value": "again"}), CancellationToken()
            )
        )
        second_request = await broker.next_request()
        assert broker.respond(
            PermissionResponse(second_request.interaction_id, PermissionAnswer.DENY)
        )
        second = await second_task
        return first, second

    first, second = asyncio.run(scenario())

    assert first.status is ToolResultStatus.SUCCESS
    assert second.status is ToolResultStatus.PERMISSION_DENIED
    assert calls == 1


def test_handler_exception_and_mismatched_results_are_safe_failures() -> None:
    async def exploding(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        raise RuntimeError("secret path C:/private/sk-secret traceback")

    async def wrong_type(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return "not a result"  # type: ignore[return-value]

    async def wrong_identity(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return ToolResult("other-call", "read", ToolResultStatus.SUCCESS, "wrong")

    for handler in (exploding, wrong_type, wrong_identity):
        registry = ToolRegistry()
        registry.register(_spec(), handler)
        result = asyncio.run(
            _executor(registry).execute(_request(), CancellationToken())
        )
        assert result.status is ToolResultStatus.EXECUTION_FAILED
        assert result.message == "tool execution failed"
        assert "secret" not in result.message
        assert "traceback" not in result.message


def test_handler_receives_one_call_and_cancellation_stops_execution() -> None:
    calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        await cancellation.wait()
        cancellation.raise_if_cancelled()
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "unreachable"
        )

    registry = ToolRegistry()
    registry.register(_spec(), handler)
    executor = _executor(registry)

    async def scenario() -> ToolResult:
        cancellation = CancellationToken()
        task = asyncio.create_task(executor.execute(_request(), cancellation))
        await asyncio.sleep(0)
        cancellation.cancel()
        return await asyncio.wait_for(task, timeout=0.1)

    result = asyncio.run(scenario())

    assert result.status is ToolResultStatus.CANCELLED
    assert calls == 1


def test_already_cancelled_request_does_not_call_handler() -> None:
    calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "bad"
        )

    registry = ToolRegistry()
    registry.register(_spec(), handler)
    cancellation = CancellationToken()
    cancellation.cancel()

    result = asyncio.run(_executor(registry).execute(_request(), cancellation))

    assert result.status is ToolResultStatus.CANCELLED
    assert calls == 0


@pytest.mark.parametrize(
    "response",
    [
        PermissionResponse("wrong-id", PermissionAnswer.ALLOW_ONCE),
        PermissionResponse("permission-1", PermissionAnswer.DENY),
    ],
)
def test_permission_wait_does_not_execute_after_invalid_or_denied_response(
    response: PermissionResponse,
) -> None:
    broker = InteractionBroker(interaction_id_factory=iter(["permission-1"]).__next__)
    registry = ToolRegistry()
    calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "allowed"
        )

    registry.register(_spec(), handler)
    executor = _executor(registry, operation=PolicyOperation.WRITE, broker=broker)

    async def scenario() -> ToolResult:
        task = asyncio.create_task(executor.execute(_request(), CancellationToken()))
        request = await broker.next_request()
        if response.interaction_id == request.interaction_id:
            broker.respond(response)
        else:
            assert broker.respond(response) is False
            broker.respond(
                PermissionResponse(request.interaction_id, PermissionAnswer.DENY)
            )
        return await task

    result = asyncio.run(scenario())

    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert calls == 0
