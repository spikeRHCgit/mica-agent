"""P4-14 结构化 ask 的 Provider、CLI 与 v3 会话闭环测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from mica_agent.cli import run_interactive
from mica_agent.context_builder import ContextBuilder
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import MessageRole
from mica_agent.providers import Finish, ProviderEvent, ProviderRequest
from mica_agent.providers import TextDelta as ProviderTextDelta
from mica_agent.session_service import SessionService
from mica_agent.tool_contracts import ToolCall
from mica_agent.workspace import resolve_workspace


class ScriptedProvider:
    def __init__(self, scripts: list[list[ProviderEvent]]) -> None:
        self._scripts = scripts
        self.requests: list[ProviderRequest] = []
        self.aclose_calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        for event in self._scripts[len(self.requests) - 1]:
            yield event

    async def aclose(self) -> None:
        self.aclose_calls += 1


class InputScript:
    def __init__(self, *items: str) -> None:
        self._items = list(items)

    async def __call__(self) -> str:
        if not self._items:
            raise EOFError
        return self._items.pop(0)


class Recorder:
    def __init__(self) -> None:
        self.text = ""

    def __call__(self, text: str) -> None:
        self.text += text


def test_structured_ask_provider_tool_json_and_v3_continue_loop(tmp_path: Path) -> None:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace = resolve_workspace(workspace_path)
    service = SessionService(JsonlSessionStore(workspace, tmp_path / "data"))
    builder = ContextBuilder(data_root=tmp_path / "data", workspace=workspace)
    provider = ScriptedProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall(
                            "ask-call",
                            "ask",
                            {
                                "question": "Choose a format",
                                "title": "Export",
                                "options": [
                                    {"label": "JSON", "recommended": True},
                                    {"label": "YAML", "description": "Human readable"},
                                ],
                                "allow_custom": False,
                            },
                        ),
                    )
                )
            ],
            [ProviderTextDelta(text="saved"), Finish()],
            [ProviderTextDelta(text="continued"), Finish()],
        ]
    )

    async def first_run() -> tuple[int, str, str]:
        out = Recorder()
        err = Recorder()
        code = await run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            provider_factory=lambda _url, _key: provider,
            read_line=InputScript("start", " 2 "),
            write_out=out,
            write_err=err,
            session_service=service,
            context_builder=builder,
        )
        return code, out.text, err.text

    code, output, error = asyncio.run(first_run())

    assert code == 0
    assert output == "saved\n"
    assert "1. JSON (recommended)\n" in error
    assert "2. YAML\n" in error
    assert "selection: tool ask: success\n" in error
    assert provider.requests[0].tools[0].name == "ask"
    assert provider.requests[0].tools[0].input_schema["properties"]["options"] == {
        "type": "array",
        "minItems": 2,
        "maxItems": 4,
        "items": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "minLength": 1},
                "description": {"type": "string", "minLength": 1},
                "recommended": {"type": "boolean"},
            },
            "required": ("label",),
            "additionalProperties": False,
        },
    }
    assert [message.role for message in provider.requests[1].messages] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
    ]
    assert json.loads(provider.requests[1].messages[-1].content)["data"] == {
        "answer": "YAML",
        "selected_option_index": 1,
        "notes": None,
    }

    out = Recorder()
    err = Recorder()
    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            session_mode="continue",
            provider_factory=lambda _url, _key: provider,
            read_line=InputScript("next"),
            write_out=out,
            write_err=err,
            session_service=service,
            context_builder=builder,
        )
    )

    assert code == 0
    assert out.text == "continued\n"
    assert err.text == ""
    assert [message.role for message in provider.requests[2].messages] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
        MessageRole.USER,
    ]
    assert provider.requests[2].messages[-1].content == "next"

    listed = service.list_sessions()
    assert listed.problems == ()
    assert len(listed.sessions) == 1
    assert len(listed.sessions[0].turns) == 2
    assert listed.sessions[0].turns[0].context_messages[2].role is MessageRole.TOOL
    raw = next(service.store.session_directory.glob("*.jsonl")).read_text(
        encoding="utf-8"
    )
    assert '"format_version":4' in raw
    assert "YAML" in raw
    assert "sk-test-only" not in raw
    assert provider.aclose_calls == 2
