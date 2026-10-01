"""测试默认 ``mica cli`` 路由、主缓冲区装配与资源清理。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import aclosing
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
        (["mica", "cli", "--continue"], False, "continue", PermissionMode.DEFAULT),
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
        (
            ["mica", "cli", "--continue", "--plain"],
            True,
            "continue",
            PermissionMode.DEFAULT,
        ),
    ],
)
def test_cli_routes_default_main_buffer_and_plain(
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


def test_preview_flag_is_rejected_as_unknown_argument(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(sys, "argv", ["mica", "cli", "--main-buffer-preview"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "unrecognized arguments" in captured.err
    assert "--main-buffer-preview" in captured.err


def test_cli_help_documents_default_terminal_and_plain(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(sys, "argv", ["mica", "cli", "--help"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "--plain" in captured.out
    assert "basic text interface" in captured.out
    assert "Textual" not in captured.out
    assert "--main-buffer-preview" not in captured.out
    assert "preview" not in captured.out.lower()


def test_plain_route_never_builds_main_buffer_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider([ProviderTextDelta("plain answer"), Finish()])
        called: list[str] = []
        out = Recorder()
        err = Recorder()

        async def fake_screen(*args, **kwargs):
            called.append("main-buffer")
            return 0

        monkeypatch.setattr(cli, "_run_main_buffer", fake_screen)
        lines = iter(["hello"])

        async def read_line() -> str:
            try:
                return next(lines)
            except StopIteration:
                raise EOFError from None

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode="new",
            permission_mode=PermissionMode.DEFAULT,
            plain=True,
            provider_factory=lambda url, key: provider,
            read_line=read_line,
            write_out=out,
            write_err=err,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
        assert code == 0
        assert called == []
        assert out.text == "plain answer\n"
        assert err.text == ""
        assert provider.requests[0].messages[-1].role is MessageRole.USER
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_default_route_uses_main_buffer_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from mica_agent.tui import main_buffer

        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider([ProviderTextDelta("x"), Finish()])
        recorded: dict[str, object] = {}

        class FakeScreen:
            def __init__(self, app, **kwargs) -> None:
                recorded["app"] = app
                recorded["kwargs"] = kwargs
                self.closed = False

            async def run(self) -> None:
                recorded["ran"] = True
                await recorded["app"].aclose()  # type: ignore[union-attr]
                self.closed = True

        monkeypatch.setattr(main_buffer, "MainBufferScreen", FakeScreen)

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode="new",
            permission_mode=PermissionMode.DEFAULT,
            plain=False,
            provider_factory=lambda url, key: provider,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
        assert code == 0
        assert recorded["ran"] is True
        kwargs = recorded["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["session_snapshot"].model == "fake-model"
        assert kwargs["workspace_path"] == workspace.workspace_path
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_startup_failure_uses_fixed_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from mica_agent.tui import main_buffer

        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider([ProviderTextDelta("x"), Finish()])
        written: list[str] = []

        class BrokenScreen:
            def __init__(self, app, **kwargs) -> None:
                raise RuntimeError("boom: secret detail")

            async def run(self) -> None:  # pragma: no cover - construction failed
                return None

        monkeypatch.setattr(main_buffer, "MainBufferScreen", BrokenScreen)

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode="new",
            permission_mode=PermissionMode.DEFAULT,
            plain=False,
            provider_factory=lambda url, key: provider,
            write_err=written.append,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
        assert code == 2
        assert written == ["mica: unable to start terminal interface\n"]
        assert "secret" not in "".join(written)
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_run_failure_closes_app_once_with_fixed_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from mica_agent.tui import main_buffer

        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider([ProviderTextDelta("x"), Finish()])
        written: list[str] = []

        class ExplodingScreen:
            def __init__(self, app, **kwargs) -> None:
                self.closed = False
                self._app = app

            async def run(self) -> None:
                raise RuntimeError(
                    "fake-key https://private.example/path: secret detail"
                )

        monkeypatch.setattr(main_buffer, "MainBufferScreen", ExplodingScreen)

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode="new",
            permission_mode=PermissionMode.DEFAULT,
            plain=False,
            provider_factory=lambda url, key: provider,
            write_err=written.append,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
        assert code == 2
        assert written == ["mica: unable to start terminal interface" + chr(10)]
        assert "secret" not in "".join(written)
        assert "fake-key" not in "".join(written)
        assert "private.example" not in "".join(written)
        assert provider.aclose_calls == 1
        assert service.list_sessions().sessions[0].turns == ()

    asyncio.run(scenario())


def test_normal_exit_never_double_closes_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from mica_agent.tui import main_buffer

        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider([ProviderTextDelta("x"), Finish()])

        class ClosingScreen:
            def __init__(self, app, **kwargs) -> None:
                self._app = app
                self.closed = False

            async def run(self) -> None:
                await self._app.aclose()
                self.closed = True

        monkeypatch.setattr(main_buffer, "MainBufferScreen", ClosingScreen)

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode="new",
            permission_mode=PermissionMode.DEFAULT,
            plain=False,
            provider_factory=lambda url, key: provider,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
        assert code == 0
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_cancelled_screen_propagates_and_closes_provider_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from mica_agent.tui import main_buffer

        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider()

        class CancelledScreen:
            def __init__(self, app, **kwargs) -> None:
                self._app = app
                self.closed = False

            async def run(self) -> None:
                raise asyncio.CancelledError

        monkeypatch.setattr(main_buffer, "MainBufferScreen", CancelledScreen)

        with pytest.raises(asyncio.CancelledError):
            await cli.run_interactive(
                "http://127.0.0.1:1/v1",
                "fake-key",
                "fake-model",
                session_mode="new",
                permission_mode=PermissionMode.DEFAULT,
                plain=False,
                provider_factory=lambda url, key: provider,
                workspace=workspace,
                session_service=service,
                context_builder=builder,
            )
        assert provider.aclose_calls == 1
        assert service.list_sessions().sessions[0].turns == ()

    asyncio.run(scenario())


def test_already_closed_screen_failure_does_not_double_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from mica_agent.tui import main_buffer

        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider([ProviderTextDelta("x"), Finish()])
        written: list[str] = []

        class ClosedThenBoom:
            def __init__(self, app, **kwargs) -> None:
                self._app = app
                self.closed = False

            async def run(self) -> None:
                await self._app.aclose()
                self.closed = True
                raise RuntimeError("after close: secret")

        monkeypatch.setattr(main_buffer, "MainBufferScreen", ClosedThenBoom)

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode="new",
            permission_mode=PermissionMode.DEFAULT,
            plain=False,
            provider_factory=lambda url, key: provider,
            write_err=written.append,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
        assert code == 2
        assert written == ["mica: unable to start terminal interface\n"]
        assert provider.aclose_calls == 1
        assert service.list_sessions().sessions[0].turns == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("session_mode", ["continue", "resume"])
def test_default_route_passes_continue_or_resume_session_to_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_mode: str
) -> None:
    async def scenario() -> None:
        from mica_agent.tui import main_buffer

        workspace, service, builder = make_dependencies(tmp_path / session_mode)
        selected = service.create_session(title="selected")
        provider = ScriptedProvider([ProviderTextDelta("restored"), Finish()])
        observed: list[object] = []
        observed_labels: list[str | None] = []

        class DrivingScreen:
            def __init__(self, app, **kwargs) -> None:
                self._app = app
                observed.append(kwargs["stored_session"])
                observed_labels.append(kwargs["session_snapshot"].session_label)
                self.closed = False

            async def run(self) -> None:
                async with aclosing(self._app.run("restored prompt")) as stream:
                    async for _event in stream:
                        pass
                await self._app.aclose()
                self.closed = True

        monkeypatch.setattr(main_buffer, "MainBufferScreen", DrivingScreen)

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode=session_mode,  # type: ignore[arg-type]
            plain=False,
            provider_factory=lambda _url, _key: provider,
            read_line=InputScript("1"),
            write_out=Recorder(),
            write_err=Recorder(),
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )

        stored = service.load_session(selected.meta.session_id)
        assert code == 0
        assert observed == [selected]
        assert observed_labels == ["selected"]
        assert provider.requests[0].messages[-1].content == "restored prompt"
        assert stored.turns
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_default_route_drives_the_assembled_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from mica_agent.tui import main_buffer

        workspace, service, builder = make_dependencies(tmp_path)
        (workspace.workspace_path / "AGENTS.md").write_text(
            "workspace-only rules", encoding="utf-8"
        )
        provider = ScriptedProvider([ProviderTextDelta("assembled"), Finish()])
        out = Recorder()
        err = Recorder()
        build_calls = 0
        prompt_calls = 0
        original_build = builder.build
        original_prompt = cli.build_system_prompt
        recorded: dict[str, object] = {}

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

        class DrivingScreen:
            def __init__(self, app, **kwargs) -> None:
                self._app = app
                recorded["kwargs"] = kwargs
                self.closed = False

            async def run(self) -> None:
                async with aclosing(self._app.run("hello from tui")) as stream:
                    async for _event in stream:
                        pass
                await self._app.aclose()
                self.closed = True

        monkeypatch.setattr(main_buffer, "MainBufferScreen", DrivingScreen)

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode="new",
            permission_mode=PermissionMode.SOLO,
            plain=False,
            provider_factory=lambda _url, _key: provider,
            read_line=InputScript(),
            write_out=out,
            write_err=err,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )

        kwargs = recorded["kwargs"]
        assert isinstance(kwargs, dict)
        selected = kwargs["stored_session"]
        snapshot = kwargs["startup_resources"]
        session_snapshot = kwargs["session_snapshot"]
        stored = service.list_sessions().sessions[0]
        assert code == 0
        assert selected is not None
        assert snapshot.system_prompt_name == "Mica built-in system prompt"
        assert tuple(source.value for source in snapshot.agent_sources) == (
            "workspace",
        )
        assert snapshot.global_agent_path is None
        assert snapshot.workspace_agent_path == "./AGENTS.md"
        assert snapshot.tool_names == ("ask", "bash", "edit", "read", "write")
        assert snapshot.skill_names == ()
        assert snapshot.extension_names == ()
        assert session_snapshot.workspace.endswith("工作区 with spaces")
        assert session_snapshot.session_label == selected.meta.session_id
        assert session_snapshot.model == "fake-model"
        assert build_calls == 1
        assert prompt_calls == 1
        assert provider.requests[0].messages[-1].content == "hello from tui"
        assert stored.meta.session_id == selected.meta.session_id
        assert stored.turns
        assert out.text == ""
        assert err.text == ""
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_distribution_declares_prompt_toolkit_and_rich() -> None:
    from importlib.metadata import requires

    declared = requires("mica-agent") or []
    assert any(entry.startswith("prompt-toolkit") for entry in declared)
    assert any(entry.startswith("rich") for entry in declared)
    assert not any(entry.startswith("textual") for entry in declared)
