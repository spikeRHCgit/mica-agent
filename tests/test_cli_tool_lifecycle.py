"""CLI 工具生命周期：交互、安全输出、取消与会话恢复。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mica_agent.app import MicaApp
from mica_agent.cli import run_interactive
from mica_agent.context_builder import ContextBuilder
from mica_agent.events import PermissionRequested, ToolStarted, UserInputRequested
from mica_agent.file_tools import register_file_tools
from mica_agent.interactions import InteractionBroker
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import MessageRole
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine
from mica_agent.providers import (
    AuthenticationError,
    Finish,
    ProviderErrorEvent,
    ProviderEvent,
    ProviderRequest,
)
from mica_agent.providers import TextDelta as ProviderTextDelta
from mica_agent.runtime_tools import register_runtime_tools
from mica_agent.session_service import SessionService
from mica_agent.tool_contracts import ToolCall
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_registry import ToolRegistry
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
    def __init__(self, *items: object) -> None:
        self._items = list(items)

    async def __call__(self) -> str:
        if not self._items:
            raise EOFError
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, str)
        return item


class BlockingInput:
    def __init__(self, first: str) -> None:
        self._first = first
        self._used = False
        self.waiting = asyncio.Event()

    async def __call__(self) -> str:
        if not self._used:
            self._used = True
            return self._first
        self.waiting.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str) -> None:
        self.calls.append(text)

    @property
    def text(self) -> str:
        return "".join(self.calls)


def make_dependencies(
    tmp_path: Path,
) -> tuple[SessionService, ContextBuilder, Path]:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace = resolve_workspace(workspace_path)
    data_root = tmp_path / "data"
    service = SessionService(JsonlSessionStore(workspace, data_root))
    builder = ContextBuilder(data_root=data_root, workspace=workspace)
    return service, builder, workspace_path


@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("answer", ["extra detail", ""])
def test_ask_answers_are_tool_results_including_empty_answer(
    tmp_path: Path, answer: str, mode: PermissionMode
) -> None:
    service, builder, _workspace_path = make_dependencies(tmp_path)
    provider = ScriptedProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall("ask-call", "ask", {"question": "Need detail?"}),
                    )
                )
            ],
            [ProviderTextDelta("answer complete"), Finish()],
        ]
    )
    out = Recorder()
    err = Recorder()

    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            provider_factory=lambda url, key: provider,
            read_line=InputScript("ask me", answer),
            permission_mode=mode,
            write_out=out,
            write_err=err,
            session_service=service,
            context_builder=builder,
        )
    )

    assert code == 0
    assert out.text == "answer complete\n"
    assert "Need detail?\n" in err.text
    assert provider.requests[1].messages[-1].role is MessageRole.TOOL
    result = json.loads(provider.requests[1].messages[-1].content)
    assert result["status"] == "success"
    assert result["data"] == {
        "answer": answer,
        "selected_option_index": None,
        "notes": None,
    }
    assert provider.requests[1].messages[-1].tool_call_id == "ask-call"


@pytest.mark.parametrize(
    "permission_mode", [PermissionMode.DEFAULT, PermissionMode.SOLO]
)
def test_modes_allow_write_edit_and_bash_without_permission_prompt(
    tmp_path: Path, permission_mode: PermissionMode
) -> None:
    service, builder, workspace_path = make_dependencies(tmp_path)
    provider = ScriptedProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall(
                            "write-direct",
                            "write",
                            {"path": "direct.txt", "content": "created"},
                        ),
                    )
                )
            ],
            [
                Finish(
                    tool_calls=(
                        ToolCall(
                            "edit-direct",
                            "edit",
                            {
                                "path": "direct.txt",
                                "old_text": "created",
                                "new_text": "edited",
                            },
                        ),
                    )
                )
            ],
            [
                Finish(
                    tool_calls=(
                        ToolCall("bash-denied", "bash", {"command": "echo hidden"}),
                    )
                )
            ],
            [ProviderTextDelta("finished safely"), Finish()],
        ]
    )
    out = Recorder()
    err = Recorder()

    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            permission_mode=permission_mode,
            provider_factory=lambda url, key: provider,
            read_line=InputScript("perform task"),
            write_out=out,
            write_err=err,
            session_service=service,
            context_builder=builder,
        )
    )

    assert code == 0
    assert (workspace_path / "direct.txt").read_text(encoding="utf-8") == "edited"
    assert "permission required" not in err.text
    assert "tool write: started\n" in err.text
    assert "tool edit: started\n" in err.text
    assert "tool bash: started\n" in err.text
    assert "tool bash: success\n" in err.text
    assert out.text == "finished safely\n"


@pytest.mark.parametrize("answer", ["n", "no", ""])
def test_default_cli_denies_write_and_returns_result_to_model(
    tmp_path: Path, answer: str
) -> None:
    service, builder, workspace_path = make_dependencies(tmp_path)
    provider = ScriptedProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall(
                            "write-denied",
                            "write",
                            {"path": ".env", "content": "must not exist"},
                        ),
                    )
                )
            ],
            [ProviderTextDelta("write declined"), Finish()],
        ]
    )
    out = Recorder()
    err = Recorder()

    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            provider_factory=lambda url, key: provider,
            read_line=InputScript("try write", answer),
            write_out=out,
            write_err=err,
            session_service=service,
            context_builder=builder,
        )
    )

    assert code == 0
    assert not (workspace_path / ".env").exists()
    assert len(provider.requests) == 2
    result_message = provider.requests[1].messages[-1]
    assert result_message.role is MessageRole.TOOL
    assert '"status":"permission_denied"' in result_message.content
    assert out.text == "write declined\n"
    assert err.text == (
        "permission required for tool write [y/N]: tool write: permission_denied\n"
    )


@pytest.mark.parametrize("answer", ["y", " YES "])
def test_default_cli_prompts_once_then_writes_with_safe_status_output(
    tmp_path: Path, answer: str
) -> None:
    service, builder, workspace_path = make_dependencies(tmp_path)
    sensitive = "sk-cli-tool-sensitive-sentinel"
    provider = ScriptedProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall(
                            "private-call-id",
                            "write",
                            {"path": ".env", "content": sensitive},
                        ),
                    )
                )
            ],
            [ProviderTextDelta("write complete"), Finish()],
        ]
    )
    out = Recorder()
    err = Recorder()

    async def scenario() -> int:
        return await asyncio.wait_for(
            run_interactive(
                "https://api.example.test/v1",
                "sk-test-only",
                "test-model",
                provider_factory=lambda url, key: provider,
                read_line=InputScript("create file", answer),
                write_out=out,
                write_err=err,
                session_service=service,
                context_builder=builder,
            ),
            timeout=0.5,
        )

    code = asyncio.run(scenario())

    assert code == 0
    assert (workspace_path / ".env").read_text(encoding="utf-8") == sensitive
    assert out.text == "write complete\n"
    assert err.text == (
        "permission required for tool write [y/N]: "
        "tool write: started\n"
        "tool write: success\n"
    )
    observed = out.text + err.text
    assert "private-call-id" not in observed
    assert ".env" not in observed
    assert sensitive not in observed


def test_default_cli_exposes_five_tools_and_recovers_from_unknown_ls(
    tmp_path: Path,
) -> None:
    service, builder, workspace_path = make_dependencies(tmp_path)
    (workspace_path / "note.txt").write_text("safe contents", encoding="utf-8")
    provider = ScriptedProvider(
        [
            [Finish(tool_calls=(ToolCall("read-1", "read", {"path": "note.txt"}),))],
            [Finish(tool_calls=(ToolCall("ls-1", "ls", {"path": "."}),))],
            [ProviderTextDelta("inspection complete"), Finish()],
        ]
    )
    out = Recorder()
    err = Recorder()

    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            provider_factory=lambda url, key: provider,
            read_line=InputScript("inspect note"),
            write_out=out,
            write_err=err,
            session_service=service,
            context_builder=builder,
        )
    )

    assert code == 0
    assert provider.aclose_calls == 1
    assert tuple(spec.name for spec in provider.requests[0].tools) == (
        "ask",
        "bash",
        "edit",
        "read",
        "write",
    )
    assert len(provider.requests) == 3
    for request in provider.requests[1:]:
        assert tuple(spec.name for spec in request.tools) == (
            "ask",
            "bash",
            "edit",
            "read",
            "write",
        )
    assert provider.requests[1].messages[-1].role is MessageRole.TOOL
    assert '"status":"success"' in provider.requests[1].messages[-1].content
    assert provider.requests[2].messages[-1].role is MessageRole.TOOL
    assert '"status":"unknown_tool"' in provider.requests[2].messages[-1].content
    assert "tool is not available" in provider.requests[2].messages[-1].content
    assert "safe contents" not in provider.requests[2].messages[-1].content
    assert out.text == "inspection complete\n"
    assert "permission required" not in err.text.lower()


def test_successful_tool_run_persists_complete_history_for_next_run(
    tmp_path: Path,
) -> None:
    service, builder, workspace_path = make_dependencies(tmp_path)
    provider = ScriptedProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall(
                            "persist-write",
                            "write",
                            {"path": "saved.txt", "content": "saved"},
                        ),
                    )
                )
            ],
            [ProviderTextDelta("first answer"), Finish()],
            [ProviderTextDelta("second answer"), Finish()],
        ]
    )
    out = Recorder()
    err = Recorder()

    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            provider_factory=lambda url, key: provider,
            read_line=InputScript("save it", "continue"),
            write_out=out,
            write_err=err,
            session_service=service,
            context_builder=builder,
        )
    )

    assert code == 0
    session = service.list_sessions().sessions[0]
    assert len(session.turns) == 2
    first_turn = session.turns[0]
    assert [message.role for message in first_turn.context_messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    assert (workspace_path / "saved.txt").read_text(encoding="utf-8") == "saved"
    assert provider.requests[2].messages[-1].content == "continue"
    assert provider.requests[2].messages[0].role is MessageRole.SYSTEM
    assert [message.role for message in provider.requests[2].messages[1:-1]] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]


def test_side_effect_failure_does_not_commit_and_warns_to_check_workspace(
    tmp_path: Path,
) -> None:
    service, builder, workspace_path = make_dependencies(tmp_path)
    provider = ScriptedProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall(
                            "failed-write",
                            "write",
                            {"path": "partial.txt", "content": "changed"},
                        ),
                    )
                )
            ],
            [ProviderErrorEvent(error=AuthenticationError("provider failed"))],
        ]
    )
    out = Recorder()
    err = Recorder()

    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            provider_factory=lambda url, key: provider,
            read_line=InputScript("write then fail"),
            write_out=out,
            write_err=err,
            session_service=service,
            context_builder=builder,
        )
    )

    assert code == 0
    assert (workspace_path / "partial.txt").read_text(encoding="utf-8") == "changed"
    assert service.list_sessions().sessions[0].turns == ()
    assert "check the workspace" in err.text


@pytest.mark.parametrize("invalid", ["sure", "ｙ", "１"])
def test_permission_prompt_retries_non_ascii_or_unknown_answers(
    tmp_path: Path, invalid: str
) -> None:
    service, builder, workspace_path = make_dependencies(tmp_path)
    provider = ScriptedProvider(
        [
            [
                Finish(
                    tool_calls=(
                        ToolCall(
                            "retry-write",
                            "write",
                            {"path": ".env", "content": "ok"},
                        ),
                    )
                )
            ],
            [ProviderTextDelta("done"), Finish()],
        ]
    )
    err = Recorder()

    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            provider_factory=lambda url, key: provider,
            read_line=InputScript("write", invalid, "yes"),
            write_out=Recorder(),
            write_err=err,
            session_service=service,
            context_builder=builder,
        )
    )

    assert code == 0
    assert (workspace_path / ".env").read_text(encoding="utf-8") == "ok"
    assert err.text.count("permission required for tool write") == 2
    assert "please answer y or n\n" in err.text


@pytest.mark.parametrize(
    ("tool_name", "arguments", "waiting_event", "target_name"),
    [
        (
            "write",
            {"path": ".env", "content": "must not be written"},
            PermissionRequested,
            ".env",
        ),
        (
            "ask",
            {"question": "Wait for an answer?"},
            UserInputRequested,
            None,
        ),
    ],
    ids=["permission-requested", "user-input-requested"],
)
def test_consumer_closing_public_app_stream_cleans_interaction_wait(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, str],
    waiting_event: type[PermissionRequested | UserInputRequested],
    target_name: str | None,
) -> None:
    service, builder, workspace_path = make_dependencies(tmp_path)
    session = service.create_session()
    workspace = resolve_workspace(workspace_path)
    broker = InteractionBroker()
    registry = ToolRegistry()
    register_file_tools(registry, workspace)
    register_runtime_tools(registry, workspace, broker, bash_executable="bash.exe")
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        interaction_broker=broker,
    )
    provider = ScriptedProvider(
        [[Finish(tool_calls=(ToolCall("consumer-close", tool_name, arguments),))]]
    )
    app = MicaApp(
        provider,
        "test-model",
        session_service=service,
        session_id=session.meta.session_id,
        context_builder=builder,
        tool_executor=executor,
    )
    observed: list[object] = []

    async def scenario() -> None:
        stream = app.run("start interaction")
        try:
            while True:
                event = await asyncio.wait_for(anext(stream), timeout=0.5)
                observed.append(event)
                if isinstance(event, waiting_event):
                    break

            assert broker.pending_count == 1
            await asyncio.wait_for(stream.aclose(), timeout=0.5)
            assert broker.pending_count == 0
            assert {
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and not task.done()
            } == set()
            assert provider.aclose_calls == 0
        finally:
            await asyncio.wait_for(stream.aclose(), timeout=0.5)
            await asyncio.wait_for(app.aclose(), timeout=0.5)

    asyncio.run(scenario())

    assert any(isinstance(event, waiting_event) for event in observed)
    if tool_name == "write":
        assert not any(isinstance(event, ToolStarted) for event in observed)
    if target_name is not None:
        assert not (workspace_path / target_name).exists()
    stored = service.list_sessions().sessions
    assert len(stored) == 1
    assert stored[0].turns == ()
    assert broker.pending_count == 0
    assert provider.aclose_calls == 1


@pytest.mark.parametrize("tool_name", ["write", "ask"])
def test_external_task_cancellation_during_interaction_closes_cleanly(
    tmp_path: Path, tool_name: str
) -> None:
    service, builder, workspace_path = make_dependencies(tmp_path)
    arguments = (
        {"path": ".env", "content": "not written"}
        if tool_name == "write"
        else {"question": "Wait here?"}
    )
    provider = ScriptedProvider(
        [[Finish(tool_calls=(ToolCall("cancel-call", tool_name, arguments),))]]
    )
    err = Recorder()
    input_source = BlockingInput("start")

    async def scenario() -> int:
        task = asyncio.create_task(
            run_interactive(
                "https://api.example.test/v1",
                "sk-test-only",
                "test-model",
                provider_factory=lambda url, key: provider,
                read_line=input_source,
                write_out=Recorder(),
                write_err=err,
                session_service=service,
                context_builder=builder,
            )
        )
        await asyncio.wait_for(input_source.waiting.wait(), timeout=0.5)
        task.cancel()
        return await task

    code = asyncio.run(scenario())

    assert code == 0
    assert provider.aclose_calls == 1
    assert service.list_sessions().sessions[0].turns == ()
    assert not (workspace_path / ".env").exists()


@pytest.mark.parametrize("interruption", [EOFError(), KeyboardInterrupt()])
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("write", {"path": ".env", "content": "not written"}),
        ("ask", {"question": "Continue?"}),
    ],
)
def test_interaction_eof_or_ctrl_c_closes_without_partial_turn(
    tmp_path: Path,
    interruption: BaseException,
    tool_name: str,
    arguments: dict[str, str],
) -> None:
    service, builder, workspace_path = make_dependencies(tmp_path)
    provider = ScriptedProvider(
        [[Finish(tool_calls=(ToolCall("interrupt-call", tool_name, arguments),))]]
    )

    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            provider_factory=lambda url, key: provider,
            read_line=InputScript("start", interruption),
            write_out=Recorder(),
            write_err=Recorder(),
            session_service=service,
            context_builder=builder,
        )
    )

    assert code == 0
    assert provider.aclose_calls == 1
    sessions = service.list_sessions().sessions
    assert len(sessions) == 1
    assert sessions[0].turns == ()
    assert not (workspace_path / ".env").exists()


class SimpleInputScript:
    def __init__(self, *items: str) -> None:
        self._items = list(items)

    async def __call__(self) -> str:
        if not self._items:
            raise EOFError
        return self._items.pop(0)


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
            read_line=SimpleInputScript("run command", *(["y"] if confirm else [])),
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


class TextRecorder:
    def __init__(self) -> None:
        self.text = ""

    def __call__(self, text: str) -> None:
        self.text += text


def test_structured_ask_provider_tool_json_and_continue_loop(tmp_path: Path) -> None:
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
        out = TextRecorder()
        err = TextRecorder()
        code = await run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            provider_factory=lambda _url, _key: provider,
            read_line=SimpleInputScript("start", " 2 "),
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

    out = TextRecorder()
    err = TextRecorder()
    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-only",
            "test-model",
            session_mode="continue",
            provider_factory=lambda _url, _key: provider,
            read_line=SimpleInputScript("next"),
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
    assert '"format_version":6' in raw
    assert "YAML" in raw
    assert "sk-test-only" not in raw
    assert provider.aclose_calls == 2
