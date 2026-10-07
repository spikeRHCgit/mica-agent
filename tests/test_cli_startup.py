"""CLI 启动集成：会话选择、标题顺序、预检查与短路。

这些测试保护 CLI 激活路径：new/continue/resume 选择、安全预检查诊断、
单次标题构造，以及会话选择或系统提示词失败必须先于 Provider 创建的顺序规则。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mica_agent import cli
from mica_agent.app import MicaApp
from mica_agent.cli import run_interactive
from mica_agent.context_builder import ContextBuilder
from mica_agent.events import RunCompleted
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import Message, MessageRole
from mica_agent.providers import Finish, ProviderEvent, ProviderRequest
from mica_agent.providers import TextDelta as ProviderTextDelta
from mica_agent.session_service import SessionService
from mica_agent.system_prompt import SystemPromptError
from mica_agent.workspace import WorkspaceIdentity, resolve_workspace

TEST_TIME = datetime(2026, 8, 23, tzinfo=UTC)


@pytest.mark.parametrize("failure", ["factory", "application"])
def test_cli_app_construction_failure_uses_safe_provider_diagnostic(
    tmp_path, monkeypatch, failure
):
    import mica_agent.app as app_module

    service, builder, _, _, _ = setup_session(tmp_path)
    out, err = Recorder(), Recorder()
    created = []
    url, key = "https://private.example.test/v1", "TEST_ONLY_KEY"
    raw_error = f"private exception {url} {key} token-value"

    def fail(*args, **kwargs):
        raise RuntimeError(raw_error)

    def factory(*args):
        created.append(True)
        return ProviderStub()

    if failure == "application":
        monkeypatch.setattr(app_module, "MicaApp", fail)
    code = asyncio.run(
        run_interactive(
            url,
            key,
            "model",
            provider_factory=fail if failure == "factory" else factory,
            session_service=service,
            context_builder=builder,
            read_line=InputScript(),
            write_out=out,
            write_err=err,
            plain=True,
        )
    )
    assert code == 2 and out.text == ""
    assert err.text == "mica: unable to initialize provider\n"
    assert created == []
    for private in (url, key, raw_error, "token-value"):
        assert private not in err.text


class ScriptedProvider:
    def __init__(self, scripts: list[list[ProviderEvent]]) -> None:
        self.scripts = scripts
        self.requests: list[ProviderRequest] = []
        self.aclose_calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.scripts) - 1)
        for event in self.scripts[index]:
            yield event

    async def aclose(self) -> None:
        self.aclose_calls += 1


class ProviderStub:
    """对每个请求返回一个 Finish 事件。"""

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []
        self.aclose_calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        yield Finish()

    async def aclose(self) -> None:
        self.aclose_calls += 1


class InputScript:
    """回放脚本化输入行；BaseException 元素会被抛出，而不是作为返回值。"""

    def __init__(self, *items: object) -> None:
        self.items = list(items)

    async def __call__(self) -> str:
        if not self.items:
            raise EOFError
        value = self.items.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value  # type: ignore[return-value]


class HeaderInputScript:
    def __init__(self, *items: str) -> None:
        self.items = list(items)

    async def __call__(self) -> str:
        if not self.items:
            raise EOFError
        return self.items.pop(0)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str) -> None:
        self.calls.append(text)

    @property
    def text(self) -> str:
        return "".join(self.calls)


def setup_session(
    tmp_path: Path,
) -> tuple[SessionService, ContextBuilder, str, Path, Path]:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    data_root = tmp_path / "mica-data"
    identity = resolve_workspace(workspace_path)
    service = SessionService(
        JsonlSessionStore(identity, data_root),
        now=lambda: TEST_TIME,
    )
    session = service.create_session()
    builder = ContextBuilder(data_root=data_root, workspace=identity)
    return service, builder, session.meta.session_id, data_root, workspace_path


def make_app(
    provider: ScriptedProvider,
    service: SessionService,
    builder: ContextBuilder,
    session_id: str,
    *,
    run_ids: list[str] | None = None,
    turn_ids: list[str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> MicaApp:
    run_values = iter(run_ids or ["run-1", "run-2", "run-3"])
    turn_values = iter(turn_ids or ["turn-1", "turn-2", "turn-3"])
    return MicaApp(
        provider=provider,
        model="test-model",
        session_service=service,
        session_id=session_id,
        context_builder=builder,
        run_id_factory=lambda: next(run_values),
        turn_id_factory=lambda: next(turn_values),
        clock=clock or (lambda: TEST_TIME),
    )


async def collect(app: MicaApp, prompt: str) -> list[object]:
    events: list[object] = []
    async for event in app.run(prompt):
        events.append(event)
    return events


def run_collect(app: MicaApp, prompt: str) -> list[object]:
    return asyncio.run(collect(app, prompt))


def _run_cli_with_injected_session(
    service: SessionService,
    builder: ContextBuilder,
    provider: ScriptedProvider,
    *,
    mode: str = "new",
    inputs: tuple[object, ...] = (),
) -> tuple[int, Recorder, Recorder, list[bool]]:
    out = Recorder()
    err = Recorder()
    provider_created: list[bool] = []

    def factory(_url: str, _key: str) -> ScriptedProvider:
        provider_created.append(True)
        return provider

    code = asyncio.run(
        run_interactive(
            "https://api.example.test/v1",
            "sk-test-key",
            "test-model",
            session_mode=mode,  # type: ignore[arg-type]
            provider_factory=factory,
            read_line=InputScript(*inputs),
            write_out=out,
            write_err=err,
            session_service=service,
            context_builder=builder,
            clock=lambda: TEST_TIME,
        )
    )
    return code, out, err, provider_created


def make_dependencies(
    tmp_path: Path,
) -> tuple[WorkspaceIdentity, SessionService, ContextBuilder]:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace = resolve_workspace(workspace_path)
    data_root = tmp_path / "data"
    service = SessionService(JsonlSessionStore(workspace, data_root))
    builder = ContextBuilder(data_root=data_root, workspace=workspace)
    data_root.mkdir(exist_ok=True)
    data_root.joinpath("AGENTS.md").write_text("global snapshot", encoding="utf-8")
    workspace_path.joinpath("AGENTS.md").write_text(
        "workspace snapshot", encoding="utf-8"
    )
    return workspace, service, builder


def test_cli_default_creates_new_session_and_enters_app(tmp_path: Path) -> None:
    service, builder, _, _, _ = setup_session(tmp_path)
    before = len(service.list_sessions().sessions)
    provider = ScriptedProvider([[ProviderTextDelta(text="ok"), Finish()]])

    code, out, err, created = _run_cli_with_injected_session(
        service, builder, provider, inputs=("hello",)
    )

    assert code == 0
    assert created == [True]
    assert out.text == "ok\n"
    assert err.text == ""
    assert len(service.list_sessions().sessions) == before + 1


def test_cli_default_blank_eof_keeps_one_empty_session_without_request(
    tmp_path: Path,
) -> None:
    service, builder, _, _, _ = setup_session(tmp_path)
    before_ids = {
        session.meta.session_id for session in service.list_sessions().sessions
    }
    provider = ScriptedProvider([])

    code, out, err, created = _run_cli_with_injected_session(
        service,
        builder,
        provider,
        inputs=("", "   ", "\t"),
    )

    after_sessions = service.list_sessions().sessions
    after_ids = {session.meta.session_id for session in after_sessions}
    new_ids = after_ids - before_ids
    assert code == 0
    assert created == [True]
    assert provider.requests == []
    assert provider.aclose_calls == 1
    assert out.text == ""
    assert err.text == ""
    assert len(new_ids) == 1
    new_session = next(
        session for session in after_sessions if session.meta.session_id in new_ids
    )
    assert new_session.turns == ()


def test_cli_context_preflight_failure_does_not_construct_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, builder, _, _, _ = setup_session(tmp_path)
    provider = ScriptedProvider([])

    def fail_build() -> object:
        raise RuntimeError("context failure with private path")

    monkeypatch.setattr(builder, "build", fail_build)
    code, out, err, created = _run_cli_with_injected_session(
        service, builder, provider, mode="continue", inputs=("question",)
    )

    assert code == 2
    assert created == []
    assert provider.aclose_calls == 0
    assert out.text == ""
    assert err.text == "mica: unable to prepare session context\n"


def test_cli_continue_reuses_last_active_session_and_history(tmp_path: Path) -> None:
    service, builder, session_id, _, _ = setup_session(tmp_path)
    first_provider = ScriptedProvider(
        [[ProviderTextDelta(text="old answer"), Finish()]]
    )
    first_app = make_app(first_provider, service, builder, session_id)
    assert isinstance(run_collect(first_app, "old question")[-1], RunCompleted)
    provider = ScriptedProvider([[ProviderTextDelta(text="new answer"), Finish()]])

    code, _, err, created = _run_cli_with_injected_session(
        service,
        builder,
        provider,
        mode="continue",
        inputs=("new question",),
    )

    assert code == 0
    assert created == [True]
    assert err.text == ""
    assert provider.requests[0].messages[-3:] == (
        Message(MessageRole.USER, "old question"),
        Message(MessageRole.ASSISTANT, "old answer"),
        Message(MessageRole.USER, "new question"),
    )
    assert len(service.list_sessions().sessions) == 1


def test_cli_resume_lists_numbered_sessions_and_accepts_valid_choice(
    tmp_path: Path,
) -> None:
    service, builder, first_id, _, _ = setup_session(tmp_path)
    service.rename_session(first_id, "first session")
    second = service.create_session(title="second session")
    listed = service.list_sessions().sessions
    selected_index = next(
        index
        for index, session in enumerate(listed, start=1)
        if session.meta.session_id == second.meta.session_id
    )
    provider = ScriptedProvider([[ProviderTextDelta(text="resumed"), Finish()]])

    code, out, err, created = _run_cli_with_injected_session(
        service,
        builder,
        provider,
        mode="resume",
        inputs=(str(selected_index), "resume question"),
    )

    assert code == 0
    assert created == [True]
    listed_titles = {line.split(". ", 1)[1] for line in out.text.splitlines()[:2]}
    assert {"first session", "second session"} == listed_titles
    assert "resumed\n" in out.text
    assert err.text == ""
    assert provider.requests[0].messages[-1] == Message(
        MessageRole.USER, "resume question"
    )


def test_cli_continue_without_healthy_session_never_creates_provider(
    tmp_path: Path,
) -> None:
    service, builder, session_id, _, _ = setup_session(tmp_path)
    service.delete_session(session_id)
    provider = ScriptedProvider([[Finish()]])

    code, out, err, created = _run_cli_with_injected_session(
        service, builder, provider, mode="continue", inputs=("should not read",)
    )

    assert code != 0
    assert created == []
    assert out.text == ""
    assert "no healthy session" in err.text
    assert "session.jsonl" not in err.text


def test_cli_resume_eof_or_invalid_choice_never_creates_provider(
    tmp_path: Path,
) -> None:
    service, builder, _, _, _ = setup_session(tmp_path)
    before_count = len(service.list_sessions().sessions)
    provider = ScriptedProvider([[Finish()]])

    eof_result = _run_cli_with_injected_session(
        service, builder, provider, mode="resume", inputs=()
    )
    invalid_result = _run_cli_with_injected_session(
        service, builder, provider, mode="resume", inputs=("999",)
    )
    unicode_result = _run_cli_with_injected_session(
        service, builder, provider, mode="resume", inputs=("²",)
    )

    for code, out, err, created in (eof_result, invalid_result, unicode_result):
        assert code != 0
        assert out.text.startswith("1. ")
        assert created == []
        assert "session selection" in err.text
        assert "session.jsonl" not in err.text

    code, out, err, created = unicode_result
    assert code == 2
    assert out.text == "1. untitled session\n"
    assert err.text == "mica: invalid session selection; enter a listed number\n"
    assert created == []
    assert "Traceback" not in err.text
    assert "sk-test-key" not in err.text
    assert str(tmp_path) not in err.text
    assert len(service.list_sessions().sessions) == before_count


def test_cli_preflight_displays_safe_agents_diagnostics(
    tmp_path: Path,
) -> None:
    service, builder, session_id, data_root, workspace_path = setup_session(tmp_path)
    data_root.joinpath("AGENTS.md").write_bytes(b"\xff")
    workspace_path.joinpath("AGENTS.md").write_text(
        "workspace warning rules", encoding="utf-8"
    )
    provider = ScriptedProvider([[ProviderTextDelta(text="ok"), Finish()]])

    code, _, err, created = _run_cli_with_injected_session(
        service, builder, provider, mode="continue", inputs=("question",)
    )

    assert code == 0
    assert created == [True]
    assert err.text == "warning: global AGENTS.md is not valid UTF-8\n"
    assert provider.requests[0].messages[0].role is MessageRole.SYSTEM
    assert provider.requests[0].messages[1:] == (
        Message(
            MessageRole.SYSTEM,
            '<INSTRUCTIONS source="workspace">\n'
            "workspace warning rules\n"
            "</INSTRUCTIONS>",
        ),
        Message(MessageRole.USER, "question"),
    )
    assert (
        b"workspace warning rules"
        not in service.store.path_for_session(session_id).read_bytes()
    )
    assert (
        b"workspace warning rules"
        not in service.store.path_for_session(session_id).read_bytes()
    )


@pytest.mark.parametrize("mode", ["new", "continue", "resume"])
def test_cli_activation_builds_each_header_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    workspace, service, builder = make_dependencies(tmp_path)
    if mode != "new":
        service.create_session(title="selected")
    provider = ProviderStub()
    original_build = builder.build
    build_calls = 0
    prompt_workspaces: list[WorkspaceIdentity] = []

    def counted_build(*args: object, **kwargs: object):
        nonlocal build_calls
        build_calls += 1
        return original_build(*args, **kwargs)

    def fake_prompt(selected_workspace: WorkspaceIdentity) -> str:
        prompt_workspaces.append(selected_workspace)
        return "built-in session snapshot"

    monkeypatch.setattr(builder, "build", counted_build)
    monkeypatch.setattr(cli, "build_system_prompt", fake_prompt, raising=False)
    inputs = ("1", "question") if mode == "resume" else ("question",)

    code = asyncio.run(
        cli.run_interactive(
            "https://api.example.test/v1",
            "fake-key",
            "test-model",
            session_mode=mode,  # type: ignore[arg-type]
            provider_factory=lambda _url, _key: provider,
            read_line=HeaderInputScript(*inputs),
            write_out=Recorder(),
            write_err=Recorder(),
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
    )

    assert code == 0
    assert build_calls == 1
    assert prompt_workspaces == [workspace]
    assert provider.aclose_calls == 1
    assert provider.requests[0].messages == (
        Message(MessageRole.SYSTEM, "built-in session snapshot"),
        Message(
            MessageRole.SYSTEM,
            '<INSTRUCTIONS source="global">\nglobal snapshot\n</INSTRUCTIONS>',
        ),
        Message(
            MessageRole.SYSTEM,
            '<INSTRUCTIONS source="workspace">\nworkspace snapshot\n</INSTRUCTIONS>',
        ),
        Message(MessageRole.USER, "question"),
    )


@pytest.mark.parametrize(
    ("mode", "inputs", "create_session"),
    [
        ("continue", (), False),
        ("resume", ("999",), True),
    ],
)
def test_failed_session_selection_precedes_header_and_provider_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    inputs: tuple[str, ...],
    create_session: bool,
) -> None:
    workspace, service, builder = make_dependencies(tmp_path)
    if create_session:
        service.create_session(title="available")
    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> object:
        calls.append("startup")
        raise AssertionError("startup must not run")

    monkeypatch.setattr(builder, "build", forbidden)
    monkeypatch.setattr(cli, "build_system_prompt", forbidden)
    code = asyncio.run(
        cli.run_interactive(
            "https://api.example.test/v1",
            "fake-key",
            "test-model",
            session_mode=mode,  # type: ignore[arg-type]
            provider_factory=forbidden,  # type: ignore[arg-type]
            read_line=HeaderInputScript(*inputs),
            write_out=Recorder(),
            write_err=Recorder(),
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
    )

    assert code == 2
    assert calls == []


def test_system_prompt_failure_is_safe_and_precedes_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, service, builder = make_dependencies(tmp_path)
    provider_created = False
    err = Recorder()

    def fail_prompt(_workspace: WorkspaceIdentity) -> str:
        raise SystemPromptError("private template C:/secret API_KEY=value")

    def forbidden_provider(_url: str, _key: str) -> ProviderStub:
        nonlocal provider_created
        provider_created = True
        raise AssertionError("provider must not be created")

    monkeypatch.setattr(cli, "build_system_prompt", fail_prompt, raising=False)
    code = asyncio.run(
        cli.run_interactive(
            "https://api.example.test/v1",
            "fake-key",
            "test-model",
            provider_factory=forbidden_provider,
            read_line=HeaderInputScript(),
            write_out=Recorder(),
            write_err=err,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
    )

    assert code == 2
    assert provider_created is False
    assert err.text == "mica: unable to prepare system prompt\n"
    assert "secret" not in err.text
    assert str(workspace.workspace_path) not in err.text


def test_saved_default_initializes_session_and_cli_override_preserves_restore_choice(
    tmp_path, monkeypatch
):
    import json

    from test_config_service import make_service

    from mica_agent.providers import AuthenticationError, ProviderErrorEvent
    from mica_agent.session_contracts import SessionSelection

    for key in ("MICA_API_URL", "MICA_API_KEY", "MICA_MODEL"):
        monkeypatch.delenv(key, raising=False)
    configuration = make_service(tmp_path)
    root = configuration.store.data_root
    (root / "config.json").write_text(
        '{"defaultModel":{"provider":"local","model":"a"}}'
    )
    workspace = resolve_workspace(configuration.store.workspace)
    service = SessionService(JsonlSessionStore(workspace, root))
    provider = ScriptedProvider(
        [
            [ProviderTextDelta("first"), Finish()],
            [ProviderErrorEvent(AuthenticationError("fixed safe error"))],
            [ProviderTextDelta("restored"), Finish()],
        ]
    )
    constructed = []

    def factory(url, key):
        constructed.append((url, key))
        return provider

    async def scenario():
        common = {
            "workspace": workspace,
            "data_root": root,
            "session_service": service,
            "provider_factory": factory,
            "write_out": lambda text: None,
            "write_err": lambda text: None,
        }
        assert await run_interactive(read_line=InputScript("first"), **common) == 0
        stored = service.last_active_session()
        assert stored.meta.selection == SessionSelection("local", "a")
        (root / "config.json").write_text(
            '{"defaultModel":{"provider":"local","model":"b"}}'
        )
        assert (
            await run_interactive(
                session_mode="continue",
                explicit_selection=SessionSelection("local", "b"),
                read_line=InputScript("override"),
                **common,
            )
            == 0
        )
        assert service.last_active_session().meta.selection == SessionSelection(
            "local", "a"
        )
        assert service.last_active_session().checkpoints
        assert (
            await run_interactive(
                session_mode="continue", read_line=InputScript("restore"), **common
            )
            == 0
        )

    asyncio.run(scenario())
    assert [request.model for request in provider.requests] == ["a", "b", "a"]
    assert constructed == [("https://example.test/v1", "TEST_ONLY_SECRET")] * 3
    raw = service.store.path_for_session(
        service.last_active_session().meta.session_id
    ).read_text()
    assert "TEST_ONLY_SECRET" not in raw and "example.test" not in raw
    assert provider.aclose_calls == 3
    assert json.loads(raw.splitlines()[0])["provider_id"] == "local"


def test_manual_configuration_reaches_actual_http_payload(tmp_path, monkeypatch):
    import json

    import httpx
    from test_config_service import make_service

    from mica_agent.providers import OpenAICompatibleProvider

    for key in ("MICA_API_URL", "MICA_API_KEY", "MICA_MODEL"):
        monkeypatch.delenv(key, raising=False)
    configuration = make_service(tmp_path)
    root = configuration.store.data_root
    (root / "config.json").write_text(
        '{"defaultModel":{"provider":"local","model":"a"},"reasoningEffort":"high"}'
    )
    directory = json.loads((root / "models.json").read_text())
    directory["providers"]["local"]["models"][0].update(
        maxTokens=123, thinkingLevelMap={"high": "vendor-high"}
    )
    (root / "models.json").write_text(json.dumps(directory))
    captured = []

    def handler(request):
        captured.append(
            (
                str(request.url),
                request.headers["authorization"],
                json.loads(request.content),
            )
        )
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        )

    provider = OpenAICompatibleProvider(
        api_key="TEST_ONLY_SECRET",
        base_url="https://example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    out, err = Recorder(), Recorder()
    result = asyncio.run(
        run_interactive(
            workspace=resolve_workspace(configuration.store.workspace),
            data_root=root,
            provider_factory=lambda url, key: provider,
            read_line=InputScript("question"),
            write_out=out,
            write_err=err,
        )
    )
    assert result == 0 and out.text == "hello\n"
    address, authorization, payload = captured[0]
    assert address == "https://example.test/v1/chat/completions"
    assert authorization == "Bearer TEST_ONLY_SECRET"
    assert payload["max_tokens"] == 123 and payload["reasoning_effort"] == "vendor-high"
    assert "TEST_ONLY_SECRET" not in err.text


def test_plain_preflight_workspace_failure_reports_only_safe_diagnostic(
    monkeypatch, capsys
):
    from mica_agent.workspace import WorkspaceResolutionError

    def fail(*args, **kwargs):
        raise WorkspaceResolutionError("TEST_ONLY_KEY TEST_ONLY_URL")

    monkeypatch.setattr(cli, "resolve_workspace", fail)
    assert cli._main_cli(plain=True) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "mica: unable to resolve the current workspace\n"


@pytest.mark.parametrize("source", ["environment", "direct"])
@pytest.mark.parametrize("unsafe", ["file", "parent"])
def test_unprotected_auth_does_not_block_independent_startup_source(
    tmp_path, monkeypatch, source, unsafe
):
    from test_config_service import make_service
    from test_credential_permissions import protect

    configuration = make_service(tmp_path)
    root = configuration.store.data_root
    auth = root / "auth.json"
    protect(root)
    if unsafe == "file":
        protect(auth, shared=True)
    else:
        protect(root, shared=True, rights="CreateFiles")
    values = (
        "https://independent.example.test/v1",
        "INDEPENDENT_TEST_KEY",
        "independent",
    )
    for name, value in zip(
        ("MICA_API_URL", "MICA_API_KEY", "MICA_MODEL"), values, strict=True
    ):
        if source == "environment":
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    provider = ScriptedProvider([[ProviderTextDelta("independent reply"), Finish()]])
    connections = []

    def factory(url, key):
        connections.append((url, key))
        return provider

    out, err = Recorder(), Recorder()
    before = auth.read_bytes()
    try:
        code = asyncio.run(
            run_interactive(
                *(values if source == "direct" else ()),
                workspace=resolve_workspace(configuration.store.workspace),
                data_root=root,
                provider_factory=factory,
                read_line=InputScript("hello"),
                write_out=out,
                write_err=err,
            )
        )
        assert code == 0
        assert out.text == "independent reply\n"
        assert connections == [values[:2]]
        assert "credential" in err.text and "protect" in err.text
        assert "TEST_ONLY_SECRET" not in out.text + err.text
        assert "INDEPENDENT_TEST_KEY" not in out.text + err.text
        assert str(root) not in err.text
        assert auth.read_bytes() == before
    finally:
        protect(root)


@pytest.mark.parametrize("damage", ["syntax", "root", "duplicate", "unreadable"])
def test_auth_file_errors_remain_fatal_with_independent_connection(
    tmp_path, monkeypatch, damage
):
    from test_config_service import make_service

    configuration = make_service(tmp_path)
    root = configuration.store.data_root
    auth = root / "auth.json"
    if damage == "unreadable":

        def fail_read(path):
            raise PermissionError("TEST_ONLY_SECRET")

        monkeypatch.setattr(
            "mica_agent._credential_permissions.read_credentials", fail_read
        )
    else:
        auth.write_bytes(
            {
                "syntax": b"{ TEST_ONLY_SECRET",
                "root": b"[]",
                "duplicate": b'{"providers":{},"providers":{}}',
            }[damage]
        )
    provider = ProviderStub()
    err = Recorder()
    result = asyncio.run(
        run_interactive(
            "https://independent.example.test/v1",
            "INDEPENDENT_TEST_KEY",
            "chat",
            workspace=resolve_workspace(configuration.store.workspace),
            data_root=root,
            provider_factory=lambda url, key: provider,
            read_line=InputScript("hello"),
            write_out=Recorder(),
            write_err=err,
        )
    )
    assert result == 2 and not provider.requests
    assert "mica:" in err.text
    assert "source disabled" not in err.text
    assert "TEST_ONLY_SECRET" not in err.text and "INDEPENDENT_TEST_KEY" not in err.text
    assert not (root / "sessions").exists()


def test_unprotected_saved_credentials_keep_reference_without_environment_fallback(
    tmp_path, monkeypatch
):
    from test_config_service import make_service
    from test_credential_permissions import protect

    from mica_agent.session_contracts import SessionSelection

    configuration = make_service(tmp_path)
    root = configuration.store.data_root
    workspace = resolve_workspace(configuration.store.workspace)
    service = SessionService(JsonlSessionStore(workspace, root))
    selected = service.create_session(selection=SessionSelection("local", "a"))
    path = service.store.path_for_session(selected.meta.session_id)
    before = path.read_bytes()
    protect(root / "auth.json", shared=True)
    for name, value in zip(
        ("MICA_API_URL", "MICA_API_KEY", "MICA_MODEL"),
        ("https://independent.example.test/v1", "INDEPENDENT_TEST_KEY", "chat"),
        strict=True,
    ):
        monkeypatch.setenv(name, value)
    provider = ProviderStub()
    err = Recorder()
    result = asyncio.run(
        run_interactive(
            workspace=workspace,
            session_service=service,
            session_mode="continue",
            provider_factory=lambda url, key: provider,
            read_line=InputScript("hello"),
            write_out=Recorder(),
            write_err=err,
        )
    )
    assert result == 2 and not provider.requests
    assert "Selected connection is unavailable" in err.text
    assert "authentication source disabled" in err.text
    assert (
        service.load_session(selected.meta.session_id).meta.selection
        == selected.meta.selection
    )
    assert path.read_bytes() == before
