"""工具执行、安全持久化、恢复与取消批次检查点。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from mica_agent.app import MicaApp
from mica_agent.cancellation import CancellationToken
from mica_agent.checkpoint_contracts import RunCheckpointToolStatus
from mica_agent.context_builder import ContextBuilder
from mica_agent.events import (
    EngineEvent,
    RunCancelled,
    RunCompleted,
    ToolFinished,
    ToolPrepared,
    ToolStarted,
)
from mica_agent.file_tools import register_file_tools
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import Message, MessageRole
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
from mica_agent.tool_presentation import ToolPresentationKind, ToolPresentationStatus
from mica_agent.tool_registry import ToolRegistry
from mica_agent.workspace import resolve_workspace

NOW = datetime(2026, 8, 26, 1, 2, 3, tzinfo=UTC)


class _ScriptedProvider:
    """为每个请求返回固定事件脚本；每个测试只运行一轮。"""

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


class ScriptedProvider:
    def __init__(self, scripts: list[list[ProviderEvent]]) -> None:
        self.scripts = scripts
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        for event in self.scripts[len(self.requests) - 1]:
            yield event

    async def aclose(self) -> None:
        return None


def make_components(
    tmp_path: Path,
) -> tuple[SessionService, str, ContextBuilder]:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace = resolve_workspace(workspace_path)
    data_root = tmp_path / "mica-data"
    service = SessionService(
        JsonlSessionStore(workspace, data_root),
        now=lambda: NOW,
        session_id_factory=lambda: "session-v2",
    )
    session = service.create_session()
    return (
        service,
        session.meta.session_id,
        ContextBuilder(data_root=data_root, workspace=workspace),
    )


def collect(app: MicaApp, prompt: str) -> list[EngineEvent]:
    async def run() -> list[EngineEvent]:
        return [event async for event in app.run(prompt)]

    return asyncio.run(run())


def test_tool_failure_recovered_by_model_commits_complete_history(
    tmp_path: Path,
) -> None:
    service, session_id, builder = make_components(tmp_path)
    executor = ToolExecutor(ToolRegistry(), PolicyEngine(PermissionMode.DEFAULT))
    call = ToolCall("call-missing", "read", {})
    provider = ScriptedProvider(
        [
            [Finish(tool_calls=(call,))],
            [ProviderTextDelta("I could not read it"), Finish()],
        ]
    )
    app = MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
        tool_executor=executor,
        run_id_factory=lambda: "run-failure-recovered",
        turn_id_factory=lambda: "turn-failure-recovered",
        clock=lambda: NOW,
    )

    events = collect(app, "inspect missing")

    assert isinstance(events[-1], RunCompleted)
    turn = service.load_session(session_id).turns[0]
    assert turn.context_messages[1] == Message(MessageRole.ASSISTANT, "", (call,))
    assert turn.context_messages[2].role is MessageRole.TOOL
    assert '"status":"unknown_tool"' in turn.context_messages[2].content
    assert turn.assistant_message.content == "I could not read it"
    assert executor.tool_specs == ()


def test_exit_reload_restores_redacted_complete_tool_context(tmp_path: Path) -> None:
    service, session_id, builder = make_components(tmp_path)
    secret_key = "sk-session-sensitive-credential"
    bearer = "Bearer session-private-token"
    call = ToolCall(
        "call-1",
        "read",
        {"path": "notes.txt", "api_key": secret_key},
    )
    spec = ToolSpec("read", "Read", {"type": "object"})
    registry = ToolRegistry()

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        return ToolResult(
            request.call_id,
            request.name,
            ToolResultStatus.SUCCESS,
            "read complete",
            {"authorization": bearer, "content": "safe result"},
        )

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.READ, False, False
        ),
    )
    first_provider = ScriptedProvider(
        [
            [Finish(tool_calls=(call,))],
            [ProviderTextDelta("first answer"), Finish()],
        ]
    )
    first_app = MicaApp(
        first_provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
        tool_executor=executor,
        run_id_factory=lambda: "run-first",
        turn_id_factory=lambda: "turn-first",
        clock=lambda: NOW,
    )

    first_events = collect(first_app, "inspect")

    assert isinstance(first_events[-1], RunCompleted)
    raw = service.store.path_for_session(session_id).read_bytes()
    assert secret_key.encode() not in raw
    assert bearer.encode() not in raw
    assert b"[REDACTED]" in raw

    second_provider = ScriptedProvider([[ProviderTextDelta("second answer"), Finish()]])
    second_app = MicaApp(
        second_provider,
        "test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
        run_id_factory=lambda: "run-second",
        turn_id_factory=lambda: "turn-second",
        clock=lambda: NOW,
    )

    second_events = collect(second_app, "continue")

    assert isinstance(second_events[-1], RunCompleted)
    restored = second_provider.requests[0].messages
    assert restored[-1] == Message(MessageRole.USER, "continue")
    assert restored[0].role is MessageRole.SYSTEM
    assert [message.role for message in restored[1:-1]] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    observed = repr(restored)
    assert secret_key not in observed
    assert bearer not in observed
    assert "[REDACTED]" in observed


class _PresentationProvider:
    def __init__(self, scripts: list[list[ProviderEvent]]) -> None:
        self.scripts = scripts
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        for event in self.scripts[min(len(self.requests) - 1, len(self.scripts) - 1)]:
            yield event

    async def aclose(self) -> None:
        pass


def _make_app(
    tmp_path: Path,
    provider: _PresentationProvider,
    *,
    tool_executor: ToolExecutor,
    workspace_path: Path | None = None,
) -> tuple[MicaApp, SessionService, str, Path]:
    if workspace_path is None:
        workspace_path = tmp_path / "workspace"
        workspace_path.mkdir()
    data_root = tmp_path / "mica-data"
    workspace = resolve_workspace(workspace_path)
    service = SessionService(JsonlSessionStore(workspace, data_root))
    session = service.create_session()
    app = MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session.meta.session_id,
        context_builder=ContextBuilder(data_root=data_root, workspace=workspace),
        system_prompt="stable test system",
        tool_executor=tool_executor,
        run_id_factory=lambda: "run-p4-12",
        turn_id_factory=lambda: "turn-p4-12",
    )
    return app, service, session.meta.session_id, workspace_path


def test_real_write_tool_emits_safe_snapshots_and_persists_v3(tmp_path: Path) -> None:
    provider = _PresentationProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall(
                            "call-write",
                            "write",
                            {
                                "path": "notes.txt",
                                "content": "token=sk-p4-12-secret\n标题",
                            },
                        ),
                    )
                )
            ],
            [ProviderTextDelta("saved"), Finish()],
        ]
    )
    # 使用真实 handler；本测试只自动化处理策略。
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(workspace_path))
    executor = ToolExecutor(registry, PolicyEngine(PermissionMode.SOLO))
    app, service, session_id, workspace_path = _make_app(
        tmp_path,
        provider,
        tool_executor=executor,
        workspace_path=workspace_path,
    )

    events = collect(app, "save the note")

    assert isinstance(events[-1], RunCompleted)
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
    assert prepared.presentation.payload == {
        "path": "notes.txt",
        "content": "token=[REDACTED]\n标题",
    }
    assert isinstance(finished, ToolFinished)
    assert finished.presentation is not None
    assert finished.presentation.kind is ToolPresentationKind.WRITE
    assert finished.presentation.status is ToolPresentationStatus.SUCCESS
    assert finished.presentation.payload == {
        "path": "notes.txt",
        "content": "token=[REDACTED]\n标题",
    }
    assert (workspace_path / "notes.txt").read_text(encoding="utf-8") == (
        "token=sk-p4-12-secret\n标题"
    )

    stored = service.load_session(session_id)
    assert stored.meta.format_version == 5
    assert stored.turns[0].tool_presentations == (finished.presentation,)
    raw = service.store.path_for_session(session_id).read_text(encoding="utf-8")
    assert "sk-p4-12-secret" not in raw
    assert "[REDACTED]" in raw
    assert all(
        message.role is not MessageRole.SYSTEM
        for message in stored.turns[0].context_messages
    )


def test_real_edit_tool_persists_minimal_numbered_diff(tmp_path: Path) -> None:
    provider = _PresentationProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall(
                            "call-edit",
                            "edit",
                            {
                                "path": "notes.txt",
                                "old_text": "TARGET",
                                "new_text": "done",
                            },
                        ),
                    )
                )
            ],
            [ProviderTextDelta("edited"), Finish()],
        ]
    )
    registry = ToolRegistry()
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    (workspace_path / "notes.txt").write_text("first\nTARGET\nlast\n", encoding="utf-8")
    register_file_tools(registry, resolve_workspace(workspace_path))
    app, service, session_id, _ = _make_app(
        tmp_path,
        provider,
        tool_executor=ToolExecutor(registry, PolicyEngine(PermissionMode.SOLO)),
        workspace_path=workspace_path,
    )

    events = collect(app, "replace the target")

    finished = next(event for event in events if isinstance(event, ToolFinished))
    assert finished.presentation is not None
    assert finished.presentation.payload["lines"] == (
        {"kind": "context", "text": "first", "old_line": 1, "new_line": 1},
        {"kind": "removed", "text": "TARGET", "old_line": 2, "new_line": None},
        {"kind": "added", "text": "done", "old_line": None, "new_line": 2},
        {"kind": "context", "text": "last", "old_line": 3, "new_line": 3},
    )
    stored = service.load_session(session_id)
    assert stored.turns[0].tool_presentations[0] == finished.presentation
