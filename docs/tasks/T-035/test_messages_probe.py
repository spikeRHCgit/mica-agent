"""T-035 离线补证：独立请求基准、产品 read 和五工具出站定义。"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from sse_transport_helpers import RecordingTransport, sse_response
from test_anthropic_messages_provider import (
    block_start,
    block_stop,
    delta,
    ending,
    sample,
    start,
    text,
)

from mica_agent.continuation_contracts import ProviderApi
from mica_agent.engine import AgentEngine
from mica_agent.events import RunCompleted, ToolFinished
from mica_agent.file_tools import register_file_tools
from mica_agent.interactions import InteractionBroker
from mica_agent.models import Message, MessageRole
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine
from mica_agent.providers.anthropic_messages import (
    AnthropicMessagesProvider,
)
from mica_agent.providers.base import ProviderRequest
from mica_agent.reasoning import ReasoningEffort
from mica_agent.runtime_tools import register_runtime_tools
from mica_agent.tool_contracts import (
    ToolCall,
    ToolResultStatus,
    to_mutable_json,
)
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_registry import ToolRegistry
from mica_agent.workspace import resolve_workspace


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original_connect = socket.socket.connect

    def denied(*args, **kwargs):
        raise AssertionError("Unexpected real network access")

    def local_connect(sock, address):
        # Windows asyncio 用本地 socketpair 唤醒事件循环；仍禁止外部连接。
        assert isinstance(address, tuple) and address[0] == "127.0.0.1"
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def baseline():
    return json.loads(Path(__file__).with_name("probe_cases.json").read_text("utf-8"))


def request(messages, specs):
    return ProviderRequest(
        "MODEL_PLACEHOLDER",
        tuple(messages),
        tools=tuple(specs),
        reasoning_effort=ReasoningEffort.OFF,
        max_tokens=1024,
        provider_id="offline-probe",
        api=ProviderApi.ANTHROPIC_MESSAGES,
    )


@pytest.mark.parametrize("five_tools", [False, True])
def test_actual_read_roundtrip_and_outbound_definitions(tmp_path, five_tools):
    case = baseline()["baseline"]
    filename = "probe.txt"
    secret = uuid4().hex
    (tmp_path / filename).write_text(secret, encoding="utf-8")
    workspace = resolve_workspace(tmp_path)
    registry = ToolRegistry()
    register_file_tools(registry, workspace)
    register_runtime_tools(registry, workspace, InteractionBroker())
    read = registry.resolve("read")
    guarded = ToolRegistry()
    calls = []

    async def allowed_read(req, token):
        assert req.name == "read" and dict(req.arguments) == {"path": filename}
        assert not calls
        calls.append(req.call_id)
        return await read.handler(req, token)

    guarded.register(read.spec, allowed_read)
    specs = registry.specs() if five_tools else guarded.specs()
    assert {s.name for s in registry.specs()} == {
        "read",
        "write",
        "edit",
        "bash",
        "ask",
    }
    assert to_mutable_json(read.spec.input_schema) == case["tools"][0]["input_schema"]
    assert read.spec.description == case["tools"][0]["description"]
    chunks = [
        start(),
        block_start(
            0,
            {"type": "tool_use", "id": "probe_call_1", "name": "read", "input": {}},
        ),
        delta(0, "input_json_delta", partial_json='{"path":'),
        delta(0, "input_json_delta", partial_json='"probe.txt"}'),
        block_stop(0),
        *ending("tool_use"),
    ]

    async def scenario():
        async def handler(http_request):
            body = json.loads(http_request.content)
            assert http_request.headers["anthropic-version"] == "2023-06-01"
            assert http_request.headers["x-api-key"] == "fake-probe-key"
            assert "authorization" not in http_request.headers
            expected_tools = [
                {
                    "name": s.name,
                    "description": s.description,
                    "input_schema": to_mutable_json(s.input_schema),
                }
                for s in specs
            ]
            assert body["tools"] == expected_tools
            assert all(t["input_schema"]["type"] == "object" for t in body["tools"])
            assert all(
                set(t) == {"name", "description", "input_schema"} for t in body["tools"]
            )
            if len(transport.requests) == 1:
                if not five_tools:
                    assert body == case
                return sse_response(chunks)
            assert len(transport.requests) == 2 and calls == ["probe_call_1"]
            suffix = body["messages"][-2:]
            assert suffix[0] == {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "probe_call_1",
                        "name": "read",
                        "input": {"path": filename},
                    }
                ],
            }
            assert suffix[1]["role"] == "user"
            result = suffix[1]["content"]
            assert len(result) == 1 and result[0]["type"] == "tool_result"
            assert result[0]["tool_use_id"] == "probe_call_1"
            assert json.loads(result[0]["content"]) == {
                "status": "success",
                "message": "file read",
                "data": secret,
            }
            return sse_response(sample([text("done")]))

        transport = RecordingTransport(handler)
        provider = AnthropicMessagesProvider(
            "fake-probe-key",
            "https://probe.invalid/v1",
            transport=transport,
            max_retries=0,
        )
        executor = ToolExecutor(
            guarded, PolicyEngine(PermissionMode.DEFAULT), workspace=workspace
        )
        try:
            events = [
                e
                async for e in AgentEngine(provider, tool_executor=executor).run(
                    request(
                        [
                            Message(
                                MessageRole.USER,
                                case["messages"][0]["content"][0]["text"],
                            )
                        ],
                        specs,
                    )
                )
            ]
        finally:
            await provider.aclose()
        assert isinstance(events[-1], RunCompleted)
        finished = [e for e in events if isinstance(e, ToolFinished)]
        assert len(finished) == 1 and finished[0].status is ToolResultStatus.SUCCESS
        assert len(transport.requests) == 2
        assert all(r.is_closed for r in transport.responses)

    asyncio.run(scenario())


def test_generic_history_matches_independent_tool_suffix(tmp_path):
    case = baseline()
    registry = ToolRegistry()
    register_file_tools(registry, resolve_workspace(tmp_path))

    async def scenario():
        async def handler(http_request):
            body = json.loads(http_request.content)
            assert body == {
                **case["baseline"],
                "messages": [
                    *case["baseline"]["messages"],
                    *case["synthetic_history_suffix"],
                ],
            }
            return sse_response(sample([text()]))

        transport = RecordingTransport(handler)
        provider = AnthropicMessagesProvider(
            "fake-probe-key", transport=transport, max_retries=0
        )
        messages = [
            Message(
                MessageRole.USER, case["baseline"]["messages"][0]["content"][0]["text"]
            ),
            Message(
                MessageRole.ASSISTANT,
                "",
                (ToolCall("probe_call_1", "read", {"path": "probe.txt"}),),
            ),
            Message(
                MessageRole.TOOL,
                case["synthetic_history_suffix"][1]["content"][0]["content"],
                tool_call_id="probe_call_1",
            ),
        ]
        try:
            events = [
                e
                async for e in provider.stream(
                    request(messages, (registry.resolve("read").spec,))
                )
            ]
        finally:
            await provider.aclose()
        assert len(transport.requests) == 1
        assert events[-1].reason == "end_turn"

    asyncio.run(scenario())
