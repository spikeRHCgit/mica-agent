"""P5-11A 主缓冲区前端集成测试：终端、Run、Ask、Permission、取消与退出。"""

from __future__ import annotations

import asyncio
import io
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

from mica_agent.app import MicaApp
from mica_agent.cancellation import CancellationToken
from mica_agent.checkpoint_contracts import RunCheckpointKind
from mica_agent.context_builder import ContextBuilder
from mica_agent.interactions import (
    AskOption,
    AskPrompt,
    InteractionBroker,
    UserInputResponse,
)
from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.models import MessageRole
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
from mica_agent.tui.main_buffer.editor import normalize_key
from mica_agent.tui.main_buffer.terminal import (
    FORBIDDEN_SEQUENCES,
    KEY_EVENT_TYPE,
    NAVIGATION_VIRTUAL_KEYS,
    WindowsVtKeyReader,
    find_forbidden_sequences,
    install_windows_navigation_fallback,
    windows_key_event,
    windows_navigation_reader,
    windows_record_sequences,
)
from mica_agent.tui.models import StartupResourceSnapshot, TuiSessionSnapshot
from mica_agent.workspace import resolve_workspace

ENTER = KeyPress(Keys.ControlM, "\r")
ESCAPE = KeyPress(Keys.Escape, "\x1b")
CTRL_C = KeyPress(Keys.ControlC, "\x03")
DOWN = KeyPress(Keys.Down, "\x1b[B")
TIMEOUT = 5.0

VK_UP = 0x26
VK_DOWN = 0x28
WIN32_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="windows console records require win32"
)
# 等效 VT 序列与其解析结果：读取层产出的按键必须与标准终端路径完全一致。
NAVIGATION_KEYS = {
    "\x1b[A": Keys.Up,
    "\x1b[B": Keys.Down,
    "\x1b[D": Keys.Left,
    "\x1b[C": Keys.Right,
    "\x1b[H": Keys.Home,
    "\x1b[F": Keys.End,
    "\x1b[3~": Keys.Delete,
}


def console_record(
    unicode_char: str = "\x00",
    virtual_key_code: int = 0,
    *,
    key_down: bool = True,
    event_type: int = KEY_EVENT_TYPE,
) -> SimpleNamespace:
    """建模一条 Windows 控制台 INPUT_RECORD（只含读取层真正用到的字段）。"""
    key_event = SimpleNamespace(
        KeyDown=1 if key_down else 0,
        VirtualKeyCode=virtual_key_code,
        uChar=SimpleNamespace(UnicodeChar=unicode_char),
    )
    return SimpleNamespace(
        EventType=event_type, Event=SimpleNamespace(KeyEvent=key_event)
    )


def real_console_record(
    unicode_char: str, virtual_key_code: int, *, key_down: bool = True
) -> Any:
    """构造锁定版 prompt_toolkit 的真实 ``INPUT_RECORD``：不需要控制台句柄。"""
    from prompt_toolkit.win32_types import INPUT_RECORD

    record = INPUT_RECORD()
    record.EventType = KEY_EVENT_TYPE
    record.Event.KeyEvent.KeyDown = 1 if key_down else 0
    record.Event.KeyEvent.VirtualKeyCode = virtual_key_code
    record.Event.KeyEvent.uChar.UnicodeChar = unicode_char
    return record


def batch_of(records: list[Any]) -> SimpleNamespace:
    """按 ``ReadConsoleInputW`` 的输出形状包装一批记录。"""
    return SimpleNamespace(value=len(records))


class ModelledConsoleReader:
    """按锁定版 ``Vt100ConsoleInputReader`` 形状建模的 Windows 读取器替身。

    平台 IO 只有一个可注入的记录队列：``read()`` 的语义与 prompt_toolkit
    完全一致（取一批记录 → ``_get_keys`` → 喂 ``Vt100Parser``），因此叠加
    导航后备之后的行为可以在没有控制台句柄的环境里断言。
    """

    def __init__(self) -> None:
        self._buffer: list[Any] = []
        self._vt100_parser = Vt100Parser(
            lambda key_press: self._buffer.append(key_press)
        )
        self.handle = None
        self.batches: list[list[Any]] = []
        self.close_calls = 0

    def push(self, *records: Any) -> None:
        self.batches.append([*records])

    def read(self) -> list[Any]:
        records = self.batches.pop(0) if self.batches else []
        for data in self._get_keys(batch_of(records), records):
            self._vt100_parser.feed(data)
        return self._take()

    def _get_keys(self, count: Any, input_records: Any) -> Iterator[str]:
        """锁定版旧实现的语义：只产出带字符的记录。"""
        for index in range(count.value):
            event = windows_key_event(input_records[index])
            if event is not None and event.uChar.UnicodeChar != "\x00":
                yield event.uChar.UnicodeChar

    def flush_keys(self) -> list[Any]:
        self._vt100_parser.flush()
        return self._take()

    def close(self) -> None:
        self.close_calls += 1

    def _take(self) -> list[Any]:
        result, self._buffer = self._buffer, []
        return result


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


