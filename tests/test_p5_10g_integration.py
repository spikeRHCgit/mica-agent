"""P5-10G2 中断检查点捕获与恢复闭环测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from mica_agent.app import MicaApp
from mica_agent.cancellation import CancellationToken
from mica_agent.checkpoint_contracts import (
    RunCheckpointKind,
    RunCheckpointToolStatus,
)
from mica_agent.context_builder import ContextBuilder
from mica_agent.events import (
    EngineEvent,
    RunCancelled,
    RunCompleted,
    RunFailed,
    ToolFinished,
)
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import Message, MessageRole
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine, PolicyInput, PolicyOperation
from mica_agent.providers import (
    AuthenticationError,
    Finish,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderRequest,
)
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


class ScriptedProvider:
    def __init__(self, scripts: list[list[ProviderEvent]]) -> None:
        self.scripts = scripts
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        for event in self.scripts[min(len(self.requests) - 1, len(self.scripts) - 1)]:
            yield event

    async def aclose(self) -> None:
        return None


def make_app(
    tmp_path: Path,
    provider: object,
    *,
    tool_executor: ToolExecutor | None = None,
    run_ids: tuple[str, ...] = ("run-p5-10g2-a", "run-p5-10g2-b"),
) -> tuple[MicaApp, SessionService, str]:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    data_root = tmp_path / "mica-data"
    workspace = resolve_workspace(workspace_path)
    service = SessionService(JsonlSessionStore(workspace, data_root))
    session = service.create_session()
    remaining = list(run_ids)
    ids = iter(("id-p5-10g2-a", "id-p5-10g2-b"))
    app = MicaApp(
        provider,  # type: ignore[arg-type]
        "test-model",
        session_service=service,
        session_id=session.meta.session_id,
        context_builder=ContextBuilder(data_root=data_root, workspace=workspace),
        system_prompt="stable test system",
        tool_executor=tool_executor,
        run_id_factory=remaining.pop,
        turn_id_factory=lambda: next(ids),
    )
    return app, service, session.meta.session_id


def collect(app: MicaApp, prompt: str) -> list[EngineEvent]:
    async def run() -> list[EngineEvent]:
        return [event async for event in app.run(prompt)]

    return asyncio.run(run())


async def collect_with_cancellation(
    app: MicaApp,
    prompt: str,
    collected: list[EngineEvent],
    *,
    cancellation: CancellationToken,
) -> None:
    async for event in app.run(prompt, cancellation=cancellation):
        collected.append(event)


def lookup_executor() -> ToolExecutor:
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


def test_cancelled_partial_text_is_restored_without_forged_assistant(
    tmp_path: Path,
) -> None:
    class HangingProvider:
        def __init__(self) -> None:
            self.requests: list[ProviderRequest] = []
            self.emitted = asyncio.Event()

        async def stream(
            self, request: ProviderRequest
        ) -> AsyncIterator[ProviderEvent]:
            self.requests.append(request)
            yield ProviderTextDelta("partial answer")
            self.emitted.set()
            await asyncio.sleep(30)
            yield Finish()

        async def aclose(self) -> None:
            return None

    hanging = HangingProvider()
    app, service, session_id = make_app(tmp_path, hanging)
    collected: list[EngineEvent] = []

    async def cancel_run() -> None:
        token = CancellationToken()
        task = asyncio.create_task(
            collect_with_cancellation(
                app, "interrupt me", collected, cancellation=token
            )
        )
        await asyncio.wait_for(hanging.emitted.wait(), timeout=1.0)
        token.cancel()
        await task

    asyncio.run(cancel_run())
    assert isinstance(collected[-1], RunCancelled)
    stored = service.load_session(session_id)
    assert stored.turns == ()
    assert stored.checkpoints[0].kind is RunCheckpointKind.CANCELLED
    assert stored.checkpoints[0].partial_assistant_message == Message(
        MessageRole.ASSISTANT, "partial answer"
    )

    follow = ScriptedProvider([[ProviderTextDelta("continued"), Finish()]])
    restored = MicaApp(
        follow,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=ContextBuilder(
            data_root=service.store.data_root,
            workspace=service.store.workspace,
        ),
        system_prompt="stable test system",
    )
    events = collect(restored, "continue")
    assert isinstance(events[-1], RunCompleted)
    restored_messages = follow.requests[0].messages
    assert restored_messages[-1] == Message(MessageRole.USER, "continue")
    assert restored_messages[-4] == Message(MessageRole.USER, "interrupt me")
    assert restored_messages[-3].role is MessageRole.SYSTEM
    assert "partial answer" in restored_messages[-3].content
    assert restored_messages[-2].role is MessageRole.SYSTEM
    assert "incomplete" in restored_messages[-2].content
    assert not any(
        message.role is MessageRole.ASSISTANT and message.content == "partial answer"
        for message in restored_messages
    )


def test_tool_batch_cancel_keeps_completed_prefix_and_unstarted_call(
    tmp_path: Path,
) -> None:
    first = ToolCall("call-a", "lookup", {"query": "one"})
    second = ToolCall("call-b", "lookup", {"query": "two"})
    token = CancellationToken()
    provider = ScriptedProvider(
        [[ProviderTextDelta("working"), Finish(tool_calls=(first, second))]]
    )
    app, service, session_id = make_app(
        tmp_path, provider, tool_executor=lookup_executor()
    )
    collected: list[EngineEvent] = []

    async def scenario() -> None:
        async for event in app.run("inspect both", cancellation=token):
            collected.append(event)
            if isinstance(event, ToolFinished) and event.call_id == "call-a":
                token.cancel()

    asyncio.run(scenario())
    assert isinstance(collected[-1], RunCancelled)
    checkpoint = service.load_session(session_id).checkpoints[0]
    step = checkpoint.progress[0]
    assert step.tools[0].status is RunCheckpointToolStatus.SUCCESS
    assert step.tools[1].status is RunCheckpointToolStatus.NOT_STARTED
    context = service.context_snapshot(session_id)
    assert context[1].role is MessageRole.ASSISTANT
    assert context[1].tool_calls == (first,)
    assert context[2].role is MessageRole.TOOL
    assert not any(
        message.role is MessageRole.TOOL and message.tool_call_id == "call-b"
        for message in context
    )


def test_auth_failure_checkpoint_then_successful_turn(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderTextDelta("partial"),
                ProviderErrorEvent(AuthenticationError("auth")),
            ],
            [ProviderTextDelta("recovered"), Finish()],
        ]
    )
    app, service, session_id = make_app(tmp_path, provider)
    first = collect(app, "question")
    assert isinstance(first[-1], RunFailed)
    second = collect(app, "again")
    assert isinstance(second[-1], RunCompleted)
    stored = service.load_session(session_id)
    assert stored.checkpoints[0].kind is RunCheckpointKind.RECOVERABLE_FAILURE
    assert stored.turns[0].assistant_message.content == "recovered"
    follow = provider.requests[1].messages
    assert follow[-1] == Message(MessageRole.USER, "again")
    assert any(
        message.role is MessageRole.SYSTEM and "recoverable failure" in message.content
        for message in follow
    )
