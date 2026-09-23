"""P4-09 会话激活头部快照集成测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mica_agent import cli
from mica_agent.context_builder import ContextBuilder
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import Message, MessageRole
from mica_agent.providers import Finish, ProviderEvent, ProviderRequest
from mica_agent.session_service import SessionService
from mica_agent.system_prompt import SystemPromptError
from mica_agent.workspace import WorkspaceIdentity, resolve_workspace


class ProviderStub:
    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []
        self.aclose_calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        yield Finish()

    async def aclose(self) -> None:
        self.aclose_calls += 1


class InputScript:
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
            read_line=InputScript(*inputs),
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
            read_line=InputScript(*inputs),
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
            read_line=InputScript(),
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