class WindowsConsoleInput(FakeInput):
    """把建模的 Windows 控制台记录接入真实前端的输入替身。"""

    def __init__(self) -> None:
        super().__init__()
        self.reader = windows_navigation_reader(ModelledConsoleReader())
        self.direct: list[Any] = []

    def feed(self, *keypresses: Any) -> None:
        """直接喂入已构造的按键，语义与 ``FakeInput.feed`` 一致。"""
        self.direct.extend(keypresses)
        if self._callback is not None:
            self._callback()

    def push(self, *records: Any) -> None:
        """喂入一批控制台记录，并按真实输入回调的语义通知前端。"""
        self.reader.push(*records)
        if self._callback is not None:
            self._callback()

    def read_keys(self) -> list[Any]:
        direct, self.direct = self.direct, []
        return [*direct, *self.reader.read()]

    def flush_keys(self) -> list[Any]:
        direct, self.direct = self.direct, []
        return [*direct, *self.reader.flush_keys()]


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
    escape_flush_delay: float = 0.0,
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
        escape_flush_delay=escape_flush_delay,
    )
    return screen, stream


def tmp_workspace(app: MicaApp) -> Path:
    return app._session_service.store.workspace.workspace_path


def freeze_done_window(screen: MainBufferScreen) -> None:
    """冻结屏幕时钟，让 ``done`` 状态在测试里稳定存在。

    ``done`` 按设计只显示 ``DONE_VISIBLE_SECONDS``（2 秒）后淡出为 ``idle``。
    把「恰好抓到 done」当作同步点时，负载造成的调度延迟会让测试错过这个窗口，
    从而把「产品已经正常结束」误判为超时（P5-11C-MR2 已用探针单独复现并证明：
    停顿 2.5 秒后 Run 仍已完成，只是状态已淡出）。冻结时钟不改变任何产品行为，
    只把被观测的瞬时状态固定下来。
    """
    screen._clock = lambda: 0.0


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
        freeze_done_window(screen)
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
            # 用户消息带左侧身份标识，因此按行包含判断。
            assert any("hi there" in line for line in frame)
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
        freeze_done_window(screen)
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
            history = app.history
            assert history[0].role is MessageRole.USER
            assert history[0].content == "cancel me"
            assert history[-1].role is MessageRole.SYSTEM
            assert "cancelled" in history[-1].content
            stored = app._session_service.load_session(app.session_id)
            assert stored.turns == ()
            assert stored.checkpoints[0].kind is RunCheckpointKind.CANCELLED
            assert screen.composer_text == ""
            frame = screen.renderer.frame()
            assert any("stop" in line for line in frame)

            fake.feed(CTRL_C)
            fake.feed(CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


# -- P5-11C-MR3：composer 物理光标即时刷新 ------------------------------------

LEFT = KeyPress(Keys.Left, "\x1b[D")
RIGHT = KeyPress(Keys.Right, "\x1b[C")
UP = KeyPress(Keys.Up, "\x1b[A")
HOME = KeyPress(Keys.Home, "\x1b[H")
END = KeyPress(Keys.End, "\x1b[F")
DELETE = KeyPress(Keys.Delete, "\x1b[3~")
NEWLINE = KeyPress(Keys.ControlJ, "\n")


def test_composer_navigation_moves_the_physical_cursor_immediately(
    tmp_path: Path,
) -> None:
    """方向键/Home/End 改变逻辑光标后，物理光标必须在同一轮输入里就移动。

    只走真实输入队列（``FakeInput`` → Screen 模式机 → 渲染器 → ``Terminal``），
    物理结果由累计字节流回放到虚拟终端后断言；只移动光标时不得重写任何物理行。
    """

    async def scenario() -> None:
        from test_tui_main_buffer_vt import VirtualTerminal

        provider = ScriptedProvider([[ProviderTextDelta("x"), Finish()]])
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        terminal = screen._terminal

        def physical() -> VirtualTerminal:
            view = VirtualTerminal(rows=24, cols=80)
            view.feed(terminal.emitted_text())
            return view

        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("hello"))
            await pump(lambda: screen.composer_text == "hello", message="draft")
            view = physical()
            composer_row = view.row_index("› hello")
            assert composer_row >= 0, view.screen()
            # 边框 1 列 + 提示符 2 列 + 5 个字符。
            assert view.cursor_cell() == (composer_row, 8)
            stable_lines = view.all_lines()

            for key, expected in (
                (LEFT, 4),
                (LEFT, 3),
                (RIGHT, 4),
                (HOME, 0),
                (END, 5),
            ):
                before = len(terminal.emitted_text())
                fake.feed(key)
                await pump(
                    lambda expected=expected: screen.editor.cursor == expected,
                    message=f"logical cursor {expected}",
                )
                delta = terminal.emitted_text()[before:]
                # 同一轮输入里就写出了定位序列：不写正文、不擦除、不清屏。
                assert delta, "cursor-only 帧必须立即写出"
                assert "\x1b[J" not in delta and "hello" not in delta, repr(delta)
                view = physical()
                assert view.cursor_cell() == (composer_row, 3 + expected), view.screen()
                # 只有光标移动：草稿与全部物理行完全不变。
                assert screen.composer_text == "hello"
                assert view.all_lines() == stable_lines

            # 多逻辑行：Up/Down 让物理光标跨行即时移动。
            fake.feed(HOME, DELETE, DELETE, DELETE, DELETE, DELETE)
            await pump(lambda: screen.composer_text == "", message="cleared draft")
            fake.feed(*typed("alpha"), NEWLINE, *typed("beta"))
            await pump(
                lambda: screen.composer_text == "alpha\nbeta", message="multiline draft"
            )
            view = physical()
            top = view.row_index("› alpha")
            bottom = view.row_index("  beta")
            assert top >= 0 and bottom == top + 1, view.screen()
            assert view.cursor_cell() == (bottom, 7)
            multiline_lines = view.all_lines()

            fake.feed(UP)
            await pump(lambda: screen.editor.cursor_row == 0, message="cursor row up")
            view = physical()
            assert view.cursor_cell() == (top, 7), view.screen()
            assert screen.composer_text == "alpha\nbeta"
            assert view.all_lines() == multiline_lines

            fake.feed(DOWN)
            await pump(lambda: screen.editor.cursor_row == 1, message="cursor row down")
            view = physical()
            assert view.cursor_cell() == (bottom, 7), view.screen()
            assert view.all_lines() == multiline_lines

            assert find_forbidden_sequences(terminal.emitted_text()) == []
            assert view.violations == []
            assert view.autowrap is False

            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_composer_cursor_only_frames_follow_soft_wrapped_rows(tmp_path: Path) -> None:
    """软换行后 Left/Home/End 跨显示行移动时，物理光标仍然即时跟随。"""

    async def scenario() -> None:
        from test_tui_main_buffer_vt import VirtualTerminal

        provider = ScriptedProvider([[ProviderTextDelta("x"), Finish()]])
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        screen, _ = make_screen(app, fake, size=(40, 24))
        terminal = screen._terminal

        def physical() -> VirtualTerminal:
            view = VirtualTerminal(rows=24, cols=40)
            view.feed(terminal.emitted_text())
            return view

        draft = "0123456789" * 4  # 40 列落在 36 列的正文宽度上 → 软换行成两行
        first, second = draft[:36], draft[36:]
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed(draft))
            await pump(lambda: screen.composer_text == draft, message="wrapped draft")
            view = physical()
            lines = view.all_lines()
            first_row = view.row_index("› " + first)
            second_row = view.row_index("  " + second)
            assert first_row >= 0 and second_row == first_row + 1, view.screen()
            assert view.cursor_cell() == (second_row, 3 + 4)

            # End → Home：物理光标跨软换行行即时回到第一行行首。
            fake.feed(HOME)
            await pump(lambda: screen.editor.cursor == 0, message="home")
            view = physical()
            assert view.cursor_cell() == (first_row, 3)
            assert view.all_lines() == lines
            assert screen.composer_text == draft

            fake.feed(END)
            await pump(lambda: screen.editor.cursor == len(draft), message="end")
            view = physical()
            assert view.cursor_cell() == (second_row, 3 + 4)
            assert view.all_lines() == lines

            # 连续 Left 越过软换行边界：行号与列号同时变化。
            # 恰好落在换行边界上的位置按产品既有约定显示在上一显示行行尾。
            crossings = (
                (39, (second_row, 3 + 3)),
                (38, (second_row, 3 + 2)),
                (37, (second_row, 3 + 1)),
                (36, (first_row, 3 + 36)),
                (35, (first_row, 3 + 35)),
            )
            for expected, cell in crossings:
                before = len(terminal.emitted_text())
                fake.feed(LEFT)
                await pump(
                    lambda expected=expected: screen.editor.cursor == expected,
                    message=f"cursor {expected}",
                )
                delta = terminal.emitted_text()[before:]
                assert delta and "\x1b[J" not in delta, repr(delta)
                view = physical()
                assert view.cursor_cell() == cell, view.screen()
                assert view.all_lines() == lines
                assert screen.composer_text == draft

            assert find_forbidden_sequences(terminal.emitted_text()) == []
            assert view.violations == []

            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


