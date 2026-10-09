"""主缓冲区前端集成测试：终端、Run、Ask、Permission、取消与退出。"""

from __future__ import annotations

import asyncio
import io
import json
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

from mica_agent.app import AppBusyError, AppClosedError, MicaApp
from mica_agent.cancellation import CancellationToken
from mica_agent.checkpoint_contracts import RunCheckpointKind
from mica_agent.config_contracts import ConfigConflictError, ConfigError
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
from mica_agent.session_contracts import SessionSelection
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
    从而把「产品已经正常结束」误判为超时。冻结时钟只把被观测的瞬时状态固定下来，
    不改变 Run 的执行与完成语义。
    """
    screen._clock = lambda: 0.0


async def pump(predicate: Any, *, message: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TIMEOUT
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {message}")
        await asyncio.sleep(0.005)


async def feed_and_wait_processed(
    screen: MainBufferScreen, fake_input: FakeInput, *keypresses: Any
) -> None:
    """为真实排队按键处理器中的每个输入等待其返回。"""
    processed: asyncio.Queue[None] = asyncio.Queue()
    original_handle_key = screen._handle_key

    def handle_key(key: Any) -> bool:
        result = original_handle_key(key)
        processed.put_nowait(None)
        return result

    screen._handle_key = handle_key
    try:
        fake_input.feed(*keypresses)
        for _ in keypresses:
            await asyncio.wait_for(processed.get(), timeout=TIMEOUT)
    finally:
        screen._handle_key = original_handle_key


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
            await feed_and_wait_processed(screen, fake, *typed("ignored"))
            assert screen.composer_text == "ignored"
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
            assert screen.composer_text == "x"
            frame = screen.renderer.frame()
            assert any("stop" in line for line in frame)

            fake.feed(CTRL_C)
            fake.feed(CTRL_C)
            await asyncio.wait_for(harness.task, timeout=TIMEOUT)

    asyncio.run(scenario())


# composer 物理光标即时刷新

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
            await feed_and_wait_processed(screen, fake, CTRL_C)
            assert screen.mode == "idle"
            assert harness.task.done() is False

            clock_value[0] = 10.0
            await feed_and_wait_processed(screen, fake, CTRL_C)
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


# Windows VT 零字符导航后备与 Esc flush


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


@pytest.mark.parametrize("size", [(100, 35), (60, 20), (140, 46)])
def test_unconfigured_input_bytes_preserve_draft_cursor_and_create_no_run(
    tmp_path, size
):
    import json
    import os

    from test_tui_main_buffer_vt import VirtualTerminal

    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    workspace = resolve_workspace(workspace_path)
    service = SessionService(JsonlSessionStore(workspace, tmp_path / "data"))
    stored = service.create_session()
    app = MicaApp(
        None,
        None,
        session_service=service,
        session_id=stored.meta.session_id,
        context_builder=ContextBuilder(
            data_root=tmp_path / "data", workspace=workspace
        ),
        connection_diagnostic="No model configured. Configure models.json, auth.json and config.json, then restart.",
    )

    class ByteInput(FakeInput):
        def __init__(self):
            super().__init__()
            self.parser = Vt100Parser(lambda key: self._pending.append(key))

        def send_bytes(self, data):
            self.parser.feed(data)
            if self._callback:
                self._callback()

    async def scenario():
        fake = ByteInput()
        screen, _ = make_screen(app, fake, size=size)
        screen.update_session_snapshot(
            TuiSessionSnapshot(
                workspace="~/workspace",
                session_label="session-title",
                model="unconfigured",
            )
        )
        frames = []

        def capture(label):
            vt = VirtualTerminal(rows=size[1], cols=size[0])
            vt.feed(screen._terminal.emitted_text())
            assert not vt.violations
            frames.append(
                {"label": label, "lines": vt.screen(), "cursor": vt.cursor_cell()}
            )
            return vt

        original = service.store.path_for_session(stored.meta.session_id).read_bytes()
        async with ScreenHarness(screen, fake):
            startup = capture("startup")
            assert "Mica is ready." not in "\n".join(startup.all_lines())
            fake.send_bytes("keep draft")
            await pump(lambda: screen.submit_text == "keep draft")
            fake.send_bytes("\x1b[D\x1b[D")
            await pump(lambda: screen.editor.cursor == 8)
            before = capture("draft")
            fake.send_bytes("\r")
            await pump(lambda: "submit rejected" in screen.diagnostics)
            after = capture("submit-rejected")
            assert screen.submit_text == "keep draft" and screen.editor.cursor == 8
            assert after.cursor_cell() == before.cursor_cell()
            assert screen.mode == "idle" and screen.run_id is None
            assert screen._runtime.active_task is None and not app.is_busy
            assert service.load_session(stored.meta.session_id).entries == ()
            assert (
                service.store.path_for_session(stored.meta.session_id).read_bytes()
                == original
            )
            fake.send_bytes("!")
            await pump(lambda: screen.submit_text == "keep dra!ft")
            capture("editable-after-rejection")
            fake.send_bytes("\x03\x03")
            await pump(lambda: screen.closed)
        evidence = os.environ.get("MICA_TEST_EVIDENCE_DIR")
        if evidence:
            target = Path(evidence)
            target.mkdir(parents=True, exist_ok=True)
            (target / f"unconfigured-{size[0]}x{size[1]}.ansi").write_text(
                screen._terminal.emitted_text(), encoding="utf-8"
            )
            (target / f"unconfigured-{size[0]}x{size[1]}.json").write_text(
                json.dumps(frames, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    asyncio.run(scenario())


CTRL_S = KeyPress(Keys.ControlS, "\x13")
BACKSPACE = KeyPress(Keys.ControlH, "\x08")
MODEL_URL = "https://example.test/v1"
MODEL_KEY = "TEST_ONLY_SECRET"
MODEL_PRIVATE_ERROR = f"private model error {MODEL_URL} {MODEL_KEY} token-value"


def assert_model_display_safe(screen):
    displayed = screen._terminal.emitted_text()
    for private in (MODEL_URL, MODEL_KEY, MODEL_PRIVATE_ERROR, "token-value"):
        assert private not in displayed


def model_screen_app(tmp_path, *, old=None, factory=None):
    from test_config_service import make_service

    configuration = make_service(tmp_path)
    root = configuration.store.data_root
    (root / "config.json").write_text(
        '{"defaultModel":{"provider":"local","model":"a"},"reasoningEffort":"high"}'
    )
    directory_path = root / "models.json"
    directory = json.loads(directory_path.read_text())
    directory["providers"]["local"]["name"] = "Provider A"
    directory["providers"]["local"]["models"][0]["name"] = "Alpha"
    directory["providers"]["local"]["models"][1]["name"] = "Beta"
    directory_path.write_text(json.dumps(directory))
    snapshot = configuration.load()
    current = SessionSelection("local", "a")
    connection = configuration.resolve(snapshot, explicit=current).connection
    service = SessionService(
        JsonlSessionStore(resolve_workspace(configuration.store.workspace), root)
    )
    stored = service.create_session(selection=current)
    old = old or ScriptedProvider([[ProviderTextDelta("old answer"), Finish()]])
    created = []

    def make_provider(url, key):
        assert (url, key) == (MODEL_URL, MODEL_KEY)
        provider = ScriptedProvider([[ProviderTextDelta("new answer"), Finish()]])
        created.append(provider)
        return provider

    app = MicaApp(
        old,
        "a",
        session_service=service,
        session_id=stored.meta.session_id,
        context_builder=ContextBuilder(
            data_root=root, workspace=service.store.workspace
        ),
        connection=connection,
        config_service=configuration,
        startup_settings=snapshot.settings,
        provider_factory=factory or make_provider,
        close_timeout=0.05,
    )
    return app, configuration, service, old, created


async def open_model_menu(screen, fake):
    await feed_and_wait_processed(screen, fake, *typed("/m"), ENTER)
    await pump(lambda: screen.mode == "menu", message="model menu")


def test_command_candidate_keyboard_position_dismissal_and_execution(tmp_path):
    async def scenario():
        from test_tui_main_buffer_vt import VirtualTerminal

        provider = ScriptedProvider([[Finish()]])
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake):
            await feed_and_wait_processed(screen, fake, *typed("/"))
            before = screen.editor.cursor
            await feed_and_wait_processed(screen, fake, DOWN, UP)
            assert screen.editor.cursor == before
            frame = screen.renderer.frame()
            composer_bottom = next(
                index for index, line in enumerate(frame) if line.startswith("└")
            )
            candidate = next(
                index for index, line in enumerate(frame) if "› /model" in line
            )
            facts = next(
                index for index, line in enumerate(frame) if "~/workspace" in line
            )
            assert composer_bottom < candidate < facts
            assert (
                frame[candidate].strip()
                == "› /model    Switch model or set global default"
            )
            view = VirtualTerminal(rows=24, cols=80)
            view.feed(screen._terminal.emitted_text())
            assert view.row_index("/model") > view.row_index("└")
            assert view.row_index("/model") < view.row_index("~/workspace")
            await feed_and_wait_processed(screen, fake, *typed("m"), ESCAPE)
            assert screen.submit_text == "/m" and not screen._candidates.visible
            await feed_and_wait_processed(screen, fake, LEFT, RIGHT)
            assert not screen._candidates.visible
            await feed_and_wait_processed(screen, fake, *typed("x"))
            assert not screen._candidates.visible
            await feed_and_wait_processed(screen, fake, BACKSPACE)
            assert screen._candidates.visible and screen.submit_text == "/m"
            await feed_and_wait_processed(screen, fake, NEWLINE)
            assert not screen._candidates.visible
            await feed_and_wait_processed(screen, fake, BACKSPACE, ENTER)
            assert screen.mode == "menu" and screen.submit_text == ""
            assert "No models available" in "\n".join(screen.renderer.frame())
            assert provider.requests == [] and app.history == ()
            assert not any("/m" in line for line in screen.renderer.document_lines())
            assert_model_display_safe(screen)
            await feed_and_wait_processed(screen, fake, ESCAPE)
            assert screen.mode == "idle" and not screen.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "original", ["/xyz 你好", "/model\nfollowup", "/model\n", " /model "]
)
def test_unmatched_slash_submission_preserves_full_message(tmp_path, original):
    async def scenario():
        provider = ScriptedProvider([[ProviderTextDelta("answer"), Finish()]])
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        freeze_done_window(screen)
        async with ScreenHarness(screen, fake):
            await feed_and_wait_processed(
                screen, fake, KeyPress(Keys.BracketedPaste, original), ENTER
            )
            await pump(lambda: screen.mode == "idle" and screen.status == "done")
            assert provider.requests[0].messages[-1].content == original
            assert app.history[0].content == original
            assert len(provider.requests) == 1 and screen._menu is None

    asyncio.run(scenario())


def test_running_command_notice_expires_and_editable_draft_keeps_cursor(tmp_path):
    async def scenario():
        from mica_agent.tui.main_buffer.screen import BUSY_COMMAND_NOTICE

        provider = ScriptedProvider(
            [[ProviderTextDelta("answer"), Finish()]], block_after_text=True
        )
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        now = [0.0]
        ticks = asyncio.Queue()
        screen._clock = lambda: now[0]

        async def tick_sleep(interval):
            await ticks.get()

        screen._sleep = tick_sleep
        async with ScreenHarness(screen, fake) as harness:
            await feed_and_wait_processed(screen, fake, *typed("first"), ENTER)
            await asyncio.wait_for(provider.started.wait(), TIMEOUT)
            await feed_and_wait_processed(
                screen, fake, KeyPress(Keys.BracketedPaste, " /MODEL "), ENTER
            )
            assert screen.submit_text == " /MODEL " and len(provider.requests) == 1
            frame = screen.renderer.frame()
            assert BUSY_COMMAND_NOTICE in "\n".join(frame)
            assert not any("› /model" in line for line in frame)
            assert BUSY_COMMAND_NOTICE not in "\n".join(
                screen.renderer.document_lines()
            )
            now[0] = 3.0
            ticks.put_nowait(None)
            await pump(
                lambda: BUSY_COMMAND_NOTICE not in "\n".join(screen.renderer.frame())
            )
            await feed_and_wait_processed(
                screen,
                fake,
                HOME,
                *([DELETE] * len(" /MODEL ")),
                *typed("next"),
                LEFT,
                BACKSPACE,
                KeyPress(Keys.BracketedPaste, "Q\nR"),
                ENTER,
                KeyPress(Keys.ControlD, "\x04"),
            )
            draft, cursor = screen.submit_text, screen.editor.cursor
            assert draft == "neQ\nRt"
            assert not harness.task.done() and len(provider.requests) == 1
            assert BUSY_COMMAND_NOTICE not in "\n".join(screen.renderer.frame())
            provider.release.set()
            await pump(lambda: screen.mode == "idle" and screen.status == "done")
            assert screen.submit_text == draft and screen.editor.cursor == cursor
            assert len(provider.requests) == 1
            assert_model_display_safe(screen)

    asyncio.run(scenario())


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_menu_switch_updates_real_app_facts_history_and_next_provider(
    tmp_path, cleanup_failure
):
    async def scenario():
        class ClosingProvider(ScriptedProvider):
            async def aclose(self):
                self.aclose_calls += 1
                if cleanup_failure:
                    raise RuntimeError(MODEL_PRIVATE_ERROR)

        old = ClosingProvider([[ProviderTextDelta("old answer"), Finish()]])
        app, _, service, old, created = model_screen_app(tmp_path, old=old)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        screen.update_session_snapshot(
            TuiSessionSnapshot(
                workspace="~/workspace",
                session_label="session-title",
                model="a (global)",
            )
        )
        freeze_done_window(screen)
        async with ScreenHarness(screen, fake):
            await feed_and_wait_processed(screen, fake, *typed("before"), ENTER)
            await pump(lambda: screen.mode == "idle" and screen.status == "done")
            history = app.history
            await open_model_menu(screen, fake)
            assert "old answer" in "\n".join(screen.renderer.document_lines())
            assert "Type your request" not in "\n".join(screen.renderer.chrome)
            assert "sessions:" not in "\n".join(screen.renderer.chrome)
            await feed_and_wait_processed(screen, fake, DOWN, ENTER)
            await pump(lambda: screen.mode == "idle", message="switch complete")
            assert app.current_selection == SessionSelection("local", "b")
            assert (
                service.load_session(app.session_id).meta.selection
                == app.current_selection
            )
            assert app.history == history
            assert "b (session)" in "\n".join(screen.renderer.frame())
            document = "\n".join(screen.renderer.document_lines())
            assert "Source: session." in document
            assert "Reasoning: off (preference high" in document
            assert app.reasoning_notice in "\n".join(screen.renderer.chrome)
            if cleanup_failure:
                assert "provider release failed" in document
            assert old.aclose_calls == 1
            await feed_and_wait_processed(screen, fake, *typed("after"), ENTER)
            await pump(lambda: screen.mode == "idle" and screen.status == "done")
            assert len(created) == 1 and created[0].requests[0].model == "b"
            assert len(old.requests) == 1
            assert_model_display_safe(screen)
        assert created[0].aclose_calls == 1

    asyncio.run(scenario())


def test_failed_menu_switch_retains_menu_old_connection_and_safe_reason(tmp_path):
    def fail_factory(url, key):
        raise RuntimeError(MODEL_PRIVATE_ERROR)

    async def scenario():
        app, _, service, old, _ = model_screen_app(tmp_path, factory=fail_factory)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        original = service.store.path_for_session(app.session_id).read_bytes()
        async with ScreenHarness(screen, fake):
            await open_model_menu(screen, fake)
            await feed_and_wait_processed(screen, fake, DOWN, ENTER)
            await pump(
                lambda: (
                    "unable to initialize provider" in "\n".join(screen.renderer.chrome)
                )
            )
            assert screen.mode == "menu" and app.current_selection == SessionSelection(
                "local", "a"
            )
            assert (
                service.store.path_for_session(app.session_id).read_bytes() == original
            )
            assert old.aclose_calls == 0
            assert_model_display_safe(screen)
            await feed_and_wait_processed(
                screen, fake, ESCAPE, *typed("still works"), ENTER
            )
            await pump(lambda: bool(old.requests))
            assert old.requests[0].model == "a"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error,hint",
    [
        (AppBusyError, "The app is busy. Try again after the current task finishes."),
        (AppClosedError, "The app is closed."),
        (RuntimeError, "Unable to switch model."),
    ],
)
def test_switch_exceptions_are_classified_without_raw_text(
    tmp_path, monkeypatch, error, hint
):
    async def scenario():
        app, _, _, _, _ = model_screen_app(tmp_path)

        async def fail(selection):
            raise error(MODEL_PRIVATE_ERROR)

        monkeypatch.setattr(app, "switch_model", fail)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake):
            await open_model_menu(screen, fake)
            await feed_and_wait_processed(screen, fake, DOWN, ENTER)
            await pump(lambda: hint in "\n".join(screen.renderer.chrome))
            assert screen.mode == "menu" and app.current_selection == SessionSelection(
                "local", "a"
            )
            assert_model_display_safe(screen)

    asyncio.run(scenario())


def test_ctrl_s_saves_global_default_refreshes_markers_without_switch(tmp_path):
    async def scenario():
        app, configuration, service, old, created = model_screen_app(tmp_path)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        original = service.store.path_for_session(app.session_id).read_bytes()
        async with ScreenHarness(screen, fake):
            await open_model_menu(screen, fake)
            await feed_and_wait_processed(screen, fake, DOWN, CTRL_S)
            frame = "\n".join(screen.renderer.chrome)
            assert "Saved as global default." in frame
            assert screen.mode == "menu" and app.current_selection == SessionSelection(
                "local", "a"
            )
            assert created == [] and old.aclose_calls == 0
            assert json.loads(
                (configuration.store.data_root / "config.json").read_text()
            )["defaultModel"] == {"provider": "local", "model": "b"}
            assert (
                service.store.path_for_session(app.session_id).read_bytes() == original
            )
            alpha = next(line for line in screen.renderer.chrome if "Alpha" in line)
            beta = next(line for line in screen.renderer.chrome if "Beta" in line)
            assert "Current" in alpha and "Default" not in alpha
            assert "›" in beta and "Default" in beta and "Current" not in beta
            assert_model_display_safe(screen)
            await feed_and_wait_processed(screen, fake, ESCAPE)
            assert screen.mode == "idle" and not screen.closed
            assert configuration.load().default_model == SessionSelection("local", "b")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure,hint",
    [
        (
            "conflict",
            "Default changed elsewhere. Please reopen the menu and try again.",
        ),
        ("config", "Unable to save global default."),
        ("raw", "Unable to save global default."),
        ("refresh", "Default saved, but unable to refresh models. Reopen the menu."),
    ],
)
def test_default_errors_and_committed_save_refresh_error_preserve_visible_markers(
    tmp_path, monkeypatch, failure, hint
):
    async def scenario():
        app, configuration, _, _, created = model_screen_app(tmp_path)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake):
            await open_model_menu(screen, fake)
            await feed_and_wait_processed(screen, fake, DOWN)
            before = tuple(
                (item.selection, item.is_default) for item in screen._menu.choices
            )
            if failure == "refresh":

                def fail_read():
                    raise ConfigError(MODEL_PRIVATE_ERROR)

                monkeypatch.setattr(app, "model_choices", fail_read)
            else:

                def fail_save(selection):
                    exception = (
                        ConfigConflictError
                        if failure == "conflict"
                        else ConfigError
                        if failure == "config"
                        else RuntimeError
                    )
                    raise exception(MODEL_PRIVATE_ERROR)

                monkeypatch.setattr(app, "save_default_model", fail_save)
            await feed_and_wait_processed(screen, fake, CTRL_S)
            assert hint in "\n".join(screen.renderer.chrome)
            assert screen.mode == "menu" and app.current_selection == SessionSelection(
                "local", "a"
            )
            assert created == []
            assert before == tuple(
                (item.selection, item.is_default) for item in screen._menu.choices
            )
            default = configuration.load().default_model
            assert default == SessionSelection(
                "local", "b" if failure == "refresh" else "a"
            )
            assert_model_display_safe(screen)

    asyncio.run(scenario())


def test_directory_load_error_is_safe_and_empty_menu_only_closes(tmp_path, monkeypatch):
    async def scenario():
        provider = ScriptedProvider([[Finish()]])
        app = make_app(tmp_path, provider)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        freeze_done_window(screen)
        async with ScreenHarness(screen, fake):
            original = app.model_choices

            def fail_read():
                raise ConfigError(MODEL_PRIVATE_ERROR)

            monkeypatch.setattr(app, "model_choices", fail_read)
            await feed_and_wait_processed(screen, fake, *typed("/model"), ENTER)
            assert screen.mode == "idle" and screen._menu is None
            assert "Unable to load models." in "\n".join(screen.renderer.chrome)
            assert provider.requests == []
            assert_model_display_safe(screen)
            monkeypatch.setattr(app, "model_choices", original)
            await open_model_menu(screen, fake)
            await feed_and_wait_processed(
                screen, fake, DOWN, *typed("ignored"), ENTER, CTRL_S
            )
            assert screen.mode == "menu" and "No models available" in "\n".join(
                screen.renderer.chrome
            )
            assert screen._menu.filter_text == "" and provider.requests == []
            await feed_and_wait_processed(screen, fake, ESCAPE)
            assert (
                screen.mode == "idle" and screen.submit_text == "" and not screen.closed
            )
            assert app.history == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_during_switch", [False, "menu", "closed"])
@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_switching_gates_input_ignores_stale_menu_result_and_cleans_exit(
    tmp_path, exit_during_switch, cleanup_failure
):
    async def scenario():
        from mica_agent.app import ModelSwitchResult
        from mica_agent.tui.main_buffer.renderer import Palette, strip_ansi
        from mica_agent.tui.main_buffer.screen import BUSY_COMMAND_NOTICE

        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class WaitingCloseProvider(ScriptedProvider):
            async def aclose(self):
                self.aclose_calls += 1
                started.set()
                try:
                    await release.wait()
                    if cleanup_failure:
                        raise RuntimeError(MODEL_PRIVATE_ERROR)
                finally:
                    finished.set()

        old = WaitingCloseProvider([[Finish()]])
        app, _, service, old, created = model_screen_app(tmp_path, old=old)
        app._close_timeout = 30.0
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        screen.update_session_snapshot(
            TuiSessionSnapshot(workspace="~/workspace", model="a (global)")
        )
        freeze_done_window(screen)
        async with ScreenHarness(screen, fake) as harness:
            screen._palette = Palette(True)
            await open_model_menu(screen, fake)
            await feed_and_wait_processed(screen, fake, DOWN, ENTER)
            await asyncio.wait_for(started.wait(), TIMEOUT)
            original_menu = screen._menu
            request = screen._switch_request
            selected = original_menu.selected.selection
            await feed_and_wait_processed(
                screen, fake, UP, ENTER, *typed("ignored"), CTRL_S
            )
            assert (
                screen._menu is original_menu
                and original_menu.selected.selection == selected
            )
            assert original_menu.filter_text == "" and len(created) == 1
            assert "Switching..." in "\n".join(screen.renderer.chrome)
            assert service.load_session(
                app.session_id
            ).meta.selection == SessionSelection("local", "b")
            if exit_during_switch == "menu":
                await feed_and_wait_processed(screen, fake, CTRL_C, CTRL_C)
                await asyncio.wait_for(harness.task, TIMEOUT)
                assert screen.closed and screen.mode == "closed"
                assert (
                    finished.is_set()
                    and old.aclose_calls == created[0].aclose_calls == 1
                )
                assert not screen._switch_tasks
                assert screen.cleanup_errors in ([], ["model switch cleanup failed"])
            else:
                await feed_and_wait_processed(screen, fake, ESCAPE)
                assert screen.mode == "switching" and screen._menu is None
                assert screen._switch_request is request and app.is_busy
                await feed_and_wait_processed(
                    screen, fake, ESCAPE, KeyPress(Keys.ControlD, "\x04")
                )
                assert screen.mode == "switching" and not harness.task.done()
                expanded = screen.renderer.expanded
                await feed_and_wait_processed(
                    screen, fake, KeyPress(Keys.ControlO, "\x0f")
                )
                assert screen.renderer.expanded is not expanded
                await feed_and_wait_processed(
                    screen, fake, KeyPress(Keys.ControlO, "\x0f")
                )
                assert screen.renderer.expanded is expanded
                document = screen.renderer.document_lines()
                screen._queue.put_nowait(
                    (
                        "model_switch",
                        (original_menu, object(), ModelSwitchResult(True, ""), None),
                    )
                )
                await feed_and_wait_processed(
                    screen, fake, KeyPress(Keys.BracketedPaste, " /MODEL "), ENTER
                )
                assert BUSY_COMMAND_NOTICE in "\n".join(screen.renderer.chrome)
                assert screen.submit_text == " /MODEL "
                assert screen._menu is None and screen._switch_request is request
                assert "› /model" not in "\n".join(screen.renderer.chrome)
                await feed_and_wait_processed(
                    screen,
                    fake,
                    HOME,
                    *([DELETE] * len(" /MODEL ")),
                    *typed("next request"),
                    LEFT,
                    ENTER,
                    CTRL_S,
                )
                draft, cursor = screen.submit_text, screen.editor.cursor
                assert draft == "next request" and cursor == len(draft) - 1
                assert app.history == () and old.requests == created[0].requests == []
                assert screen.renderer.document_lines() == document
                assert len(created) == 1 and screen.mode == "switching"
                body = next(
                    row for row in screen.renderer.chrome if "next request" in row
                )
                assert body == Palette(True).paint(strip_ansi(body), "muted")
                if exit_during_switch == "closed":
                    await feed_and_wait_processed(screen, fake, CTRL_C, CTRL_C)
                    await asyncio.wait_for(harness.task, TIMEOUT)
                    assert screen.closed and screen.mode == "closed"
                    assert finished.is_set()
                    assert old.aclose_calls == created[0].aclose_calls == 1
                    assert not screen._switch_tasks
                    assert screen.cleanup_errors in (
                        [],
                        ["model switch cleanup failed"],
                    )
                    assert screen.renderer.document_lines() == document
                    assert_model_display_safe(screen)
                    return
                release.set()
                await pump(
                    lambda: screen.mode == "idle",
                    message="closed-menu switch processed",
                )
                assert finished.is_set() and not app.is_busy
                assert screen.submit_text == draft and screen.editor.cursor == cursor
                assert app.history == () and created[0].requests == []
                body = next(
                    row for row in screen.renderer.chrome if "next request" in row
                )
                assert body == Palette(True).paint(strip_ansi(body), "composer")
                assert screen._session_snapshot.model == "b (session)"
                assert "b (session)" in "\n".join(screen.renderer.chrome)
                assert screen._configuration_notice == app.reasoning_notice
                assert app.reasoning_notice in "\n".join(screen.renderer.chrome)
                completed = screen.renderer.document_lines()
                assert "\n".join(completed).count("Source: session.") == 1
                assert "\n".join(completed).count("provider release failed") == int(
                    cleanup_failure
                )
                assert old.aclose_calls == 1 and len(created) == 1
                await feed_and_wait_processed(screen, fake, ENTER)
                await pump(lambda: screen.mode == "idle" and screen.status == "done")
                assert len(created[0].requests) == 1
                assert created[0].requests[0].messages[-1].content == draft
                assert created[0].requests[0].model == "b" and old.requests == []
                await open_model_menu(screen, fake)
                new_menu = screen._menu
                assert new_menu is not original_menu
                assert new_menu.current.selection == app.current_selection
                assert new_menu.selected.selection == SessionSelection("local", "b")
                completed = screen.renderer.document_lines()
                screen._queue.put_nowait(
                    (
                        "model_switch",
                        (original_menu, request, ModelSwitchResult(True, ""), None),
                    )
                )
                await feed_and_wait_processed(screen, fake, KeyPress(Keys.F1, ""))
                assert screen._menu is new_menu and screen.mode == "menu"
                assert screen.renderer.document_lines() == completed
                assert screen._session_snapshot.model == "b (session)"
            assert_model_display_safe(screen)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure,hint",
    [
        ("business", "unable to initialize provider"),
        (AppBusyError, "The app is busy. Try again after the current task finishes."),
        (AppClosedError, "The app is closed."),
        (RuntimeError, "Unable to switch model."),
    ],
)
def test_closed_menu_switch_failure_recovers_draft_and_safe_submission(
    tmp_path, monkeypatch, failure, hint
):
    def fail_factory(url, key):
        raise RuntimeError(MODEL_PRIVATE_ERROR)

    async def scenario():
        app, _, service, old, created = model_screen_app(tmp_path, factory=fail_factory)
        started, release = asyncio.Event(), asyncio.Event()
        switch = app.switch_model

        async def delayed_switch(selection):
            started.set()
            await release.wait()
            if failure == "business":
                return await switch(selection)
            raise failure(MODEL_PRIVATE_ERROR)

        monkeypatch.setattr(app, "switch_model", delayed_switch)
        original = service.store.path_for_session(app.session_id).read_bytes()
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        screen._clock = lambda: 0.0
        freeze_done_window(screen)
        async with ScreenHarness(screen, fake):
            await open_model_menu(screen, fake)
            await feed_and_wait_processed(screen, fake, DOWN, ENTER)
            await asyncio.wait_for(started.wait(), TIMEOUT)
            document = screen.renderer.document_lines()
            await feed_and_wait_processed(
                screen, fake, ESCAPE, *typed("retry"), LEFT, ENTER
            )
            assert screen.mode == "switching" and screen._menu is None
            assert screen.submit_text == "retry" and screen.editor.cursor == 4
            assert old.requests == created == [] and app.history == ()
            release.set()
            await pump(
                lambda: screen.mode == "idle", message="switch failure processed"
            )
            assert hint in "\n".join(screen.renderer.chrome)
            assert screen._switch_request is None and not screen._switch_tasks
            assert screen.submit_text == "retry" and screen.editor.cursor == 4
            assert app.current_selection == SessionSelection("local", "a")
            assert (
                service.store.path_for_session(app.session_id).read_bytes() == original
            )
            assert old.aclose_calls == 0
            assert screen.renderer.document_lines() == document
            assert_model_display_safe(screen)
            await feed_and_wait_processed(screen, fake, ENTER)
            await pump(lambda: screen.mode == "idle" and screen.status == "done")
            assert len(old.requests) == 1 and old.requests[0].model == "a"
            assert old.requests[0].messages[-1].content == "retry"
            assert_model_display_safe(screen)

    asyncio.run(scenario())


def test_valid_switch_result_updates_facts_without_mutating_another_menu(
    tmp_path, monkeypatch
):
    async def scenario():
        from mica_agent.app import ModelSwitchResult
        from mica_agent.tui.main_buffer.menus import ModelMenu

        app, _, _, _, created = model_screen_app(tmp_path)
        started, release = asyncio.Event(), asyncio.Event()
        switch = app.switch_model

        async def delayed_switch(selection):
            started.set()
            await release.wait()
            return await switch(selection)

        monkeypatch.setattr(app, "switch_model", delayed_switch)
        fake = FakeInput()
        screen, _ = make_screen(app, fake)
        async with ScreenHarness(screen, fake):
            await open_model_menu(screen, fake)
            original_menu = screen._menu
            await feed_and_wait_processed(screen, fake, DOWN, ENTER)
            await asyncio.wait_for(started.wait(), TIMEOUT)
            request = screen._switch_request
            await feed_and_wait_processed(screen, fake, ESCAPE)
            # Deliberately replace only the presentation to exercise lifecycle isolation.
            other_menu = ModelMenu(app.model_choices())
            other_menu.set_hint("Other menu result.")
            screen._menu = other_menu
            screen._mode = "menu"
            choices = other_menu.choices
            release.set()
            await pump(
                lambda: screen._switch_request is None, message="valid result processed"
            )
            assert screen._menu is other_menu and screen.mode == "menu"
            assert other_menu.choices == choices
            assert "Other menu result." in "\n".join(screen.renderer.chrome)
            assert screen._session_snapshot.model == "b (session)"
            assert screen._configuration_notice == app.reasoning_notice
            document = screen.renderer.document_lines()
            assert "\n".join(document).count("Source: session.") == 1
            screen._queue.put_nowait(
                (
                    "model_switch",
                    (original_menu, request, ModelSwitchResult(True, ""), None),
                )
            )
            await feed_and_wait_processed(screen, fake, KeyPress(Keys.F1, ""))
            assert screen._menu is other_menu and screen.mode == "menu"
            assert screen.renderer.document_lines() == document
            assert_model_display_safe(screen)
        assert created[0].aclose_calls == 1
        # The shutdown guard also rejects late direct delivery of the previous request.
        screen._finish_model_switch(
            original_menu, request, ModelSwitchResult(True, ""), None
        )
        assert screen.renderer.document_lines() == document
        assert screen.mode == "closed" and screen._menu is None

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(100, 30), (40, 14), (24, 10)])
def test_model_menu_vt_replay_and_visual_evidence(tmp_path, size):
    import os

    from test_tui_main_buffer_vt import VirtualTerminal

    from mica_agent.tui.main_buffer.renderer import Palette, cell_len, strip_ansi

    async def scenario():
        app, configuration, _, _, created = model_screen_app(tmp_path)
        directory_path = configuration.store.data_root / "models.json"
        directory = json.loads(directory_path.read_text())
        directory["providers"]["local"]["models"][1]["contextWindow"] = 32000
        directory_path.write_text(json.dumps(directory))
        fake = FakeInput()
        terminal = Terminal(
            output=io.StringIO(),
            input_factory=lambda: fake,
            size=size,
            environ={},
            interactive=True,
        )
        screen = MainBufferScreen(
            app,
            terminal=terminal,
            session_snapshot=TuiSessionSnapshot(
                workspace="~/workspace",
                model="a (global)",
                session_label="session-title",
            ),
            clock=lambda: 0.0,
        )
        frames = []

        def capture(label):
            view = VirtualTerminal(rows=size[1], cols=size[0])
            view.feed(terminal.emitted_text())
            assert not view.violations
            assert all(
                cell_len(strip_ansi(row)) <= size[0] for row in screen.renderer.chrome
            )
            frames.append(
                {
                    "label": label,
                    "width": size[0],
                    "height": size[1],
                    "lines": view.screen(),
                    "rows": screen.renderer.physical_frame(),
                    "cursor": view.cursor_cell(),
                }
            )
            return view

        async with ScreenHarness(screen, fake):
            await feed_and_wait_processed(screen, fake, *typed("/m"))
            candidate = capture("command-candidate")
            assert candidate.row_index("/model") > candidate.row_index("└")
            assert candidate.row_index("/model") < candidate.row_index("sessions:")
            await feed_and_wait_processed(screen, fake, ENTER)
            capture("current-and-default")
            assert screen.mode == "menu"
            assert any(
                "✓" in strip_ansi(row) and "Current Default" in strip_ansi(row)
                for row in screen.renderer.chrome
            )
            await feed_and_wait_processed(screen, fake, DOWN)
            capture("navigation-is-independent")
            selected = next(
                row for row in screen.renderer.chrome if "›" in strip_ansi(row)
            )
            assert strip_ansi(selected).startswith(" ›   ")
            assert Palette(True).paint("Beta [Provider A]", "muted") in selected
            await feed_and_wait_processed(screen, fake, CTRL_S)
            capture("saved-global-default")
            assert app.current_selection == SessionSelection("local", "a")
            assert configuration.load().default_model == SessionSelection("local", "b")
            await feed_and_wait_processed(screen, fake, ENTER)
            capture("smaller-context-confirmation")
            assert screen.mode == "menu" and created == []
            await feed_and_wait_processed(screen, fake, ESCAPE)
            assert screen.mode == "menu" and created == []
            await feed_and_wait_processed(screen, fake, ENTER, ENTER)
            await pump(lambda: screen.mode == "idle", message="confirmed model switch")
            capture("model-switched")
            assert app.current_selection == SessionSelection("local", "b")
            assert_model_display_safe(screen)
        evidence = os.environ.get("MICA_TEST_EVIDENCE_DIR")
        if evidence:
            target = Path(evidence)
            target.mkdir(parents=True, exist_ok=True)
            (target / f"model-menu-{size[0]}x{size[1]}.ansi").write_text(
                terminal.emitted_text(), encoding="utf-8"
            )
            (target / f"model-menu-{size[0]}x{size[1]}.json").write_text(
                json.dumps(frames, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    asyncio.run(scenario())
