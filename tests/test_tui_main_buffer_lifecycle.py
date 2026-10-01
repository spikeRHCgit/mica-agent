"""P5-11A-MR1 §4：初始化与退出路径的资源清理。

所有权衡都用计数替身验证：进入/退出次数、关闭幂等性与「单项失败不阻断
其他清理」都可断言，不访问真实 Provider、网络或用户 HOME。
"""

from __future__ import annotations

import asyncio
import io
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from test_tui_main_buffer_screen import (
    CTRL_C,
    ENTER,
    FakeInput,
    ScriptedProvider,
    make_app,
    pump,
    typed,
)

from mica_agent.app import MicaApp
from mica_agent.providers import Finish
from mica_agent.providers import TextDelta as ProviderTextDelta
from mica_agent.tui.main_buffer import MainBufferScreen, Terminal

TIMEOUT = 5.0


class CountingApp(MicaApp):
    """记录 ``aclose`` 次数的 MicaApp 子类。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1
        await super().aclose()


class FailingOutput(io.StringIO):
    """可以在指定的第 N 次写入上失败一次的输出流。"""

    def __init__(self) -> None:
        super().__init__()
        self.writes = 0
        self.fail_after: int | None = None

    def write(self, text: str) -> int:
        self.writes += 1
        if self.fail_after is not None and self.writes == self.fail_after:
            self.fail_after = None
            raise RuntimeError("output failed")
        return super().write(text)


class BrokenExit(AbstractContextManager[None]):
    """进入成功、退出失败的上下文：验证单项清理失败不阻断其他清理。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        raise RuntimeError("raw exit failed")


class BrokenRawInput(FakeInput):
    def raw_mode(self) -> AbstractContextManager[None]:
        self.raw_mode_entries += 1
        return BrokenExit()