# -- Ask 与权限阻塞模式 -----------------------------------------------------


def make_ask_executor(
    broker: InteractionBroker, *, allow_custom: bool = False
) -> tuple[ToolExecutor, list[str]]:
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
                allow_custom=allow_custom,
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
        freeze_done_window(screen)
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
        freeze_done_window(screen)
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
        freeze_done_window(screen)
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
            history = app.history
            assert history[0].content == "write it"
            assert "cancelled" in history[-1].content
            stored = app._session_service.load_session(app.session_id)
            assert stored.turns == ()
            assert stored.checkpoints[0].kind is RunCheckpointKind.CANCELLED

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
        freeze_done_window(screen)
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


# -- P5-11C-MR2：Windows VT 零字符导航后备与 Esc flush ------------------------


def test_windows_record_sequences_map_zero_char_navigation_records() -> None:
    """零字符导航记录必须映射为等效 VT 序列；其余记录一律不产出。"""
    for virtual_key_code, sequence in NAVIGATION_VIRTUAL_KEYS.items():
        records = [console_record("\x00", virtual_key_code)]
        assert windows_record_sequences(records) == [sequence]

    # key-up 不是第二次按键。
    assert (
        windows_record_sequences([console_record("\x00", VK_UP, key_down=False)]) == []
    )
    # 不支持的 VirtualKeyCode 与鼠标/焦点等非按键事件不产出任何输入。
    assert windows_record_sequences([console_record("\x00", 0x70)]) == []
    assert (
        windows_record_sequences([console_record("\x00", VK_UP, event_type=16)]) == []
    )
    assert windows_record_sequences([]) == []


