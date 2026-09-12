"""P6-11A `--main-buffer-preview` 参数、路由与依赖分发测试。"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mica_agent import cli
from mica_agent.context_builder import ContextBuilder
from mica_agent.jsonl_store import JsonlSessionStore
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
    ("argv", "expected_plain", "expected_preview"),
    [
        (["mica", "cli"], False, False),
        (["mica", "cli", "--main-buffer-preview"], False, True),
        (["mica", "cli", "--plain"], True, False),
        (["mica", "cli", "--plain", "--main-buffer-preview"], True, False),
        (["mica", "cli", "--continue", "--main-buffer-preview"], False, True),
    ],
)
def test_cli_routes_preview_flag_without_changing_default_or_plain(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected_plain: bool,
    expected_preview: bool,
) -> None:
    monkeypatch.setenv("MICA_API_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("MICA_API_KEY", "fake-key")
    monkeypatch.setenv("MICA_MODEL", "fake-model")
    calls: list[tuple[bool, bool]] = []

    def fake_main_cli(mode, permission, *, plain, main_buffer_preview=False):
        calls.append((plain, main_buffer_preview))
        return 0

    monkeypatch.setattr(cli, "_main_cli", fake_main_cli)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 0
    assert calls == [(expected_plain, expected_preview)]


def test_cli_help_documents_preview_and_default_tui(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(sys, "argv", ["mica", "cli", "--help"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "Textual TUI" in captured.out
    assert "--main-buffer-preview" in captured.out
    assert "--plain" in captured.out


def test_default_route_never_builds_main_buffer_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider([ProviderTextDelta("x"), Finish()])
        called: list[str] = []

        async def fake_screen(*args, **kwargs):
            called.append("main-buffer")
            return 0

        monkeypatch.setattr(cli, "_run_main_buffer", fake_screen)

        async def runner(runtime) -> None:
            called.append("textual")
            await runtime.aclose()

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode="new",
            permission_mode=PermissionMode.DEFAULT,
            plain=False,
            tui_runner=runner,
            provider_factory=lambda url, key: provider,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
        assert code == 0
        assert called == ["textual"]

    import asyncio

    asyncio.run(scenario())


def test_preview_route_uses_main_buffer_screen(
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

            async def run(self) -> None:
                recorded["ran"] = True
                await recorded["app"].aclose()  # type: ignore[union-attr]

        monkeypatch.setattr(main_buffer, "MainBufferScreen", FakeScreen)

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode="new",
            permission_mode=PermissionMode.DEFAULT,
            plain=False,
            main_buffer_preview=True,
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

    import asyncio

    asyncio.run(scenario())


def test_preview_startup_failure_uses_fixed_diagnostic(
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

            async def run(self) -> None:  # pragma: no cover - 构造期已失败
                return None

        monkeypatch.setattr(main_buffer, "MainBufferScreen", BrokenScreen)

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode="new",
            permission_mode=PermissionMode.DEFAULT,
            plain=False,
            main_buffer_preview=True,
            provider_factory=lambda url, key: provider,
            write_err=written.append,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
        assert code == 2
        assert written == ["mica: unable to start terminal interface\n"]
        assert provider.aclose_calls == 1

    import asyncio

    asyncio.run(scenario())


def test_preview_run_failure_closes_app_once_with_fixed_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from mica_agent.tui import main_buffer

        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider([ProviderTextDelta("x"), Finish()])
        written: list[str] = []

        class ExplodingScreen:
            """构造成功但运行失败：CLI 必须兜底收敛业务资源。"""

            def __init__(self, app, **kwargs) -> None:
                self.closed = False
                self._app = app

            async def run(self) -> None:
                raise RuntimeError("boom: secret detail")

        monkeypatch.setattr(main_buffer, "MainBufferScreen", ExplodingScreen)

        code = await cli.run_interactive(
            "http://127.0.0.1:1/v1",
            "fake-key",
            "fake-model",
            session_mode="new",
            permission_mode=PermissionMode.DEFAULT,
            plain=False,
            main_buffer_preview=True,
            provider_factory=lambda url, key: provider,
            write_err=written.append,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
        assert code == 2
        assert written == ["mica: unable to start terminal interface" + chr(10)]
        assert provider.aclose_calls == 1

    import asyncio

    asyncio.run(scenario())


def test_preview_normal_exit_never_double_closes_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        from mica_agent.tui import main_buffer

        workspace, service, builder = make_dependencies(tmp_path)
        provider = ScriptedProvider([ProviderTextDelta("x"), Finish()])

        class ClosingScreen:
            """正常退出：屏幕自己收敛，CLI 不得重复关闭。"""

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
            main_buffer_preview=True,
            provider_factory=lambda url, key: provider,
            workspace=workspace,
            session_service=service,
            context_builder=builder,
        )
        assert code == 0
        assert provider.aclose_calls == 1

    import asyncio

    asyncio.run(scenario())


def test_distribution_declares_prompt_toolkit_dependency() -> None:
    from importlib.metadata import requires

    declared = requires("mica-agent") or []
    assert any(entry.startswith("prompt-toolkit") for entry in declared)
