"""P6-11A-MR1 §6：真实 screen 级 resize、工作状态刷新与事实刷新。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from test_tui_main_buffer_lifecycle import ImmediateSleep, StepClock, build
from test_tui_main_buffer_screen import (
    CTRL_C,
    DOWN,
    ENTER,
    ESCAPE,
    RecordingBroker,
    ScriptedProvider,
    make_ask_executor,
    pump,
    typed,
)

from mica_agent.providers import Finish
from mica_agent.providers import TextDelta as ProviderTextDelta
from mica_agent.tool_contracts import ToolCall
from mica_agent.tui.models import TuiSessionSnapshot

TIMEOUT = 5.0


def screen_frame(env: SimpleNamespace) -> str:
    renderer = env.screen.renderer
    assert renderer is not None
    return "\n".join(renderer.frame())


async def run_screen(env: SimpleNamespace) -> asyncio.Task[None]:
    task = asyncio.create_task(env.screen.run())
    await pump(lambda: env.fake.is_attached, message="attach")
    return task


async def exit_screen(env: SimpleNamespace, task: asyncio.Task[None]) -> None:
    if not task.done():
        env.fake.feed(CTRL_C, CTRL_C)
        await asyncio.wait_for(task, timeout=TIMEOUT)


def test_resize_updates_renderer_and_keeps_draft(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = build(tmp_path, size=(60, 20))
        task = await run_screen(env)
        env.fake.feed(*typed("draft"))
        await pump(lambda: env.screen.composer_text == "draft", message="draft")

        env.terminal.set_size((40, 12))
        env.fake.feed(*typed("x"))  # 任意按键触发一次真实绘制
        await pump(lambda: env.screen.renderer.width == 40, message="resized")

        assert env.screen.renderer.height == 12
        assert env.screen.composer_text == "draftx"
        assert env.screen.mode == "idle"
        assert "\x1b[?1049h" not in env.terminal.emitted_text()
        await exit_screen(env, task)

    asyncio.run(scenario())


def test_resize_during_run_keeps_mode_and_pending_tool(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider([[]])
        broker = RecordingBroker()
        executor, _ = make_ask_executor(broker)
        env = build(
            tmp_path,
            size=(60, 20),
            provider=provider,
            tool_executor=executor,
        )
        task = await run_screen(env)
        provider.scripts = [
            [Finish(tool_calls=(ToolCall("call-ask", "ask", {"question": "Who?"}),))]
        ]
        env.fake.feed(*typed("go"), ENTER)
        await pump(lambda: env.screen.mode == "ask", message="ask")

        before = env.screen.interaction
        assert before is not None
        selected = before.selected

        env.terminal.set_size((40, 12))
        env.fake.feed(*typed("x"))  # 选项模式下不会改变状态，只触发重绘
        await pump(lambda: env.screen.renderer.width == 40, message="resized")

        assert env.screen.mode == "ask"
        assert env.screen.interaction is before
        assert env.screen.interaction.selected == selected
        await exit_screen(env, task)

    asyncio.run(scenario())


def test_resize_while_running_keeps_status_and_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [[ProviderTextDelta("partial"), Finish()]], block_after_text=True
        )
        env = build(tmp_path, size=(60, 20), provider=provider)
        task = await run_screen(env)

        env.fake.feed(*typed("go"), ENTER)
        await pump(lambda: env.screen.mode == "running", message="running")
        await pump(lambda: "partial" in screen_frame(env), message="streamed")

        env.terminal.set_size((40, 12))
        env.fake.feed(*typed("ignored"))
        await pump(lambda: env.screen.renderer.width == 40, message="resized")

        assert env.screen.mode == "running"
        assert env.screen.status == "working"
        assert "partial" in screen_frame(env)

        provider.release.set()
        await pump(
            lambda: env.screen.status in {"done", "idle"} and env.screen.mode == "idle",
            message="done",
        )
        await exit_screen(env, task)

    asyncio.run(scenario())


def test_execute_escape_cancel_then_exit(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [[ProviderTextDelta("partial"), Finish()]], block_after_text=True
        )
        env = build(tmp_path, provider=provider)
        task = await run_screen(env)
        env.fake.feed(*typed("go"), ENTER)
        await pump(lambda: env.screen.mode == "running", message="running")
        env.fake.feed(ESCAPE)
        await pump(lambda: env.screen.status == "stop", message="stop")
        assert "Mica · stop" in screen_frame(env)
        provider.release.set()
        await exit_screen(env, task)

    asyncio.run(scenario())


def test_working_animation_uses_a_single_refresh_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [[ProviderTextDelta("partial"), Finish()]], block_after_text=True
        )
        env = build(
            tmp_path,
            provider=provider,
            clock=StepClock(0.5),
            sleep=ImmediateSleep(),
        )
        task = await run_screen(env)
        env.fake.feed(*typed("go"), ENTER)
        await pump(lambda: env.screen.mode == "running", message="running")

        task_ids: set[int] = set()
        variants: set[str] = set()
        for _ in range(60):
            await asyncio.sleep(0)
            refresh = env.screen.refresh_task
            if refresh is not None:
                task_ids.add(id(refresh))
            variants.update(
                line for line in env.screen.renderer.chrome if "working" in line
            )
        assert len(task_ids) == 1
        assert len(variants) >= 2

        provider.release.set()
        await pump(lambda: env.screen.mode == "idle", message="idle")
        await exit_screen(env, task)
        assert env.screen.refresh_task is None

    asyncio.run(scenario())


def test_done_expires_after_two_seconds_and_stop_persists(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = build(
            tmp_path,
            provider=ScriptedProvider([[Finish()]]),
            clock=StepClock(0.5),
            sleep=ImmediateSleep(),
        )
        task = await run_screen(env)
        env.fake.feed(*typed("go"), ENTER)
        await pump(lambda: env.screen.status == "done", message="done")
        assert "Mica · done(" in screen_frame(env)

        await pump(lambda: env.screen.status == "idle", message="expired")
        assert "Mica · done(" not in screen_frame(env)
        assert env.screen.refresh_task is None or env.screen.refresh_task.done()
        await exit_screen(env, task)

    asyncio.run(scenario())


def test_stop_status_persists_until_the_next_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [[ProviderTextDelta("partial"), Finish()]], block_after_text=True
        )
        env = build(
            tmp_path,
            provider=provider,
            clock=StepClock(0.5),
            sleep=ImmediateSleep(),
        )
        task = await run_screen(env)
        env.fake.feed(*typed("go"), ENTER)
        await pump(lambda: env.screen.mode == "running", message="running")
        provider.release.set()
        env.fake.feed(ESCAPE)
        await pump(lambda: env.screen.status == "stop", message="stop")

        for _ in range(20):
            await asyncio.sleep(0)
            assert env.screen.status == "stop"
        assert "Mica · stop" in screen_frame(env)
        await exit_screen(env, task)

    asyncio.run(scenario())


def test_facts_refresh_uses_public_seams(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = build(
            tmp_path,
            provider=ScriptedProvider([[Finish()]]),
            workspace_path=tmp_path,
            branch_reader=lambda path: "feature/x",
            session_snapshot=TuiSessionSnapshot(
                workspace="/ws", session_label="first", model="model-a"
            ),
        )
        task = await run_screen(env)
        env.fake.feed(*typed("go"), ENTER)
        await pump(lambda: env.screen.status == "done", message="done")
        await pump(lambda: env.screen.branch == "feature/x", message="branch")

        frame = screen_frame(env)
        assert "feature/x" in frame
        assert "model-a" in frame

        env.screen.update_session_snapshot(
            TuiSessionSnapshot(
                workspace="/ws", session_label="renamed", model="model-b"
            )
        )
        updated = screen_frame(env)
        assert "model-b" in updated
        assert "renamed" in updated
        assert "model-a" not in updated
        await exit_screen(env, task)

    asyncio.run(scenario())


def test_arrow_key_after_resize_still_reaches_current_mode(tmp_path: Path) -> None:
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
                [Finish()],
            ]
        )
        env = build(tmp_path, provider=provider, tool_executor=executor, size=(60, 20))
        task = await run_screen(env)
        env.fake.feed(*typed("go"), ENTER)
        await pump(lambda: env.screen.mode == "ask", message="ask")

        env.terminal.set_size((50, 16))
        env.fake.feed(DOWN)
        await pump(lambda: env.screen.renderer.width == 50, message="resized")
        interaction = env.screen.interaction
        assert interaction is not None
        assert interaction.selected == 1

        await exit_screen(env, task)

    asyncio.run(scenario())
