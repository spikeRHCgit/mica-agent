"""ToolExecutor 公共行为测试。"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
from pathlib import Path

import pytest

from mica_agent.cancellation import CancellationToken
from mica_agent.interactions import (
    InteractionBroker,
    PermissionAnswer,
    PermissionResponse,
)
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine, PolicyInput, PolicyOperation
from mica_agent.runtime_tools import ToolOutputReporter
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


def test_registered_ls_has_no_builtin_list_privilege() -> None:
    operations: list[PolicyOperation] = []

    def observe(policy_input: PolicyInput) -> bool:
        operations.append(policy_input.operation)
        return True

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "custom tool ran"
        )

    registry = ToolRegistry()
    registry.register(_spec("ls"), handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT, allow_rules=[observe]),
    )

    result = asyncio.run(executor.execute(_request("ls"), CancellationToken()))

    assert result.status is ToolResultStatus.SUCCESS
    assert operations == [PolicyOperation.OTHER]


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


def test_unregistered_ls_fails_safely_without_filesystem_or_command_execution(
    monkeypatch,
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("unknown ls must not perform side effects")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    executor = ToolExecutor(ToolRegistry(), PolicyEngine(PermissionMode.DEFAULT))
    secret = "credential-like-argument"

    result = asyncio.run(
        executor.execute(
            ToolRequest("unknown-ls", "ls", {"path": secret}), CancellationToken()
        )
    )

    assert result.status is ToolResultStatus.UNKNOWN_TOOL
    assert result.tool_name == "ls"
    assert result.message == "tool is not available"
    assert result.data is None
    assert secret not in result.message
    assert "Traceback" not in result.message


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


def test_output_reporter_routes_only_during_current_handler() -> None:
    registry = ToolRegistry()
    entered = asyncio.Event()
    release = asyncio.Event()
    observed: list[tuple[str, str, str]] = []
    lifecycle: list[str] = []
    reporter: ToolOutputReporter | None = None

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        lifecycle.append("handler")
        reporter(request.call_id, request.name, "first")
        entered.set()
        await release.wait()
        reporter(request.call_id, request.name, "second")
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    registry.register(_spec(), handler)
    executor = _executor(registry)
    reporter = executor.output_reporter

    def started(request: ToolRequest) -> None:
        lifecycle.append("started")
        assert reporter is not None
        reporter(request.call_id, request.name, "during started")

    def finished(result: ToolResult) -> None:
        lifecycle.append("finished")
        assert reporter is not None
        reporter(result.call_id, result.tool_name, "during finished")

    async def scenario() -> ToolResult:
        task = asyncio.create_task(
            executor.execute(
                _request(),
                CancellationToken(),
                on_started=started,
                on_finished=finished,
                on_output=lambda call_id, tool_name, text: observed.append(
                    (call_id, tool_name, text)
                ),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=0.2)
        assert observed == [("call-1", "read", "first")]
        release.set()
        return await asyncio.wait_for(task, timeout=0.2)

    result = asyncio.run(scenario())

    assert callable(executor.output_reporter)
    assert not inspect.iscoroutinefunction(executor.output_reporter)
    assert result.status is ToolResultStatus.SUCCESS
    assert observed == [
        ("call-1", "read", "first"),
        ("call-1", "read", "second"),
    ]
    assert lifecycle == ["started", "handler", "finished"]
    reporter("call-1", "read", "late")
    assert observed == [
        ("call-1", "read", "first"),
        ("call-1", "read", "second"),
    ]


def test_output_reporter_ignores_mismatched_identity_and_rejects_invalid_text() -> None:
    registry = ToolRegistry()
    observed: list[tuple[str, str, str]] = []
    reporter: ToolOutputReporter | None = None

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        reporter("other-call", request.name, "wrong call")
        reporter(request.call_id, "write", "wrong tool")
        with pytest.raises(ValueError, match="non-empty string"):
            reporter(request.call_id, request.name, "")
        with pytest.raises(ValueError, match="non-empty string"):
            reporter(request.call_id, request.name, 42)  # type: ignore[arg-type]
        reporter(request.call_id, request.name, "safe")
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    registry.register(_spec(), handler)
    executor = _executor(registry)
    reporter = executor.output_reporter

    result = asyncio.run(
        executor.execute(
            _request(),
            CancellationToken(),
            on_output=lambda call_id, tool_name, text: observed.append(
                (call_id, tool_name, text)
            ),
        )
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert observed == [("call-1", "read", "safe")]
    reporter("call-1", "read", "late")
    reporter("other-call", "read", "late")
    assert observed == [("call-1", "read", "safe")]


def test_output_callback_exception_is_not_swallowed_by_executor() -> None:
    registry = ToolRegistry()
    reporter: ToolOutputReporter | None = None
    callback_error = RuntimeError("private callback failure")

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        with pytest.raises(RuntimeError) as raised:
            reporter(request.call_id, request.name, "safe")
        assert raised.value is callback_error
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    def on_output(call_id: str, tool_name: str, text: str) -> None:
        raise callback_error

    registry.register(_spec(), handler)
    executor = _executor(registry)
    reporter = executor.output_reporter

    result = asyncio.run(
        executor.execute(_request(), CancellationToken(), on_output=on_output)
    )

    assert result.status is ToolResultStatus.SUCCESS


def test_output_route_is_removed_after_handler_failure() -> None:
    registry = ToolRegistry()
    entered = asyncio.Event()
    observed: list[tuple[str, str, str]] = []
    reporter: ToolOutputReporter | None = None

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        reporter(request.call_id, request.name, "before failure")
        entered.set()
        raise RuntimeError("private failure")

    registry.register(_spec(), handler)
    executor = _executor(registry)
    reporter = executor.output_reporter

    async def scenario() -> ToolResult:
        task = asyncio.create_task(
            executor.execute(
                _request(),
                CancellationToken(),
                on_output=lambda call_id, tool_name, text: observed.append(
                    (call_id, tool_name, text)
                ),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=0.2)
        return await asyncio.wait_for(task, timeout=0.2)

    result = asyncio.run(scenario())

    assert result.status is ToolResultStatus.EXECUTION_FAILED
    assert observed == [("call-1", "read", "before failure")]
    reporter("call-1", "read", "late failure")
    assert observed == [("call-1", "read", "before failure")]


def test_output_route_is_removed_after_token_cancellation() -> None:
    registry = ToolRegistry()
    entered = asyncio.Event()
    observed: list[tuple[str, str, str]] = []
    reporter: ToolOutputReporter | None = None

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        reporter(request.call_id, request.name, "before cancellation")
        entered.set()
        await cancellation.wait()
        cancellation.raise_if_cancelled()
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "bad"
        )

    registry.register(_spec(), handler)
    executor = _executor(registry)
    reporter = executor.output_reporter

    async def scenario() -> ToolResult:
        cancellation = CancellationToken()
        task = asyncio.create_task(
            executor.execute(
                _request(),
                cancellation,
                on_output=lambda call_id, tool_name, text: observed.append(
                    (call_id, tool_name, text)
                ),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=0.2)
        cancellation.cancel()
        return await asyncio.wait_for(task, timeout=0.2)

    result = asyncio.run(scenario())

    assert result.status is ToolResultStatus.CANCELLED
    assert observed == [("call-1", "read", "before cancellation")]
    reporter("call-1", "read", "late cancellation")
    assert observed == [("call-1", "read", "before cancellation")]


def test_output_route_is_removed_after_external_task_cancellation() -> None:
    registry = ToolRegistry()
    entered = asyncio.Event()
    observed: list[tuple[str, str, str]] = []
    reporter: ToolOutputReporter | None = None

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        reporter(request.call_id, request.name, "before task cancellation")
        entered.set()
        await asyncio.Event().wait()
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "bad"
        )

    registry.register(_spec(), handler)
    executor = _executor(registry)
    reporter = executor.output_reporter

    async def scenario() -> None:
        task = asyncio.create_task(
            executor.execute(
                _request(),
                CancellationToken(),
                on_output=lambda call_id, tool_name, text: observed.append(
                    (call_id, tool_name, text)
                ),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)

    asyncio.run(scenario())

    assert observed == [("call-1", "read", "before task cancellation")]
    reporter("call-1", "read", "late task cancellation")
    assert observed == [("call-1", "read", "before task cancellation")]


def test_output_routes_isolate_concurrent_call_ids() -> None:
    registry = ToolRegistry()
    entered = {"call-1": asyncio.Event(), "call-2": asyncio.Event()}
    release = asyncio.Event()
    observed: dict[str, list[tuple[str, str, str]]] = {
        "call-1": [],
        "call-2": [],
    }
    reporter: ToolOutputReporter | None = None

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        assert reporter is not None
        reporter(request.call_id, request.name, f"{request.call_id}-first")
        entered[request.call_id].set()
        await release.wait()
        reporter(request.call_id, request.name, f"{request.call_id}-second")
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    registry.register(_spec(), handler)
    executor = _executor(registry)
    reporter = executor.output_reporter

    async def scenario() -> tuple[ToolResult, ToolResult]:
        first = asyncio.create_task(
            executor.execute(
                ToolRequest("call-1", "read", {"value": "one"}),
                CancellationToken(),
                on_output=lambda call_id, tool_name, text: observed["call-1"].append(
                    (call_id, tool_name, text)
                ),
            )
        )
        second = asyncio.create_task(
            executor.execute(
                ToolRequest("call-2", "read", {"value": "two"}),
                CancellationToken(),
                on_output=lambda call_id, tool_name, text: observed["call-2"].append(
                    (call_id, tool_name, text)
                ),
            )
        )
        await asyncio.wait_for(
            asyncio.gather(entered["call-1"].wait(), entered["call-2"].wait()),
            timeout=0.2,
        )
        release.set()
        return await asyncio.wait_for(asyncio.gather(first, second), timeout=0.2)

    first_result, second_result = asyncio.run(scenario())

    assert first_result.status is ToolResultStatus.SUCCESS
    assert second_result.status is ToolResultStatus.SUCCESS
    assert observed == {
        "call-1": [
            ("call-1", "read", "call-1-first"),
            ("call-1", "read", "call-1-second"),
        ],
        "call-2": [
            ("call-2", "read", "call-2-first"),
            ("call-2", "read", "call-2-second"),
        ],
    }


def test_non_callable_on_output_fails_before_tool_behavior() -> None:
    registry = ToolRegistry()
    handler_calls = 0
    policy_calls = 0

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        nonlocal handler_calls
        handler_calls += 1
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "bad"
        )

    def policy_input(request: ToolRequest, spec: ToolSpec) -> PolicyInput:
        nonlocal policy_calls
        policy_calls += 1
        return PolicyInput(request.name, PolicyOperation.READ, False, False)

    registry.register(_spec(), handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=policy_input,
    )

    with pytest.raises(TypeError, match="on_output"):
        asyncio.run(
            executor.execute(
                _request(),
                CancellationToken(),
                on_output=object(),  # type: ignore[arg-type]
            )
        )

    assert handler_calls == 0
    assert policy_calls == 0