class StepClock:
    """每次读取都前进固定秒数的可注入时钟。"""

    def __init__(self, step: float = 0.0) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class ImmediateSleep:
    """不真正等待的 sleep：只让出事件循环，让刷新任务可以快速推进。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, delay: float) -> None:
        self.calls += 1
        await asyncio.sleep(0)


def build(
    tmp_path: Path,
    *,
    interactive: bool = True,
    factory: Any = None,
    output: Any = None,
    size: tuple[int, int] = (60, 20),
    clock: Any = None,
    sleep: Any = None,
    branch_reader: Any = None,
    workspace_path: Any = None,
    provider: Any = None,
    tool_executor: Any = None,
    session_snapshot: Any = None,
    startup_resources: Any = None,
) -> SimpleNamespace:
    from test_tui_main_buffer_screen import ScriptedProvider

    provider = ScriptedProvider([[]]) if provider is None else provider
    app = make_app(tmp_path, provider, tool_executor=tool_executor, app_cls=CountingApp)
    fake = None if factory is not None else FakeInput()
    stream = FailingOutput() if output is None else output
    terminal = Terminal(
        output=stream,
        input_factory=factory if factory is not None else (lambda: fake),
        size=size,
        environ={"NO_COLOR": "1"},
        interactive=interactive,
    )
    extra: dict[str, Any] = {}
    if clock is not None:
        extra["clock"] = clock
    if sleep is not None:
        extra["sleep"] = sleep
    if branch_reader is not None:
        extra["branch_reader"] = branch_reader
    screen = MainBufferScreen(
        app,
        terminal=terminal,
        startup_resources=startup_resources,
        session_snapshot=session_snapshot,
        workspace_path=workspace_path,
        **extra,
    )
    return SimpleNamespace(
        provider=provider,
        app=app,
        fake=fake,
        terminal=terminal,
        screen=screen,
        stream=stream,
    )


def test_non_interactive_terminal_is_refused_without_acquiring_anything(
    tmp_path: Path,
) -> None:
    fake = FakeInput()
    env = build(tmp_path, interactive=False, factory=lambda: fake)
    with pytest.raises(RuntimeError):
        asyncio.run(env.screen.run())

    assert fake.raw_mode_entries == 0
    assert fake.close_calls == 0
    assert env.terminal.emitted == []
    assert env.terminal.cleanup_errors == []
    # 构造阶段被拒绝：屏幕从未接管业务资源，兜底关闭由 CLI 负责。
    assert env.app.aclose_calls == 0
    assert env.screen.closed is False


def test_input_factory_failure_closes_business_resources(tmp_path: Path) -> None:
    def factory() -> Any:
        raise RuntimeError("no input")

    env = build(tmp_path, factory=factory)
    with pytest.raises(RuntimeError):
        asyncio.run(env.screen.run())

    assert env.app.aclose_calls == 1
    assert env.screen.closed is True
    assert env.terminal.cleanup_errors == []


def test_raw_mode_failure_closes_the_input_object(tmp_path: Path) -> None:
    fake = FakeInput(fail_raw_mode=True)
    env = build(tmp_path, factory=lambda: fake)
    with pytest.raises(RuntimeError):
        asyncio.run(env.screen.run())

    assert fake.raw_mode_entries == 0
    assert fake.close_calls == 1
    assert env.app.aclose_calls == 1


def test_attach_failure_restores_raw_mode_and_autowrap(tmp_path: Path) -> None:
    fake = FakeInput(fail_attach=True)
    env = build(tmp_path, factory=lambda: fake)
    with pytest.raises(RuntimeError):
        asyncio.run(env.screen.run())

    text = env.terminal.emitted_text()
    assert fake.raw_mode_entries == 1
    assert fake.close_calls == 1
    assert text.count("\x1b[?7l") == 1
    assert text.count("\x1b[?7h") == 1
    assert env.app.aclose_calls == 1


def test_first_frame_failure_cleans_up_and_restores_terminal(tmp_path: Path) -> None:
    fake = FakeInput()
    stream = FailingOutput()
    stream.fail_after = 2  # 第 1 次是关闭自动换行，第 2 次是首帧
    env = build(tmp_path, factory=lambda: fake, output=stream)
    with pytest.raises(RuntimeError):
        asyncio.run(env.screen.run())

    assert fake.raw_mode_entries == 1
    assert fake.close_calls == 1
    assert env.terminal.emitted_text().count("\x1b[?7h") == 1
    assert env.app.aclose_calls == 1
    assert env.terminal.cleanup_errors == []


def test_cleanup_survives_a_failing_single_step(tmp_path: Path) -> None:
    fake = BrokenRawInput()
    env = build(tmp_path, factory=lambda: fake)
    env.terminal.open_input()
    env.terminal.close_input()

    # raw mode 退出失败只记账，不阻止恢复自动换行与关闭输入对象。
    assert fake.raw_mode_entries == 1
    assert fake.close_calls == 1
    assert env.terminal.cleanup_errors == ["leave raw mode: RuntimeError"]
    assert env.terminal.emitted_text().count("\x1b[?7h") == 1
    assert env.terminal.is_open is False


def test_terminal_cleanup_is_idempotent(tmp_path: Path) -> None:
    fake = FakeInput()
    env = build(tmp_path, factory=lambda: fake)
    env.terminal.open_input()
    env.terminal.close_input()
    env.terminal.close_input()

    assert fake.raw_mode_entries == 1
    assert fake.close_calls == 1
    assert env.terminal.cleanup_errors == []
    assert env.terminal.emitted_text().count("\x1b[?7h") == 1


def test_close_input_and_shutdown_are_idempotent(tmp_path: Path) -> None:
    fake = FakeInput()
    env = build(tmp_path, factory=lambda: fake)
    env.terminal.open_input()

    async def scenario() -> None:
        await env.screen._shutdown()
        await env.screen._shutdown()

    asyncio.run(scenario())
    assert fake.close_calls == 1
    assert env.app.aclose_calls == 1


def test_run_loop_failure_propagates_and_cleans_up(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = build(tmp_path)
        task = asyncio.create_task(env.screen.run())
        await pump(lambda: env.fake.is_attached, message="attach")
        env.stream.fail_after = env.stream.writes + 1
        env.fake.feed(*typed("a"))
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(task, timeout=TIMEOUT)

        assert env.fake.close_calls == 1
        assert env.app.aclose_calls == 1
        assert env.screen.refresh_task is None
        assert env.terminal.emitted_text().count("\x1b[?7h") == 1

    asyncio.run(scenario())


def test_external_cancellation_restores_terminal_and_closes_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        env = build(tmp_path)
        task = asyncio.create_task(env.screen.run())
        await pump(lambda: env.fake.is_attached, message="attach")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=TIMEOUT)

        assert env.fake.close_calls == 1
        assert env.app.aclose_calls == 1
        assert env.terminal.emitted_text().count("\x1b[?7h") == 1

    asyncio.run(scenario())


def test_two_press_ctrl_c_exit_releases_everything_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = build(tmp_path)
        task = asyncio.create_task(env.screen.run())
        await pump(lambda: env.fake.is_attached, message="attach")
        env.fake.feed(CTRL_C, CTRL_C)
        await asyncio.wait_for(task, timeout=TIMEOUT)

        assert env.screen.closed is True
        assert env.screen.mode == "closed"
        assert env.fake.close_calls == 1
        assert env.app.aclose_calls == 1
        assert env.screen.refresh_task is None
        assert env.terminal.emitted_text().count("\x1b[?7h") == 1

    asyncio.run(scenario())


def test_clear_chrome_write_failure_still_finishes_every_cleanup(
    tmp_path: Path,
) -> None:
    """清理阶段第一次 write 失败：其余清理与最终状态仍必须完成。"""

    async def scenario() -> None:
        stream = FailingOutput()
        env = build(tmp_path, output=stream)
        task = asyncio.create_task(env.screen.run())
        await pump(lambda: env.fake.is_attached, message="attach")
        renderer = env.screen.renderer
        assert renderer is not None
        original = renderer.clear_chrome

        def clear_chrome_with_failing_write() -> None:
            # 只让 clear_chrome 自身发出的那一次 write 失败。
            stream.fail_after = stream.writes + 1
            original()

        renderer.clear_chrome = clear_chrome_with_failing_write  # type: ignore[method-assign]
        env.fake.feed(CTRL_C, CTRL_C)
        await asyncio.wait_for(task, timeout=TIMEOUT)

        assert env.screen.closed is True
        assert env.screen.mode == "closed"
        assert env.screen._shutdown_done is True
        assert env.screen.cleanup_errors == ["clear chrome: RuntimeError"]
        # 其余清理互不阻断：输入关闭、autowrap 恢复、业务资源关闭各一次。
        assert env.fake.close_calls == 1
        assert env.screen._terminal.is_open is False
        assert env.terminal.emitted_text().count(chr(27) + "[?7h") == 1
        assert env.app.aclose_calls == 1
        # 不向使用者显示 traceback 或内部标签。
        emitted = env.terminal.emitted_text()
        assert "Traceback" not in emitted
        assert "clear chrome" not in emitted
        assert "RuntimeError" not in emitted
        # 幂等：再次关闭不会重复计数。
        await env.screen._shutdown()
        assert env.fake.close_calls == 1
        assert env.app.aclose_calls == 1

    asyncio.run(scenario())


# -- §6 EOF / Ctrl+D 最小规则 -------------------------------------------------


CTRL_D = KeyPress(Keys.ControlD, chr(4))


class EofInput(FakeInput):
    """在下一次读取时抛出 EOFError，模拟输入流结束。"""

    def __init__(self) -> None:
        super().__init__()
        self.raise_eof = False

    def read_keys(self) -> list[Any]:
        if self.raise_eof:
            self.raise_eof = False
            raise EOFError
        return super().read_keys()


def test_ctrl_d_exits_cleanly_when_idle_with_an_empty_draft(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = build(tmp_path)
        provider = env.provider
        task = asyncio.create_task(env.screen.run())
        await pump(lambda: env.fake.is_attached, message="attach")
        document = env.screen.renderer.document_lines()
        assert env.screen.mode == "idle"

        env.fake.feed(CTRL_D)
        await asyncio.wait_for(task, timeout=TIMEOUT)

        assert env.screen.closed is True
        assert env.screen.mode == "closed"
        assert env.screen.status == "idle"
        assert env.fake.close_calls == 1
        assert env.app.aclose_calls == 1
        assert env.screen.refresh_task is None
        assert env.screen.run_id is None
        assert env.screen.composer_text == ""
        # 不产生用户消息，也不产生空 Run。
        assert env.screen.renderer.document_lines() == document
        assert provider.requests == []
        assert env.terminal.emitted_text().count(chr(27) + "[?7h") == 1

    asyncio.run(scenario())


def test_ctrl_d_is_ignored_with_a_draft_or_an_active_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [[ProviderTextDelta("answer"), Finish()]], block_after_text=True
        )
        env = build(tmp_path, provider=provider)
        handled = asyncio.Event()
        original_handle_key = env.screen._handle_key

        def handle_key(key: Any) -> bool:
            result = original_handle_key(key)
            if key.name == "ctrl_d":
                # 仅在真实排队按键处理器返回后通知。
                handled.set()
            return result

        env.screen._handle_key = handle_key
        task = asyncio.create_task(env.screen.run())
        await pump(lambda: env.fake.is_attached, message="attach")

        env.fake.feed(*typed("draft"))
        await pump(lambda: env.screen.composer_text == "draft")
        env.fake.feed(CTRL_D)
        await asyncio.wait_for(handled.wait(), timeout=TIMEOUT)
        assert env.screen.closed is False
        assert env.screen.composer_text == "draft"

        env.fake.feed(ENTER)
        await pump(lambda: env.screen.mode == "running", message="running")
        handled.clear()
        env.fake.feed(CTRL_D)
        await asyncio.wait_for(handled.wait(), timeout=TIMEOUT)
        assert env.screen.closed is False
        assert env.screen.mode == "running"

        provider.release.set()
        await pump(lambda: env.screen.mode == "idle", message="idle")
        env.fake.feed(CTRL_C, CTRL_C)
        await asyncio.wait_for(task, timeout=TIMEOUT)
        assert env.screen.closed is True

    asyncio.run(scenario())


def test_input_stream_eof_follows_the_same_idle_rule(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = EofInput()
        env = build(tmp_path, factory=lambda: fake)
        task = asyncio.create_task(env.screen.run())
        await pump(lambda: fake.is_attached, message="attach")

        fake.raise_eof = True
        fake.feed(CTRL_C)  # 触发一次读取回调，回调此时抛出 EOFError
        await asyncio.wait_for(task, timeout=TIMEOUT)

        assert env.screen.closed is True
        assert env.screen.mode == "closed"
        assert fake.close_calls == 1
        assert env.app.aclose_calls == 1
        assert "input read failed" not in env.screen.diagnostics

    asyncio.run(scenario())


def test_runtime_accepts_a_mica_app_subclass(tmp_path: Path) -> None:
    from mica_agent.tui.main_buffer.runtime import MainBufferRuntime

    async def sink(_event: object) -> None:
        return None

    env = build(tmp_path)
    runtime = MainBufferRuntime(env.app, event_sink=sink)
    assert isinstance(env.app, MicaApp)
    assert runtime.is_closed is False


def test_runtime_rejects_objects_that_only_look_like_mica_app() -> None:
    from mica_agent.tui.main_buffer.runtime import MainBufferRuntime

    async def sink(_event: object) -> None:
        return None

    class Impostor:
        run = object()
        aclose = object()

    with pytest.raises(TypeError, match="app must be a MicaApp"):
        MainBufferRuntime(object(), event_sink=sink)
    with pytest.raises(TypeError, match="app must be a MicaApp"):
        MainBufferRuntime(Impostor(), event_sink=sink)


def test_importing_screen_and_runtime_does_not_load_mica_app() -> None:
    import subprocess
    import sys

    code = """
import sys
from mica_agent.tui import MainBufferScreen
from mica_agent.tui.main_buffer.runtime import MainBufferRuntime
assert MainBufferScreen.__name__ == "MainBufferScreen"
assert MainBufferRuntime.__name__ == "MainBufferRuntime"
assert "mica_agent.app" not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
