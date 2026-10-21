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

    def fake_main_cli(mode, permission, *, plain, explicit_selection):
        assert explicit_selection is None
        calls.append((mode, permission, plain))
        return 0

    monkeypatch.setattr(cli, "_main_cli", fake_main_cli)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 0
    assert calls == [
        (
            expected_mode,
            expected_permission if "--permission-mode" in argv else None,
            expected_plain,
        )
    ]


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
            provider_factory=lambda connection: provider,
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
            provider_factory=lambda connection: provider,
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
            provider_factory=lambda connection: provider,
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
            provider_factory=lambda connection: provider,
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
            provider_factory=lambda connection: provider,
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
                provider_factory=lambda connection: provider,
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
            provider_factory=lambda connection: provider,
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
            provider_factory=lambda connection: provider,
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
            provider_factory=lambda connection: provider,
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


@pytest.mark.parametrize("outcome", ["text", "reasoning-only", "done-only"])
def test_configured_http_stream_reaches_screen_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    import io
    import json

    import httpx
    from sse_transport_helpers import RecordingTransport
    from test_config_service import make_service
    from test_tui_main_buffer_screen import CTRL_C, ENTER, FakeInput, pump, typed
    from test_tui_main_buffer_vt import VirtualTerminal

    from mica_agent.checkpoint_contracts import RunCheckpointKind
    from mica_agent.events import (
        ErrorCategory,
        ReasoningDelta,
        RunCompleted,
        RunFailed,
        TextDelta,
    )
    from mica_agent.providers import OpenAICompatibleProvider
    from mica_agent.session_contracts import SessionSelection
    from mica_agent.tui import main_buffer

    async def scenario() -> None:
        configuration = make_service(tmp_path)
        for name in ("MICA_API_URL", "MICA_API_KEY", "MICA_MODEL"):
            monkeypatch.delenv(name, raising=False)
        root = configuration.store.data_root
        (root / "config.json").write_text(
            '{"defaultModel":{"provider":"local","model":"a"},'
            '"reasoningDisplay":"hidden"}'
        )
        workspace = resolve_workspace(configuration.store.workspace)
        service = SessionService(JsonlSessionStore(workspace, root))
        fake = FakeInput()
        terminal = main_buffer.Terminal(
            output=io.StringIO(),
            input_factory=lambda: fake,
            size=(100, 35),
            environ={"NO_COLOR": "1"},
            interactive=True,
        )
        blocked = asyncio.Event()
        release_prefix = asyncio.Event()
        release_tail = asyncio.Event()
        progress_seen = asyncio.Event()
        terminal_seen = asyncio.Event()
        events: list[object] = []
        screens = []

        class Stream(httpx.AsyncByteStream):
            close_count = 0

            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b'data: {"choices":[{"delta":{"role":"assistant","content":""},"finish_reason":""}]}\n\n'
                blocked.set()
                await release_prefix.wait()
                if outcome != "done-only":
                    yield b'data: {"choices":[{"delta":{"reasoning_content":"offline private analysis"},"finish_reason":""}]}\n\n'
                if outcome == "text":
                    yield b'data: {"choices":[{"delta":{"content":"Offline reply "},"finish_reason":""}]}\n\n'
                await release_tail.wait()
                if outcome == "text":
                    yield b'data: {"choices":[{"delta":{"content":"is complete."},"finish_reason":"stop"}]}\n\n'
                elif outcome == "reasoning-only":
                    yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                yield b"data: [DONE]\n\n"

            async def aclose(self) -> None:
                self.close_count += 1

        body = Stream()

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=body)

        transport = RecordingTransport(handler)

        def factory(connection) -> OpenAICompatibleProvider:
            return OpenAICompatibleProvider(
                connection.api_key,
                connection.api_url,
                transport=transport,
                max_retries=2,
            )

        class ObservedScreen(main_buffer.MainBufferScreen):
            def __init__(self, app, **kwargs) -> None:
                super().__init__(
                    app, clock=lambda: 0.0, branch_reader=lambda path: None, **kwargs
                )
                screens.append(self)

            async def handle_event(self, event: object) -> None:
                events.append(event)
                await super().handle_event(event)
                expected = TextDelta if outcome == "text" else ReasoningDelta
                if isinstance(event, expected):
                    progress_seen.set()
                if isinstance(event, (RunCompleted, RunFailed)):
                    terminal_seen.set()

        monkeypatch.setattr(main_buffer, "MainBufferScreen", ObservedScreen)
        monkeypatch.setattr(main_buffer, "Terminal", lambda: terminal)
        task = asyncio.create_task(
            cli.run_interactive(
                None,
                None,
                None,
                data_root=root,
                plain=False,
                provider_factory=factory,
                workspace=workspace,
                session_service=service,
                context_builder=ContextBuilder(data_root=root, workspace=workspace),
            )
        )
        try:
            await pump(lambda: fake.is_attached)
            screen = screens[0]
            fake.feed(*typed("hi"), ENTER)
            await asyncio.wait_for(blocked.wait(), 5)
            await pump(lambda: screen.mode == "running")
            selected = service.list_sessions().sessions[0]
            assert selected.meta.selection == SessionSelection("local", "a")
            assert selected.turns == selected.checkpoints == ()
            assert screen.mode == "running" and screen.status == "working"
            assert not terminal_seen.is_set()
            release_prefix.set()
            if outcome != "done-only":
                await asyncio.wait_for(progress_seen.wait(), 5)
                assert service.load_session(selected.meta.session_id).turns == ()
                assert not terminal_seen.is_set()
            release_tail.set()
            await asyncio.wait_for(terminal_seen.wait(), 5)
            await pump(lambda: screen.mode == "idle")
            stored = service.load_session(selected.meta.session_id)
            assert "offline private analysis" not in repr(stored)
            assert "TEST_ONLY_SECRET" not in repr(stored)
            if outcome == "text":
                assert sum(isinstance(e, RunCompleted) for e in events) == 1
                assert not any(isinstance(e, RunFailed) for e in events)
                assert screen.status == "done"
                assert len(stored.turns) == 1 and stored.checkpoints == ()
                assert (
                    stored.turns[0].assistant_message.content
                    == "Offline reply is complete."
                )
                visible = "Offline reply is complete."
            else:
                failures = [e for e in events if isinstance(e, RunFailed)]
                assert len(failures) == 1
                assert failures[0].category is ErrorCategory.MALFORMED
                assert not any(isinstance(e, RunCompleted) for e in events)
                assert screen.status == "error" and stored.turns == ()
                assert len(stored.checkpoints) == 1
                checkpoint = stored.checkpoints[0]
                assert checkpoint.kind is RunCheckpointKind.RECOVERABLE_FAILURE
                assert checkpoint.partial_assistant_message is None
                visible = "The provider returned an unusable response."
            await pump(lambda: any(visible in line for line in screen.renderer.frame()))
            ansi = "".join(terminal.emitted)
            vt = VirtualTerminal(cols=100, rows=35)
            vt.feed(ansi)
            assert any(visible in line for line in vt.screen())
            assert vt.violations == []
            assert "offline private analysis" not in "\n".join(vt.all_lines())
            assert "TEST_ONLY_SECRET" not in ansi
            (tmp_path / "stream-replay.ansi").write_text(ansi, encoding="utf-8")
            (tmp_path / "stream-frame.json").write_text(
                json.dumps(
                    {
                        "outcome": outcome,
                        "frame": vt.screen(),
                        "cursor": vt.cursor_cell(),
                        "status": screen.status,
                        "terminal_events": [
                            type(e).__name__
                            for e in events
                            if isinstance(e, (RunCompleted, RunFailed))
                        ],
                        "saved_replies": [
                            t.assistant_message.content for t in stored.turns
                        ],
                        "checkpoint_kinds": [c.kind.value for c in stored.checkpoints],
                    }
                ),
                encoding="utf-8",
            )
            fake.feed(CTRL_C, CTRL_C)
            assert await asyncio.wait_for(task, 5) == 0
            assert body.close_count == transport.aclose_count == 1
            assert len(transport.requests) == 1
            assert transport.responses[0].is_closed
            assert json.loads(transport.requests[0].read())["max_tokens"] == 4096
            closed_ansi = "".join(terminal.emitted)
            vt.feed(closed_ansi[len(ansi) :])
            assert vt.violations == []
            assert vt.autowrap and vt.cursor_visible
            assert fake.raw_mode_entries == 1
            assert screen.cleanup_errors == []
            (tmp_path / "stream-closed.ansi").write_text(closed_ansi, encoding="utf-8")
        finally:
            release_prefix.set()
            release_tail.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_distribution_declares_prompt_toolkit_and_rich() -> None:
    from importlib.metadata import requires

    declared = requires("mica-agent") or []
    assert any(entry.startswith("prompt-toolkit") for entry in declared)
    assert any(entry.startswith("rich") for entry in declared)
    assert not any(entry.startswith("textual") for entry in declared)
