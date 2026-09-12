"""P6-11A 主缓冲区前端集成测试：终端、Run、Ask、Permission、取消与退出。"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Self

import pytest
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

from mica_agent.app import MicaApp
from mica_agent.cancellation import CancellationToken
from mica_agent.context_builder import ContextBuilder
from mica_agent.interactions import (
    AskOption,
    AskPrompt,
    InteractionBroker,
)
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.permissions import PermissionMode
from mica_agent.policy import PolicyEngine, PolicyInput, PolicyOperation
from mica_agent.providers import Finish, ProviderEvent, ProviderRequest
from mica_agent.providers import TextDelta as ProviderTextDelta
from mica_agent.session_service import SessionService
from mica_agent.tool_contracts import (
    ToolCall,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)
from mica_agent.tool_executor import ToolExecutor
from mica_agent.tool_registry import ToolRegistry
from mica_agent.tui.main_buffer import MainBufferScreen, Terminal
from mica_agent.tui.main_buffer.terminal import FORBIDDEN_SEQUENCES
from mica_agent.tui.models import StartupResourceSnapshot, TuiSessionSnapshot
from mica_agent.workspace import resolve_workspace

ENTER = KeyPress(Keys.ControlM, "\r")
ESCAPE = KeyPress(Keys.Escape, "\x1b")
CTRL_C = KeyPress(Keys.ControlC, "\x03")
DOWN = KeyPress(Keys.Down, "\x1b[B")
TIMEOUT = 5.0


def typed(value: str) -> list[KeyPress]:
    return [KeyPress(character, character) for character in value]


class _NullContext(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeInput:
    """确定性的低层输入替身：只实现主缓冲区用到的 Input 子集。"""

    def __init__(
        self, *, fail_raw_mode: bool = False, fail_attach: bool = False
    ) -> None:
        self._pending: list[Any] = []
        self._callback: Any = None
        self.raw_mode_entries = 0
        self.close_calls = 0
        self.is_attached = False
        self.fail_raw_mode = fail_raw_mode
        self.fail_attach = fail_attach

    def feed(self, *keypresses: Any) -> None:
        self._pending.extend(keypresses)
        if self._callback is not None:
            self._callback()

    def read_keys(self) -> list[Any]:
        pending, self._pending = self._pending, []
        return pending

    def flush_keys(self) -> list[Any]:
        pending, self._pending = self._pending, []
        return pending

    def attach(self, callback: Any) -> AbstractContextManager[None]:
        if self.fail_attach:
            raise RuntimeError("attach failed")
        self._callback = callback
        self.is_attached = True
        return _NullContext()

    def raw_mode(self) -> AbstractContextManager[None]:
        if self.fail_raw_mode:
            raise RuntimeError("raw mode failed")
        self.raw_mode_entries += 1
        return _NullContext()

    def close(self) -> None:
        self.close_calls += 1


class ScriptedProvider:
    def __init__(
        self, scripts: list[list[ProviderEvent]], *, block_after_text: bool = False
    ) -> None:
        self.scripts = scripts
        self.block_after_text = block_after_text
        self.requests: list[ProviderRequest] = []
        self.aclose_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        self.started.set()
        for event in self.scripts[min(len(self.requests) - 1, len(self.scripts) - 1)]:
            yield event
            if (
                self.block_after_text
                and isinstance(event, ProviderTextDelta)
                and not self.release.is_set()
            ):
                await self.release.wait()

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self.release.set()


class RecordingBroker(InteractionBroker):
    def __init__(self) -> None:
        super().__init__()
        self.responses: list[Any] = []

    def respond(self, response: Any) -> bool:
        self.responses.append(response)
        return super().respond(response)


def make_app(
    tmp_path: Path,
    provider: ScriptedProvider,
    *,
    tool_executor: ToolExecutor | None = None,
    app_cls: type[MicaApp] = MicaApp,
) -> MicaApp:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir(parents=True, exist_ok=True)
    workspace = resolve_workspace(workspace_path)
    data_root = tmp_path / "data"
    service = SessionService(JsonlSessionStore(workspace, data_root))
    session = service.create_session()
    return app_cls(
        provider,
        "test-model",
        session_service=service,
        session_id=session.meta.session_id,
        context_builder=ContextBuilder(data_root=data_root, workspace=workspace),
        tool_executor=tool_executor,
    )


def make_screen(
    app: MicaApp,
    fake_input: FakeInput,
    *,
    output: io.StringIO | None = None,
    size: tuple[int, int] = (80, 24),
) -> tuple[MainBufferScreen, io.StringIO]:
    stream = io.StringIO() if output is None else output
    terminal = Terminal(
        output=stream,
        input_factory=lambda: fake_input,
        size=size,
        environ={"NO_COLOR": "1"},
        interactive=True,
    )
    screen = MainBufferScreen(
        app,
        terminal=terminal,
        startup_resources=StartupResourceSnapshot(
            system_prompt_name="Mica built-in system prompt",
            tool_names=("ask", "bash"),
        ),
        session_snapshot=TuiSessionSnapshot(
            workspace="~/workspace",
            session_label="session-title",
            model="test-model",
        ),
        workspace_path=tmp_workspace(app),
    )
    return screen, stream


def tmp_workspace(app: MicaApp) -> Path:
    return app._session_service.store.workspace.workspace_path


async def pump(predicate: Any, *, message: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TIMEOUT
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {message}")
        await asyncio.sleep(0.005)


class ScreenHarness:
    """在后台运行主缓冲区前端，并提供按键与断言辅助。"""

    def __init__(self, screen: MainBufferScreen, fake_input: FakeInput) -> None:
        self.screen = screen
        self.input = fake_input
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self.task = asyncio.create_task(self.screen.run())
        await pump(lambda: self.input.is_attached, message="input attach")
        await pump(lambda: self.screen.renderer is not None, message="renderer")
        return self

    async def __aexit__(self, *exc: object) -> bool:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
        return False

    def output(self) -> str:
        return "".join(self.screen._terminal.emitted)


# -- 首帧与闭环 -------------------------------------------------------------


def test_startup_frame_keeps_main_buffer_and_avoids_forbidden_sequences(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider([[ProviderTextDelta("x"), Finish()]])
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        screen, _ = make_screen(app, fake, size=(70, 20))
        async with ScreenHarness(screen, fake) as harness:
            frame = screen.renderer.frame()
            text = "\n".join(frame)
            assert "Mica is ready." in text
            assert "~/workspace" in text
            assert "sessions: session-title" in text
            assert "test-model" in text
            for sequence in FORBIDDEN_SEQUENCES:
                assert sequence not in harness.output()
            assert screen.mode == "idle"

            fake.feed(CTRL_C)
            fake.feed(CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)
        assert screen.mode == "closed"
        assert fake.raw_mode_entries == 1
        assert "\x1b[?7l" in harness.output()

    asyncio.run(scenario())


def test_submit_runs_model_stream_and_restores_composer(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider([[ProviderTextDelta("Hello"), Finish()]])
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            assert screen.status == "idle"
            fake.feed(*typed("hi there"))
            await pump(lambda: screen.composer_text == "hi there")
            fake.feed(ENTER)
            await pump(lambda: screen.mode == "running", message="running")
            await pump(
                lambda: screen.mode == "idle" and screen.status == "done",
                message="done",
            )
            frame = screen.renderer.frame()
            assert "hi there" in frame
            assert any("Hello" in line for line in frame)
            assert app.history[-1].content == "Hello"
            assert screen.composer_text == ""
            await pump(
                lambda: any("done(" in line for line in screen.renderer.frame()),
                message="done status",
            )

            fake.feed(CTRL_C)
            fake.feed(CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)
        assert provider.aclose_calls == 1

    asyncio.run(scenario())


def test_running_state_ignores_composer_input_and_second_submit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [[ProviderTextDelta("answer"), Finish()]], block_after_text=True
        )
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("first"), ENTER)
            await asyncio.wait_for(provider.started.wait(), timeout=TIMEOUT)
            fake.feed(*typed("ignored"))
            await asyncio.sleep(0.05)
            assert screen.composer_text == ""
            assert provider.requests and len(provider.requests) == 1
            provider.release.set()
            await pump(
                lambda: screen.mode == "idle" and screen.status == "done",
                message="done",
            )
            assert len(provider.requests) == 1

            fake.feed(CTRL_C)
            fake.feed(CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_escape_cancels_active_run_without_committing_turn(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [[ProviderTextDelta("partial"), Finish()]], block_after_text=True
        )
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("cancel me"), ENTER)
            await asyncio.wait_for(provider.started.wait(), timeout=TIMEOUT)
            fake.feed(*typed("x"))
            fake.feed(ESCAPE)
            await pump(
                lambda: screen.status == "stop" and screen.mode == "idle",
                message="stop",
            )
            assert app.history == ()
            assert screen.composer_text == ""
            frame = screen.renderer.frame()
            assert any("stop" in line for line in frame)

            fake.feed(CTRL_C)
            fake.feed(CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


# -- Ask 与权限阻塞模式 -----------------------------------------------------


def make_ask_executor(broker: InteractionBroker) -> tuple[ToolExecutor, list[str]]:
    calls: list[str] = []
    registry = ToolRegistry()
    spec = ToolSpec("ask", "Ask", {"type": "object", "required": ["question"]})

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        response = await broker(
            request.call_id,
            AskPrompt(
                request.arguments["question"],
                options=(AskOption("Alice"), AskOption("Bob")),
            ),
            cancellation,
        )
        calls.append(request.call_id)
        return ToolResult(
            request.call_id,
            request.name,
            ToolResultStatus.SUCCESS,
            "answer received",
            {"answer": response.answer},
        )

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        interaction_broker=broker,
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.ASK, False, False
        ),
    )
    return executor, calls


def test_ask_blocks_composer_and_returns_selected_answer(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = RecordingBroker()
        executor, calls = make_ask_executor(broker)
        provider = ScriptedProvider(
            [
                [
                    Finish(
                        tool_calls=(ToolCall("call-ask", "ask", {"question": "Who?"}),)
                    )
                ],
                [ProviderTextDelta("final"), Finish()],
            ]
        )
        app = make_app(tmp_path, provider, tool_executor=executor)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("run ask"), ENTER)
            await pump(lambda: screen.mode == "ask", message="ask mode")
            frame = "\n".join(screen.renderer.frame())
            assert "Who?" in frame
            assert "Alice" in frame and "Bob" in frame

            fake.feed(DOWN, ENTER)
            await pump(
                lambda: screen.mode == "idle" and screen.status == "done",
                message="done",
            )
            assert broker.responses[-1].answer == "Bob"
            assert calls == ["call-ask"]
            assert "final" in "\n".join(screen.renderer.frame())

            fake.feed(CTRL_C)
            fake.feed(CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def make_permission_executor(
    broker: InteractionBroker,
) -> tuple[ToolExecutor, list[str]]:
    calls: list[str] = []
    registry = ToolRegistry()
    spec = ToolSpec("write", "Write", {"type": "object"})

    async def handler(
        request: ToolRequest, cancellation: CancellationToken
    ) -> ToolResult:
        calls.append(request.call_id)
        return ToolResult(
            request.call_id, request.name, ToolResultStatus.SUCCESS, "written"
        )

    registry.register(spec, handler)
    executor = ToolExecutor(
        registry,
        PolicyEngine(PermissionMode.DEFAULT),
        interaction_broker=broker,
        policy_input_factory=lambda request, spec: PolicyInput(
            request.name, PolicyOperation.OTHER, False, False
        ),
    )
    return executor, calls


def test_permission_blocks_and_allow_once_executes_tool(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = RecordingBroker()
        executor, calls = make_permission_executor(broker)
        provider = ScriptedProvider(
            [
                [Finish(tool_calls=(ToolCall("call-write", "write", {}),))],
                [ProviderTextDelta("after"), Finish()],
            ]
        )
        app = make_app(tmp_path, provider, tool_executor=executor)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("write it"), ENTER)
            await pump(lambda: screen.mode == "permission", message="permission")
            frame = "\n".join(screen.renderer.frame())
            assert "Do you want to proceed?" in frame
            assert "1. Yes" in frame and "2. No" in frame

            fake.feed(ENTER)
            await pump(
                lambda: screen.mode == "idle" and screen.status == "done",
                message="done",
            )
            assert calls == ["call-write"]
            assert "after" in "\n".join(screen.renderer.frame())

            fake.feed(CTRL_C)
            fake.feed(CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_permission_deny_keeps_run_alive_without_executing(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = RecordingBroker()
        executor, calls = make_permission_executor(broker)
        provider = ScriptedProvider(
            [
                [Finish(tool_calls=(ToolCall("call-write", "write", {}),))],
                [ProviderTextDelta("recovered"), Finish()],
            ]
        )
        app = make_app(tmp_path, provider, tool_executor=executor)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("write it"), ENTER)
            await pump(lambda: screen.mode == "permission", message="permission")
            fake.feed(DOWN, ENTER)
            await pump(
                lambda: screen.mode == "idle" and screen.status == "done",
                message="done",
            )
            assert calls == []
            assert "recovered" in "\n".join(screen.renderer.frame())

            fake.feed(CTRL_C)
            fake.feed(CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_escape_during_permission_cancels_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = RecordingBroker()
        executor, calls = make_permission_executor(broker)
        provider = ScriptedProvider(
            [[Finish(tool_calls=(ToolCall("call-write", "write", {}),))]]
        )
        app = make_app(tmp_path, provider, tool_executor=executor)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("write it"), ENTER)
            await pump(lambda: screen.mode == "permission", message="permission")
            fake.feed(ESCAPE)
            await pump(
                lambda: screen.mode == "idle" and screen.status == "stop",
                message="stop",
            )
            assert calls == []
            assert app.history == ()

            fake.feed(CTRL_C)
            fake.feed(CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


# -- 退出语义 ---------------------------------------------------------------


def test_failed_tool_record_shows_failure_and_run_continues(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        registry = ToolRegistry()
        spec = ToolSpec("read", "Read", {"type": "object"})

        async def handler(
            request: ToolRequest, cancellation: CancellationToken
        ) -> ToolResult:
            calls.append(request.call_id)
            return ToolResult(
                request.call_id,
                request.name,
                ToolResultStatus.EXECUTION_FAILED,
                "failed safely",
            )

        registry.register(spec, handler)
        executor = ToolExecutor(
            registry,
            PolicyEngine(PermissionMode.DEFAULT),
            policy_input_factory=lambda request, spec: PolicyInput(
                request.name, PolicyOperation.SAFE_READ_ONLY_COMMAND, False, False
            ),
        )
        provider = ScriptedProvider(
            [
                [Finish(tool_calls=(ToolCall("call-read", "read", {}),))],
                [ProviderTextDelta("recovered"), Finish()],
            ]
        )
        app = make_app(tmp_path, provider, tool_executor=executor)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("read it"), ENTER)
            await pump(
                lambda: screen.mode == "idle" and screen.status == "done",
                message="done",
            )
            assert calls == ["call-read"]
            frame = "\n".join(screen.renderer.frame())
            assert "× read" in frame
            assert "recovered" in frame

            fake.feed(CTRL_C)
            fake.feed(CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_single_ctrl_c_does_not_exit_and_timeout_rearms(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider([[ProviderTextDelta("x"), Finish()]])
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        clock_value = [0.0]
        screen, _ = make_screen(app, fake)
        screen._clock = lambda: clock_value[0]
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(CTRL_C)
            await asyncio.sleep(0.05)
            assert screen.mode == "idle"
            assert harness.task.done() is False

            clock_value[0] = 10.0
            fake.feed(CTRL_C)
            await asyncio.sleep(0.05)
            assert harness.task.done() is False

            clock_value[0] = 11.0
            fake.feed(CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)
        assert screen.mode == "closed"

    asyncio.run(scenario())


def test_non_interactive_output_is_refused_without_touching_the_document(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider([[ProviderTextDelta("x"), Finish()]])
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        stream = io.StringIO()
        terminal = Terminal(
            output=stream,
            input_factory=lambda: fake,
            size=(80, 24),
            environ={"NO_COLOR": "1"},
            interactive=False,
        )
        screen = MainBufferScreen(app, terminal=terminal)
        with pytest.raises(RuntimeError):
            await screen.run()
        assert stream.getvalue() == ""
        assert fake.raw_mode_entries == 0

    asyncio.run(scenario())


# -- §3 提交被运行时拒绝时保留草稿 -------------------------------------------


PASTE_TEXT = chr(10).join(f"pasted line {index}" for index in range(12))


def _reject_submit(_prompt: str) -> bool:
    return False


def test_rejected_submit_keeps_the_plain_draft(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider([[]])
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            screen._runtime.submit = _reject_submit  # type: ignore[method-assign]
            fake.feed(*typed("keep me"))
            await pump(lambda: screen.composer_text == "keep me")
            document = screen.renderer.document_lines()
            cursor = screen.editor.cursor
            fake.feed(ENTER)
            await pump(lambda: len(screen.diagnostics) == 1, message="rejected")

            assert screen.diagnostics == ["submit rejected"]
            assert screen.mode == "idle"
            assert screen.status == "idle"
            assert screen.composer_text == "keep me"
            assert screen.editor.cursor == cursor
            assert screen.renderer.document_lines() == document
            assert "keep me" not in chr(10).join(screen.renderer.document_lines())
            assert screen.run_id is None
            assert screen.refresh_task is None
            frame = chr(10).join(screen.renderer.frame())
            assert "submit rejected" not in frame
            assert "Traceback" not in frame
            assert screen.mode == "idle"

            # 之后仍可正常退出，说明拒绝路径没有留下半成品状态。
            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_rejected_submit_keeps_the_paste_fragment(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider([[]])
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            screen._runtime.submit = _reject_submit  # type: ignore[method-assign]
            fake.feed(KeyPress(Keys.BracketedPaste, PASTE_TEXT))
            await pump(
                lambda: screen.composer_text.startswith("[paste #1"), message="paste"
            )
            shown = screen.composer_text
            fragments = screen.editor.fragment_summaries
            cursor = screen.editor.cursor
            assert fragments == ["[paste #1 +12 lines]"]
            assert screen.editor.submit_text() == PASTE_TEXT

            fake.feed(ENTER)
            await pump(lambda: len(screen.diagnostics) == 1, message="rejected")

            assert screen.composer_text == shown
            assert screen.editor.fragment_summaries == fragments
            assert screen.editor.submit_text() == PASTE_TEXT
            assert screen.editor.cursor == cursor
            assert screen.mode == "idle"
            assert screen.status == "idle"
            assert screen.run_id is None
            assert screen.refresh_task is None
            assert "pasted line 0" not in chr(10).join(screen.renderer.document_lines())

            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())