def test_windows_record_sequences_keep_standard_character_records() -> None:
    """标准 VT 字符继续原样进入锁定版解析器，不被后备改写。"""
    arrow = [console_record(character, VK_DOWN) for character in "\x1b[B"]
    assert windows_record_sequences(arrow) == ["\x1b", "[", "B"]
    assert windows_record_sequences([console_record("a")]) == ["a"]
    assert windows_record_sequences([console_record("\r")]) == ["\r"]
    assert windows_record_sequences([console_record("中")]) == ["中"]


def test_windows_record_sequences_never_double_a_single_physical_key() -> None:
    """同一批里已有字符记录的按键不再走后备。"""
    mixed = [
        console_record("\x1b", VK_UP),
        console_record("\x00", VK_UP),
        console_record("[", VK_UP),
        console_record("A", VK_UP),
    ]
    assert windows_record_sequences(mixed) == ["\x1b", "[", "A"]
    # 不同 VirtualKeyCode 的零字符记录不受影响。
    both = [console_record("a", 0x41), console_record("\x00", VK_UP)]
    assert windows_record_sequences(both) == ["a", "\x1b[A"]


def test_windows_vt_key_reader_parses_backup_and_standard_records() -> None:
    for virtual_key_code, sequence in NAVIGATION_VIRTUAL_KEYS.items():
        reader = WindowsVtKeyReader()
        reader.feed_records([console_record("\x00", virtual_key_code)])
        keys = reader.take()
        assert [keypress.key for keypress in keys] == [NAVIGATION_KEYS[sequence]]
        assert [keypress.data for keypress in keys] == [sequence]

    reader = WindowsVtKeyReader()
    reader.feed_records(
        [console_record("\x00", VK_UP), console_record("\x00", VK_DOWN)]
    )
    assert [keypress.key for keypress in reader.take()] == [Keys.Up, Keys.Down]

    # 标准路径：三条字符记录合成一次导航，且不会有第二次。
    reader.feed_records([console_record(character, VK_DOWN) for character in "\x1b[B"])
    keys = reader.take()
    assert len(keys) == 1
    assert (keys[0].key, keys[0].data) == (Keys.Down, "\x1b[B")

    # 普通按键不回归：数字、Enter、Backspace、中文。
    reader.feed_records(
        [
            console_record("2"),
            console_record("\r"),
            console_record("\x7f"),
            console_record("中"),
        ]
    )
    assert [normalize_key(keypress).name for keypress in reader.take()] == [
        "text",
        "enter",
        "backspace",
        "text",
    ]


