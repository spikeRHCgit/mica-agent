"""P5-12 工具展示与会话 v3 的跨层公共闭环测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from mica_agent.app import MicaApp
from mica_agent.context_builder import ContextBuilder
from mica_agent.events import (
    EngineEvent,
    RunCompleted,
    ToolFinished,
    ToolPrepared,
    ToolStarted,
)
from mica_agent.file_tools import register_file_tools
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import MessageRole
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine
from mica_agent.providers import Finish, ProviderEvent, ProviderRequest
from mica_agent.providers import TextDelta as ProviderTextDelta
from mica_agent.session_service import SessionService
from mica_agent.tool_contracts import ToolCall
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_presentation import ToolPresentationKind, ToolPresentationStatus
from mica_agent.tool_registry import ToolRegistry
from mica_agent.workspace import resolve_workspace


class _ScriptedProvider:
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
    provider: _ScriptedProvider,
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
        run_id_factory=lambda: "run-p5-12",
        turn_id_factory=lambda: "turn-p5-12",
    )
    return app, service, session.meta.session_id, workspace_path


def _collect(app: MicaApp, prompt: str) -> list[EngineEvent]:
    async def run() -> list[EngineEvent]:
        return [event async for event in app.run(prompt)]

    return asyncio.run(run())


def test_real_write_tool_emits_safe_snapshots_and_persists_v3(tmp_path: Path) -> None:
    provider = _ScriptedProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall(
                            "call-write",
                            "write",
                            {
                                "path": "notes.txt",
                                "content": "token=sk-p5-12-secret\n标题",
                            },
                        ),
                    )
                )
            ],
            [ProviderTextDelta("saved"), Finish()],
        ]
    )
    # The real handler is used; only the policy is automated for this test.
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

    events = _collect(app, "save the note")

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
        "token=sk-p5-12-secret\n标题"
    )

    stored = service.load_session(session_id)
    assert stored.meta.format_version == 4
    assert stored.turns[0].tool_presentations == (finished.presentation,)
    raw = service.store.path_for_session(session_id).read_text(encoding="utf-8")
    assert "sk-p5-12-secret" not in raw
    assert "[REDACTED]" in raw
    assert all(
        message.role is not MessageRole.SYSTEM
        for message in stored.turns[0].context_messages
    )


def test_real_edit_tool_persists_minimal_numbered_diff(tmp_path: Path) -> None:
    provider = _ScriptedProvider(
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

    events = _collect(app, "replace the target")

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
