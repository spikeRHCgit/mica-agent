"""P4-13C2 基础 CLI 动态 Bash 输出集成测试。"""

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
        self.calls: list[str] = []

    def __call__(self, text: str) -> None:
        self.calls.append(text)

    @property
    def text(self) -> str:
        return "".join(self.calls)


def _dependencies(tmp_path: Path) -> tuple[SessionService, ContextBuilder, Path]:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace = resolve_workspace(workspace_path)
    data_root = tmp_path / "data"
    service = SessionService(JsonlSessionStore(workspace, data_root))
    return (
        service,
        ContextBuilder(data_root=data_root, workspace=workspace),
        workspace_path,
    )


def _run(
    tmp_path: Path,
    command: str,
    response: str = "final answer",
    *,
    confirm: bool = False,
) -> tuple[int, Recorder, Recorder, ScriptedProvider, Path, SessionService]:
    service, builder, workspace_path = _dependencies(tmp_path)
    provider = ScriptedProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall(
                            "cli-bash-call",
                            "bash",
                            {"command": command},
                        ),
                    )
                )
            ],
            [ProviderTextDelta(response), Finish()],
        ]
    )
    out = Recorder()
    err = Recorder()
    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            provider_factory=lambda _url, _key: provider,
            read_line=InputScript("run command", *(["y"] if confirm else [])),
            write_out=out,
            write_err=err,
            session_service=service,
            context_builder=builder,
        )
    )
    return code, out, err, provider, workspace_path, service


def test_cli_displays_cleaned_bash_deltas_between_status_lines(
    tmp_path: Path,
) -> None:
    secret = "sk-cli-output-secret-sentinel"
    code, out, err, provider, _workspace, service = _run(
        tmp_path,
        "printf 'first output\\n'; "
        "printf 'api_key=%s\\n' '" + secret + "'; "
        "printf 'second output\\n' >&2",
        confirm=True,
    )

    assert code == 0
    assert out.text == "final answer\n"
    assert err.text == (
        "permission required for tool bash [y/N]: "
        "tool bash: started\n"
        "first output\n"
        "api_key=[REDACTED]\n"
        "second output\n"
        "tool bash: success\n"
    )
    assert secret not in out.text + err.text
    assert provider.aclose_calls == 1
    assert len(provider.requests) == 2
    assert tuple(spec.name for spec in provider.requests[0].tools) == (
        "ask",
        "bash",
        "edit",
        "read",
        "write",
    )
    assert provider.requests[1].messages[-1].role is MessageRole.TOOL
    tool_result = json.loads(provider.requests[1].messages[-1].content)
    assert tool_result["data"]["output"] == (
        "first output\napi_key=[REDACTED]\nsecond output\n"
    )
    raw_session = next(service.store.session_directory.glob("*.jsonl")).read_text(
        encoding="utf-8"
    )
    assert secret not in raw_session
    assert "ToolOutputDelta" not in raw_session
    assert "Traceback" not in raw_session


def test_cli_adds_one_separator_for_unterminated_bash_delta(
    tmp_path: Path,
) -> None:
    code, out, err, _provider, _workspace, _service = _run(
        tmp_path,
        "printf 'unterminated output'",
    )

    assert code == 0
    assert out.text == "final answer\n"
    assert err.text == ("tool bash: started\nunterminated output\ntool bash: success\n")
    assert err.text.count("unterminated output") == 1


def test_cli_keeps_no_output_tool_status_without_extra_blank_line(
    tmp_path: Path,
) -> None:
    code, out, err, _provider, _workspace, _service = _run(tmp_path, ":")

    assert code == 0
    assert out.text == "final answer\n"
    assert err.text == ("tool bash: started\ntool bash: success\n")
    assert "started\n\n" not in err.text
