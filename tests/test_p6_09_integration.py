"""P6-09 CLI route, TUI assembly, and lifecycle integration tests."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mica_agent import cli
from mica_agent.context_builder import ContextBuilder
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import MessageRole
from mica_agent.permissions import PermissionMode
from mica_agent.providers import Finish, ProviderEvent, ProviderRequest
from mica_agent.providers import TextDelta as ProviderTextDelta
from mica_agent.session_service import SessionService
from mica_agent.tui.runtime import MicaTuiRuntime
from mica_agent.workspace import resolve_workspace


class ScriptedProvider:
    def __init__(self, *scripts: list[ProviderEvent]) -> None:
        self.scripts = list(scripts)
        self.requests: list[ProviderRequest] = []
        self.aclose_calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        for event in self.scripts[len(self.requests) - 1]:
            yield event

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


def make_dependencies(tmp_path: Path):
    workspace_path = tmp_path / "工作区 with spaces"
    workspace_path.mkdir(parents=True)
    data_root = tmp_path / "isolated home" / ".mica"
    data_root.mkdir(parents=True)
    workspace = resolve_workspace(workspace_path)
    service = SessionService(JsonlSessionStore(workspace, data_root))
    builder = ContextBuilder(data_root=data_root, workspace=workspace)
    return workspace, service, builder


@pytest.mark.parametrize(
    ("argv", "expected_plain", "expected_mode", "expected_permission"),
    [
        (["mica", "cli"], False, "new", PermissionMode.DEFAULT),
        (["mica", "cli", "--plain"], True, "new", PermissionMode.DEFAULT),
        (
            ["mica", "cli", "--continue", "--plain"],
            True,
            "continue",
            PermissionMode.DEFAULT,
        ),
        (["mica", "cli", "--resume"], False, "resume", PermissionMode.DEFAULT),
        (
            ["mica", "cli", "--permission-mode", "solo"],
            False,
            "new",
            PermissionMode.SOLO,
        ),
        (
            ["mica", "cli", "--plain", "--permission-mode", "solo"],
            True,
            "new",
            PermissionMode.SOLO,
        ),
    ],
)
def test_cli_main_routes_default_tui_and_plain_without_startup_work(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected_plain: bool,
    expected_mode: str,
    expected_permission: PermissionMode,
) -> None:
    monkeypatch.setenv("MICA_API_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("MICA_API_KEY", "fake-key")
    monkeypatch.setenv("MICA_MODEL", "fake-model")
    calls: list[tuple[object, object, object]] = []

    def fake_main_cli(mode, permission, *, plain):
        calls.append((mode, permission, plain))
        return 0

    monkeypatch.setattr(cli, "_main_cli", fake_main_cli)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 0
    assert calls == [(expected_mode, expected_permission, expected_plain)]


def test_cli_help_explains_default_tui_and_plain(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["mica", "cli", "--help"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "Textual TUI" in captured.out
    assert "--plain" in captured.out
    assert "basic text interface" in captured.out


def test_tui_route_freezes_context_snapshot_and_uses_selected_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        workspace, service, builder = make_dependencies(tmp_path)
        (workspace.workspace_path / "AGENTS.md").write_text(
            "workspace-only rules", encoding="utf-8"
        )
        provider = ScriptedProvider([ProviderTextDelta("done"), Finish()])
        out = Recorder()
        err = Recorder()
        build_calls = 0
        prompt_calls = 0
        original_build = builder.build
        original_prompt = cli.build_system_prompt

        def counted_build():
            nonlocal build_calls
            build_calls += 1
            return original_build()

        def counted_prompt(selected_workspace):
            nonlocal prompt_calls
            prompt_calls += 1
            return original_prompt(selected_workspace)

        monkeypatch.setattr(builder, "build", counted_build)
        monkeypatch.setattr(cli, "build_system_prompt", counted_prompt)
        observed: dict[str, object] = {}

        async def runner(runtime: MicaTuiRuntime) -> None:
            observed["session"] = runtime.tui.stored_session
            observed["snapshot"] = runtime.tui.startup_resources
            async with runtime.tui.run_test(size=(70, 18)) as pilot:
                await pilot.pause()
                assert runtime.submit("hello from tui") is True
                await runtime.wait_idle()
                await pilot.pause()
                assert (
                    runtime.tui.query_one("#work-status")
                    .render()
                    .plain.startswith("Mica · done")
                )

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            plain=False,
            tui_runner=runner,
            provider_factory=lambda _url, _key: provider,
            read_line=InputScript(),
            write_out=out,
            write_err=err,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
            permission_mode=PermissionMode.SOLO,
        )

        selected = observed["session"]
        snapshot = observed["snapshot"]
        assert code == 0
        assert selected is not None
        assert provider.requests[0].messages[-1].content == "hello from tui"
        assert snapshot.system_prompt_name == "Mica built-in system prompt"
        assert tuple(source.value for source in snapshot.agent_sources) == (
            "workspace",
        )
        assert snapshot.tool_names == ("ask", "bash", "edit", "read", "write")
        assert snapshot.skill_names == ()
        assert snapshot.extension_names == ()
        assert build_calls == 1
        assert prompt_calls == 1
        assert (
            service.list_sessions().sessions[0].meta.session_id
            == selected.meta.session_id
        )
        assert provider.aclose_calls == 1
        assert out.text == ""
        assert err.text == ""

    asyncio.run(scenario())


@pytest.mark.parametrize("session_mode", ["continue", "resume"])
def test_tui_route_passes_continue_or_resume_session_to_runtime(
    tmp_path: Path, session_mode: str
) -> None:
    async def scenario() -> None:
        workspace, service, builder = make_dependencies(tmp_path / session_mode)
        selected = service.create_session(title="selected")
        provider = ScriptedProvider([ProviderTextDelta("restored"), Finish()])
        observed: list[object] = []

        async def runner(runtime: MicaTuiRuntime) -> None:
            observed.append(runtime.tui.stored_session)
            async with runtime.tui.run_test(size=(70, 18)) as pilot:
                await pilot.pause()
                assert runtime.submit("restored prompt") is True
                await runtime.wait_idle()
                for _ in range(100):
                    if runtime.tui.query("#transcript"):
                        break
                    await pilot.pause()

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode=session_mode,  # type: ignore[arg-type]
            plain=False,
            tui_runner=runner,
            provider_factory=lambda _url, _key: provider,
            read_line=InputScript("1"),
            write_out=Recorder(),
            write_err=Recorder(),
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )

        assert code == 0
        assert observed == [selected]
        assert provider.requests[0].messages[-1].content == "restored prompt"
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_tui_runner_failure_is_safe_and_closes_provider_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider()
        err = Recorder()

        async def failing_runner(_runtime: MicaTuiRuntime) -> None:
            raise RuntimeError("fake-key https://private.example/path")

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            plain=False,
            tui_runner=failing_runner,
            provider_factory=lambda _url, _key: provider,
            read_line=InputScript(),
            write_out=Recorder(),
            write_err=err,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )

        assert code == 2
        assert err.text == "mica: unable to start terminal interface\n"
        assert "fake-key" not in err.text
        assert "private" not in err.text
        assert provider.aclose_calls == 1
        assert service.list_sessions().sessions[0].turns == ()

    asyncio.run(scenario())


def test_cancelled_tui_runner_closes_runtime_and_provider_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider()

        async def cancelled_runner(_runtime: MicaTuiRuntime) -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await cli.run_interactive(
                "http://127.0.0.1:1/v1",
                "fake-key",
                "fake-model",
                plain=False,
                tui_runner=cancelled_runner,
                provider_factory=lambda _url, _key: provider,
                read_line=InputScript(),
                write_out=Recorder(),
                write_err=Recorder(),
                workspace=workspace,
                session_service=service,
                context_builder=builder,
            )
        assert provider.aclose_calls == 1
        assert service.list_sessions().sessions[0].turns == ()

    asyncio.run(scenario())


def test_direct_run_interactive_remains_plain_compatible(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider([ProviderTextDelta("plain answer"), Finish()])
        out = Recorder()
        err = Recorder()
        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            provider_factory=lambda _url, _key: provider,
            read_line=InputScript("plain prompt"),
            write_out=out,
            write_err=err,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
        assert code == 0
        assert out.text == "plain answer\n"
        assert err.text == ""
        assert provider.aclose_calls == 1
        assert provider.requests[0].messages[-1].role is MessageRole.USER

    asyncio.run(scenario())