def test_lone_escape_arrives_only_after_the_bounded_flush() -> None:
    reader = WindowsVtKeyReader()
    reader.feed_records([console_record("\x1b")])
    assert reader.take() == []
    keys = reader.flush()
    assert [keypress.key for keypress in keys] == [Keys.Escape]


@WIN32_ONLY
def test_real_windows_records_confirm_the_stock_reader_gap() -> None:
    """真实 INPUT_RECORD：旧实现丢弃零字符导航记录，新读取器补上按键。"""
    from prompt_toolkit.input.win32 import Vt100ConsoleInputReader

    records = [real_console_record("\x00", VK_UP), real_console_record("\x00", VK_DOWN)]
    # 旧实现只测记录解释这一步，因此不需要真的拥有控制台句柄。
    stock = Vt100ConsoleInputReader.__new__(Vt100ConsoleInputReader)
    assert list(stock._get_keys(batch_of(records), records)) == []

    reader = WindowsVtKeyReader()
    reader.feed_records(records)
    assert [keypress.key for keypress in reader.take()] == [Keys.Up, Keys.Down]

    reader.feed_records([real_console_record("\x00", VK_UP, key_down=False)])
    assert reader.take() == []


@WIN32_ONLY
def test_navigation_reader_installs_once_and_keeps_standard_records() -> None:
    class FakeWin32Input:
        def __init__(self, reader: Any) -> None:
            self.console_input_reader = reader

    original = ModelledConsoleReader()
    inner = FakeWin32Input(original)
    assert install_windows_navigation_fallback(inner) is True
    reader = inner.console_input_reader
    assert reader is not original
    assert original.close_calls == 1

    reader.push(console_record("\x00", VK_UP))
    assert [keypress.key for keypress in reader.read()] == [Keys.Up]

    reader.push(*[console_record(character, VK_DOWN) for character in "\x1b[B"])
    keys = reader.read()
    assert [(keypress.key, keypress.data) for keypress in keys] == [
        (Keys.Down, "\x1b[B")
    ]

    # 非 VT 读取器与非 Windows 控制台输入都不改造。
    assert install_windows_navigation_fallback(FakeWin32Input(object())) is False
    assert install_windows_navigation_fallback(object()) is False


