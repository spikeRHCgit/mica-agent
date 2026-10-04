"""验证 reasoning 在 transport、engine 与 App 之间传递，但不持久化到 session。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

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


@pytest.mark.parametrize("mapped_high", ["high", "vendor-high"])
def test_reasoning_crosses_transport_and_engine_but_not_session(
    tmp_path, mapped_high
) -> None:
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
        thinking_level_map={ReasoningEffort.HIGH: mapped_high},
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
            "max_tokens": 4096,
            "reasoning_effort": mapped_high,
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


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("effort", [ReasoningEffort.HIGH, ReasoningEffort.OFF])
def test_http_tool_roundtrips_preserve_mapping_and_next_run_settings(
    tmp_path, configured, effort
):
    from test_config_service import make_service

    from mica_agent.permissions import PermissionMode
    from mica_agent.policy import PolicyEngine
    from mica_agent.session_contracts import SessionSelection
    from mica_agent.tool_contracts import ToolResult, ToolResultStatus, ToolSpec
    from mica_agent.tool_executor import ToolExecutor
    from mica_agent.tool_registry import ToolRegistry

    configuration = make_service(tmp_path)
    root = configuration.store.data_root
    config_path = root / "config.json"
    config_path.write_text(
        json.dumps({"reasoningEffort": effort.value, "permissionMode": "solo"})
    )
    models_path = root / "models.json"
    directory = json.loads(models_path.read_text())
    directory["providers"]["local"]["models"][0]["thinkingLevelMap"] = {
        "high": "configured-high"
    }
    models_path.write_text(json.dumps(directory))
    connection = configuration.resolve(
        configuration.load(), explicit=SessionSelection("local", "a")
    ).connection
    captured = []

    def handler(request):
        captured.append(json.loads(request.read()))
        delta = (
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-probe",
                        "type": "function",
                        "function": {"name": "probe", "arguments": "{}"},
                    }
                ]
            }
            if len(captured) == 1
            else {"content": "answer"}
        )
        finish = "tool_calls" if len(captured) == 1 else "stop"
        return httpx.Response(
            200,
            text=(
                "data: "
                + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]})
                + "\n\n"
                + "data: [DONE]\n\n"
            ),
            request=request,
        )

    workspace = resolve_workspace(configuration.store.workspace)
    service = SessionService(JsonlSessionStore(workspace, root))
    session = service.create_session()
    registry = ToolRegistry()
    executed = []

    async def probe(request, cancellation):
        executed.append(request.call_id)
        config_path.write_text('{"reasoningEffort":"off","permissionMode":"solo"}')
        directory["providers"]["local"]["models"][0]["thinkingLevelMap"] = {
            "high": "changed-high"
        }
        models_path.write_text(json.dumps(directory))
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "done"
        )

    registry.register(ToolSpec("probe", "Probe", {"type": "object"}), probe)
    provider = OpenAICompatibleProvider(
        "TEST_ONLY_KEY",
        "https://example.test/v1",
        transport=httpx.MockTransport(handler),
        max_retries=0,
        thinking_level_map={ReasoningEffort.HIGH: "vendor-high"},
    )
    app = MicaApp(
        provider,
        "a",
        session_service=service,
        session_id=session.meta.session_id,
        context_builder=ContextBuilder(data_root=root, workspace=workspace),
        tool_executor=ToolExecutor(registry, PolicyEngine(PermissionMode.SOLO)),
        config_service=configuration if configured else None,
        connection=connection if configured else None,
    )

    async def scenario():
        try:
            first = [
                event
                async for event in app.run(
                    "first", **({} if configured else {"reasoning_effort": effort})
                )
            ]
            second = [event async for event in app.run("second")]
            assert isinstance(first[-1], RunCompleted) and isinstance(
                second[-1], RunCompleted
            )
            config_path.write_text('{"reasoningEffort":"high","permissionMode":"solo"}')
            third = [
                event
                async for event in app.run(
                    "third",
                    **(
                        {} if configured else {"reasoning_effort": ReasoningEffort.HIGH}
                    ),
                )
            ]
            assert isinstance(third[-1], RunCompleted)
        finally:
            await app.aclose()

    asyncio.run(scenario())
    assert executed == ["call-probe"]
    expected = (
        None
        if effort is ReasoningEffort.OFF
        else "configured-high"
        if configured
        else "vendor-high"
    )
    assert [payload.get("reasoning_effort") for payload in captured] == [
        expected,
        expected,
        None,
        "changed-high" if configured else "vendor-high",
    ]
    assert captured[1]["messages"][-1] == {
        "role": "tool",
        "content": '{"status":"success","message":"done","data":null}',
        "tool_call_id": "call-probe",
    }
    assert len(service.load_session(session.meta.session_id).turns) == 3
