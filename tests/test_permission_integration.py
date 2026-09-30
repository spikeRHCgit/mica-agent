"""Two-mode authorization through the public tool executor; no destructive commands run."""

import asyncio
from pathlib import Path

import pytest

from mica_agent.cancellation import CancellationToken
from mica_agent.file_tools import register_file_tools
from mica_agent.interactions import (
    InteractionBroker,
    PermissionAnswer,
    PermissionResponse,
)
from mica_agent.permissions import PermissionMode, PolicyDecision
from mica_agent.policy import PolicyEngine, PolicyInput, PolicyOperation
from mica_agent.tool_contracts import (
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_registry import ToolRegistry
from mica_agent.workspace import resolve_workspace


@pytest.mark.parametrize(
    "command",
    [
        "rm fake",
        "timeout 5 rm fake",
        "nohup rm fake",
        "xargs rm",
    ],
)
@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("answer", list(PermissionAnswer))
def test_risky_call_waits_once_or_solo_bypasses(command, mode, answer) -> None:
    registry = ToolRegistry()
    calls = []

    async def handler(request, cancellation):
        calls.append(request.arguments["command"])
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    registry.register(ToolSpec("bash", "Fake", {"type": "object"}), handler)
    broker = InteractionBroker()
    executor = ToolExecutor(registry, PolicyEngine(mode), interaction_broker=broker)
    raw = command

    async def scenario():
        for index in range(2):
            task = asyncio.create_task(
                executor.execute(
                    ToolRequest(str(index), "bash", {"command": raw}),
                    CancellationToken(),
                )
            )
            if mode is PermissionMode.DEFAULT:
                request = await asyncio.wait_for(broker.next_request(), 1)
                assert len(calls) == index * (answer is PermissionAnswer.ALLOW_ONCE)
                assert broker.pending_count == 1
                assert broker.respond(
                    PermissionResponse(request.interaction_id, answer)
                )
            result = await asyncio.wait_for(task, 1)
            allowed = (
                mode is PermissionMode.SOLO or answer is PermissionAnswer.ALLOW_ONCE
            )
            assert result.status is (
                ToolResultStatus.SUCCESS
                if allowed
                else ToolResultStatus.PERMISSION_DENIED
            )
            assert broker.pending_count == 0
        assert calls == ([raw, raw] if allowed else [])

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("with_workspace", [False, True])
def test_protected_file_read_write_edit_work_after_approval(
    tmp_path: Path, mode, with_workspace
) -> None:
    workspace = resolve_workspace(tmp_path)
    registry = ToolRegistry()
    register_file_tools(registry, workspace)
    broker = InteractionBroker()
    executor = ToolExecutor(
        registry,
        PolicyEngine(mode),
        interaction_broker=broker,
        workspace=workspace if with_workspace else None,
    )

    async def scenario():
        for name, arguments in [
            ("write", {"path": ".env", "content": "FAKE=one"}),
            ("read", {"path": ".env"}),
            ("edit", {"path": ".env", "old_text": "one", "new_text": "two"}),
        ]:
            task = asyncio.create_task(
                executor.execute(
                    ToolRequest(name, name, arguments), CancellationToken()
                )
            )
            if mode is PermissionMode.DEFAULT:
                request = await asyncio.wait_for(broker.next_request(), 1)
                assert broker.respond(
                    PermissionResponse(
                        request.interaction_id, PermissionAnswer.ALLOW_ONCE
                    )
                )
            assert (await asyncio.wait_for(task, 1)).status is ToolResultStatus.SUCCESS
        assert broker.pending_count == 0

    asyncio.run(scenario())
    assert (tmp_path / ".env").read_text() == "FAKE=two"


def test_solo_skips_fact_factory_and_rules_but_validates_and_cancels() -> None:
    def explode(*args):
        raise AssertionError("policy must not run")

    policy = PolicyEngine(PermissionMode.SOLO, [explode])
    for operation in PolicyOperation:
        assert (
            policy.decide(PolicyInput("tool", operation, True, False, True, True))
            is PolicyDecision.ALLOW
        )
    registry = ToolRegistry()
    calls = []

    async def handler(request, cancellation):
        calls.append(request)
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    registry.register(
        ToolSpec(
            "custom",
            "Fake",
            {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string"}},
            },
        ),
        handler,
    )
    executor = ToolExecutor(registry, policy, policy_input_factory=explode)

    async def scenario():
        good = ToolRequest("1", "custom", {"value": "ok"})
        assert (
            await executor.execute(good, CancellationToken())
        ).status is ToolResultStatus.SUCCESS
        assert (
            await executor.execute(ToolRequest("2", "custom", {}), CancellationToken())
        ).status is ToolResultStatus.INVALID_ARGUMENTS
        assert (
            await executor.execute(ToolRequest("3", "missing", {}), CancellationToken())
        ).status is ToolResultStatus.UNKNOWN_TOOL
        token = CancellationToken()
        token.cancel()
        assert (
            await executor.execute(good, token)
        ).status is ToolResultStatus.CANCELLED
        assert len(calls) == 1

    asyncio.run(scenario())


def test_risk_wait_cancellation_never_starts_handler() -> None:
    registry = ToolRegistry()

    async def handler(request, cancellation):
        raise AssertionError("must not execute")

    registry.register(ToolSpec("bash", "Fake", {"type": "object"}), handler)
    executor = ToolExecutor(registry, PolicyEngine(PermissionMode.DEFAULT))

    async def scenario():
        token = CancellationToken()
        task = asyncio.create_task(
            executor.execute(ToolRequest("1", "bash", {"command": "rm fake"}), token)
        )
        await asyncio.wait_for(executor.interaction_broker.next_request(), 1)
        token.cancel()
        assert (await asyncio.wait_for(task, 1)).status is ToolResultStatus.CANCELLED
        assert executor.interaction_broker.pending_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "command", ["echo hello", "curl https://example.test", "wget https://example.test"]
)
def test_daily_bash_and_network_use_fake_handler_without_permission(command):
    registry = ToolRegistry()
    calls = []

    async def handler(request, cancellation):
        calls.append(request.arguments["command"])
        return ToolResult(request.call_id, request.name, ToolResultStatus.SUCCESS, "ok")

    registry.register(ToolSpec("bash", "Fake", {"type": "object"}), handler)
    executor = ToolExecutor(registry, PolicyEngine(PermissionMode.DEFAULT))

    async def scenario():
        result = await asyncio.wait_for(
            executor.execute(
                ToolRequest("1", "bash", {"command": command}), CancellationToken()
            ),
            1,
        )
        assert result.status is ToolResultStatus.SUCCESS
        assert executor.interaction_broker.pending_count == 0

    asyncio.run(scenario())
    assert calls == [command]


@pytest.mark.parametrize("path", ["../outside.txt", "NUL", "normal.txt:secret"])
def test_solo_keeps_file_capability_restrictions(tmp_path, path):
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(tmp_path))
    executor = ToolExecutor(registry, PolicyEngine(PermissionMode.SOLO))
    result = asyncio.run(
        executor.execute(
            ToolRequest("1", "write", {"path": path, "content": "fake"}),
            CancellationToken(),
        )
    )
    assert result.status is not ToolResultStatus.SUCCESS
    assert list(tmp_path.iterdir()) == []
