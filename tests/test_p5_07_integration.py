"""P5-07 会话 v2、Engine 转录与 MicaApp 提交跨层测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from mica_agent.app import MicaApp
from mica_agent.cancellation import CancellationToken
from mica_agent.context_builder import ContextBuilder
from mica_agent.events import EngineEvent, RunCompleted
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
from mica_agent.tool_registry import ToolRegistry
from mica_agent.workspace import resolve_workspace

NOW = datetime(2026, 8, 26, 1, 2, 3, tzinfo=UTC)


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
    assert [message.role for message in restored[:-1]] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    observed = repr(restored)
    assert secret_key not in observed
    assert bearer not in observed
    assert "[REDACTED]" in observed
