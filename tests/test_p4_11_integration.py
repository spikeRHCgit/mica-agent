"""P4-11 思考强度与独立思考事件跨层集成测试。"""

from __future__ import annotations

import asyncio
import json

import httpx

from mica_agent.app import MicaApp
from mica_agent.context_builder import ContextBuilder
from mica_agent.events import (
    EngineEvent,
    ReasoningDelta,
    RunCompleted,
    RunStarted,
    TextDelta,
)
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import Message, MessageRole
from mica_agent.providers import OpenAICompatibleProvider
from mica_agent.reasoning import ReasoningEffort
from mica_agent.session_service import SessionService
from mica_agent.workspace import resolve_workspace


def test_reasoning_crosses_transport_and_engine_but_not_session(tmp_path) -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read()))
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"reasoning_content":"private analysis","content":"formal answer"}}]}\n\n'
                "data: [DONE]\n\n"
            ),
            request=request,
        )

    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    data_root = tmp_path / "isolated-data"
    workspace = resolve_workspace(workspace_path)
    service = SessionService(JsonlSessionStore(workspace, data_root))
    session = service.create_session()
    provider = OpenAICompatibleProvider(
        "test-api-key",
        "https://api.example.test/v1",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    app = MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session.meta.session_id,
        context_builder=ContextBuilder(data_root=data_root, workspace=workspace),
        system_prompt="frozen system prompt",
        run_id_factory=lambda: "run-p4-11",
        turn_id_factory=lambda: "turn-p4-11",
    )

    async def scenario() -> list[EngineEvent]:
        events = [
            event
            async for event in app.run(
                "question", reasoning_effort=ReasoningEffort.HIGH
            )
        ]
        await app.aclose()
        return events

    events = asyncio.run(scenario())

    assert captured == [
        {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "frozen system prompt"},
                {"role": "user", "content": "question"},
            ],
            "stream": True,
            "reasoning_effort": "high",
        }
    ]
    assert events == [
        RunStarted("run-p4-11"),
        ReasoningDelta("run-p4-11", "private analysis", 1),
        TextDelta("run-p4-11", "formal answer", 2),
        RunCompleted("run-p4-11", 3),
    ]
    assert app.history == (
        Message(MessageRole.USER, "question"),
        Message(MessageRole.ASSISTANT, "formal answer"),
    )
    raw = service.store.path_for_session(session.meta.session_id).read_text(
        encoding="utf-8"
    )
    assert "private analysis" not in raw
