"""Cross-layer session evidence for a cancelled tool batch.

This module protects the narrow App -> Engine -> ToolExecutor -> SessionService
path in which a batch stops after its first tool completes: the cancelled run
commits a checkpoint for the completed prefix and the unstarted call, and the
restored context keeps the real prefix without fabricating a result for the
tool that never ran.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from mica_agent.app import MicaApp
from mica_agent.cancellation import CancellationToken
from mica_agent.checkpoint_contracts import RunCheckpointToolStatus
from mica_agent.context_builder import ContextBuilder
from mica_agent.events import EngineEvent, RunCancelled, ToolFinished
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import MessageRole
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine, PolicyInput, PolicyOperation
from mica_agent.providers import Finish, ProviderEvent, ProviderRequest
from mica_agent.providers import TextDelta as ProviderTextDelta
from mica_agent.session_service import SessionService
from mica_agent.tool_contracts import (
    ToolCall,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_registry import ToolRegistry
from mica_agent.workspace import resolve_workspace


class _ScriptedProvider:
    """Yield one fixed event script for every request; one run per test."""

    def __init__(self, events: list[ProviderEvent]) -> None:
        self._events = events
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        for event in self._events:
            yield event

    async def aclose(self) -> None:
        return None


def _lookup_executor() -> ToolExecutor:
    registry = ToolRegistry()
    spec = ToolSpec("lookup", "Look up", {"type": "object"})

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        cancellation.raise_if_cancelled()
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "lookup complete"
        )

    registry.register(spec, handler)
    return ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.READ, False, False
        ),
    )


def test_tool_batch_cancel_keeps_completed_prefix_and_unstarted_call(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    data_root = tmp_path / "mica-data"
    workspace = resolve_workspace(workspace_path)
    service = SessionService(JsonlSessionStore(workspace, data_root))
    session = service.create_session()
    first = ToolCall("call-a", "lookup", {"query": "one"})
    second = ToolCall("call-b", "lookup", {"query": "two"})
    token = CancellationToken()
    provider = _ScriptedProvider(
        [ProviderTextDelta(text="working"), Finish(tool_calls=(first, second))]
    )
    app = MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session.meta.session_id,
        context_builder=ContextBuilder(data_root=data_root, workspace=workspace),
        system_prompt="stable test system",
        tool_executor=_lookup_executor(),
    )
    collected: list[EngineEvent] = []

    async def scenario() -> None:
        async for event in app.run("inspect both", cancellation=token):
            collected.append(event)
            if isinstance(event, ToolFinished) and event.call_id == "call-a":
                token.cancel()

    asyncio.run(scenario())
    assert isinstance(collected[-1], RunCancelled)
    checkpoint = service.load_session(session.meta.session_id).checkpoints[0]
    step = checkpoint.progress[0]
    assert step.tools[0].status is RunCheckpointToolStatus.SUCCESS
    assert step.tools[1].status is RunCheckpointToolStatus.NOT_STARTED
    context = service.context_snapshot(session.meta.session_id)
    assert context[1].role is MessageRole.ASSISTANT
    assert context[1].tool_calls == (first,)
    assert context[2].role is MessageRole.TOOL
    assert not any(
        message.role is MessageRole.TOOL and message.tool_call_id == "call-b"
        for message in context
    )