def test_zero_char_records_move_the_ask_marker_on_the_physical_screen(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        from test_tui_main_buffer_vt import VirtualTerminal

        broker = RecordingBroker()
        executor, calls = make_ask_executor(broker, allow_custom=True)
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
        fake = WindowsConsoleInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("run ask"), ENTER)
            await pump(lambda: screen.mode == "ask", message="ask mode")
            interaction = screen.interaction
            assert interaction is not None

            fake.push(console_record("\x00", VK_DOWN))
            await pump(lambda: interaction.selected == 1, message="down")
            fake.push(console_record("\x00", VK_UP))
            await pump(lambda: interaction.selected == 0, message="up")

            view = VirtualTerminal(rows=24, cols=80)
            view.feed(screen._terminal.emitted_text())
            assert view.row_index("› 1. Alice") >= 0
            assert view.violations == []

            fake.push(*[console_record(character, VK_DOWN) for character in "\x1b[B"])
            await pump(lambda: interaction.selected == 1, message="standard down")
            view = VirtualTerminal(rows=24, cols=80)
            view.feed(screen._terminal.emitted_text())
            assert view.row_index("› 2. Bob") >= 0

            fake.feed(ENTER)
            # done 状态只保留 2 秒（设计内的淡出窗口），因此同步点用稳定的
            # 「Run 结束 + 最终回复已落地」，而不是必须恰好抓到 done。
            await pump(
                lambda: (
                    screen.mode == "idle"
                    and "final" in "\n".join(screen.renderer.frame())
                ),
                message="run finished",
            )
            assert screen.status in {"done", "idle"}
            assert broker.responses[-1].answer == "Bob"
            assert calls == ["call-ask"]
            assert "final" in "\n".join(screen.renderer.frame())

            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_one_physical_navigation_record_is_handled_exactly_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        broker = RecordingBroker()
        executor, _ = make_ask_executor(broker)
        provider = ScriptedProvider(
            [[Finish(tool_calls=(ToolCall("call-ask", "ask", {"question": "Who?"}),))]]
        )
        app = make_app(tmp_path, provider, tool_executor=executor)
        fake = WindowsConsoleInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("run ask"), ENTER)
            await pump(lambda: screen.mode == "ask", message="ask mode")
            interaction = screen.interaction
            assert interaction is not None
            routed: list[str] = []
            original = interaction.handle

            def counting_handle(key: Any) -> Any:
                routed.append(key.name)
                return original(key)

            interaction.handle = counting_handle  # type: ignore[method-assign]
            fake.push(console_record("\x00", VK_DOWN))
            await pump(lambda: interaction.selected == 1, message="down")
            await asyncio.sleep(0)
            assert routed == ["down"]
            assert interaction.selected == 1

            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_lone_escape_after_flush_cancels_the_real_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = RecordingBroker()
        executor, calls = make_ask_executor(broker)
        provider = ScriptedProvider(
            [[Finish(tool_calls=(ToolCall("call-ask", "ask", {"question": "Who?"}),))]]
        )
        app = make_app(tmp_path, provider, tool_executor=executor)
        fake = WindowsConsoleInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("run ask"), ENTER)
            await pump(lambda: screen.mode == "ask", message="ask mode")

            fake.push(console_record("\x1b"))
            assert screen.mode == "ask"  # 单独 Esc 先缓冲，不立即产生按键
            await pump(
                lambda: screen.status == "stop" and screen.mode == "idle",
                message="stop",
            )
            # Esc 只取消 Run：不伪造拒绝回答，也不产生 ask 工具结果。
            assert broker.responses == []
            assert calls == []
            history = app.history
            assert history[0].content == "run ask"
            assert "cancelled" in history[-1].content
            stored = app._session_service.load_session(app.session_id)
            assert stored.turns == ()
            assert stored.checkpoints[0].kind is RunCheckpointKind.CANCELLED

            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_lone_escape_after_flush_cancels_a_permission_without_denying(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        broker = RecordingBroker()
        executor, calls = make_permission_executor(broker)
        provider = ScriptedProvider(
            [[Finish(tool_calls=(ToolCall("call-write", "write", {}),))]]
        )
        app = make_app(tmp_path, provider, tool_executor=executor)
        fake = WindowsConsoleInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("write it"), ENTER)
            await pump(lambda: screen.mode == "permission", message="permission")

            fake.push(console_record("\x1b"))
            await pump(
                lambda: screen.status == "stop" and screen.mode == "idle",
                message="stop",
            )
            assert broker.responses == []
            assert calls == []

            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_complete_escape_sequence_is_not_split_by_the_flush(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = RecordingBroker()
        executor, _ = make_ask_executor(broker)
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
        fake = WindowsConsoleInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("run ask"), ENTER)
            await pump(lambda: screen.mode == "ask", message="ask mode")
            interaction = screen.interaction
            assert interaction is not None

            # 序列在 flush 触发前完成：先喂 ESC，再在同一轮事件循环里喂 "[B"。
            fake.push(console_record("\x1b", VK_DOWN))
            fake.push(console_record("[", VK_DOWN), console_record("B", VK_DOWN))
            await pump(lambda: interaction.selected == 1, message="standard down")
            await asyncio.sleep(0)
            assert screen.mode == "ask"
            assert screen.status == "working"

            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_pending_escape_flush_is_cancelled_at_shutdown(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = RecordingBroker()
        executor, _ = make_ask_executor(broker)
        provider = ScriptedProvider(
            [[Finish(tool_calls=(ToolCall("call-ask", "ask", {"question": "Who?"}),))]]
        )
        app = make_app(tmp_path, provider, tool_executor=executor)
        fake = WindowsConsoleInput()
        # 长延迟：flush 一定还挂起，关闭必须取消它而不是等它超时。
        screen, _ = make_screen(app, fake, escape_flush_delay=30.0)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("run ask"), ENTER)
            await pump(lambda: screen.mode == "ask", message="ask mode")
            fake.push(console_record("\x1b"))
            await pump(
                lambda: screen._flush_handle is not None, message="pending flush"
            )

            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

        assert screen.closed is True
        assert screen._flush_handle is None
        assert screen.cleanup_errors == []

    asyncio.run(scenario())


def test_custom_answer_reaches_the_real_broker_and_continues_the_run(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        broker = RecordingBroker()
        executor, calls = make_ask_executor(broker, allow_custom=True)
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
            interaction = screen.interaction
            assert interaction is not None

            fake.feed(DOWN, DOWN, ENTER)
            await pump(lambda: interaction.editing == "custom", message="custom")
            fake.feed(*typed("Carol"))
            await pump(lambda: interaction.custom == "Carol", message="draft")
            fake.feed(ENTER)
            # 同前：用「Run 结束 + 最终回复已落地」作为稳定同步点。
            await pump(
                lambda: (
                    screen.mode == "idle"
                    and "final" in "\n".join(screen.renderer.frame())
                ),
                message="run finished",
            )
            assert screen.status in {"done", "idle"}

            response = broker.responses[-1]
            assert response.answer == "Carol"
            assert response.selected_option_index is None
            assert calls == ["call-ask"]
            assert broker._pending == {}
            # 自定义回答不生成用户消息，也不启动新 Run。
            assert all(message.content != "Carol" for message in app.history)
            assert "final" in "\n".join(screen.renderer.frame())

            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_legacy_custom_answer_index_is_rejected_and_the_panel_stays(
    tmp_path: Path,
) -> None:
    """回归依据：旧的 ``selected_option_index=len(options)`` 提交会被业务侧拒绝。

    旧实现因此既提交不出去、又没有任何可见反馈；这里先复现拒绝，再证明真实
    的 ``Type something.`` 路径能通过同一个 Broker 完成闭环。
    """

    async def scenario() -> None:
        broker = RecordingBroker()
        executor, calls = make_ask_executor(broker, allow_custom=True)
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
            interaction = screen.interaction
            assert interaction is not None

            legacy = UserInputResponse(interaction.interaction_id, "Carol", 2, None)
            assert broker.respond(legacy) is False
            assert screen.mode == "ask"
            assert broker._pending != {}

            fake.feed(DOWN, DOWN, ENTER)
            await pump(lambda: interaction.editing == "custom", message="custom")
            fake.feed(*typed("Carol"), ENTER)
            # 同前：用「Run 结束 + 最终回复已落地」作为稳定同步点。
            await pump(
                lambda: (
                    screen.mode == "idle"
                    and "final" in "\n".join(screen.renderer.frame())
                ),
                message="run finished",
            )
            assert screen.status in {"done", "idle"}, (
                screen.status,
                list(screen.diagnostics),
                "\n".join(screen.renderer.frame()),
            )
            assert broker.responses[-1].answer == "Carol"
            assert broker.responses[-1].selected_option_index is None
            assert calls == ["call-ask"]
            assert "final" in "\n".join(screen.renderer.frame())

            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


def test_selection_survives_later_refresh_frames(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = RecordingBroker()
        executor, _ = make_ask_executor(broker)
        provider = ScriptedProvider(
            [[Finish(tool_calls=(ToolCall("call-ask", "ask", {"question": "Who?"}),))]]
        )
        app = make_app(tmp_path, provider, tool_executor=executor)
        fake = WindowsConsoleInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("run ask"), ENTER)
            await pump(lambda: screen.mode == "ask", message="ask mode")
            interaction = screen.interaction
            assert interaction is not None

            fake.push(console_record("\x00", VK_DOWN))
            await pump(lambda: interaction.selected == 1, message="down")
            for _ in range(4):
                screen.render()
                await asyncio.sleep(0)
            assert interaction.selected == 1
            frame = "\n".join(screen.renderer.frame())
            assert "› 2. Bob" in frame

            fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())
