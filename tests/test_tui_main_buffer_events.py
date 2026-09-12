"""P6-11A-MR1 §8/§9：公共事件身份、顺序、终态与 stop/error 分类。

事件通过屏幕的公开投影边界 ``handle_event`` 注入，因此这些测试断言的正是
「公共事件如何改变界面」的契约，而不是内部实现细节。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from test_tui_main_buffer_lifecycle import build
from test_tui_main_buffer_screen import CTRL_C, DOWN, ENTER, ESCAPE, pump

from mica_agent.events import (
    ErrorCategory,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
    ToolFinished,
    ToolOutputDelta,
    ToolPrepared,
    ToolStarted,
    UserInputRequested,
)
from mica_agent.interactions import AskOption, AskPrompt
from mica_agent.tool_contracts import (
    ToolRequest,
    ToolResult,
    ToolResultStatus,
)
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    complete_tool_presentation,
    prepare_tool_presentation,
)
from mica_agent.tui.main_buffer.editor import Key
from mica_agent.tui.main_buffer.interactions import InteractionOutcome
from mica_agent.tui.main_buffer.runtime import InterruptedMarker

TIMEOUT = 5.0
UP = KeyPress(Keys.Up, chr(27) + chr(91) + "A")
RUN = "synth-run"


def frame_of(env: SimpleNamespace) -> str:
    """按当前状态重绘一帧并返回逻辑屏内容。

    合成事件走的是 ``handle_event``（状态投影），绘制仍由模式机负责，
    因此这里显式渲染一次，与真实循环中「事件 → 绘制」的顺序一致。
    """
    env.screen.render()
    renderer = env.screen.renderer
    assert renderer is not None
    return "\n".join(renderer.frame())


def prepared(run_id: str, call_id: str, seq: int, name: str = "read") -> ToolPrepared:
    return ToolPrepared(
        run_id=run_id,
        seq=seq,
        presentation=prepare_tool_presentation(
            ToolRequest(call_id, name, {"path": "/a.txt"})
        ),
    )


async def with_screen(tmp_path: Path, body: object) -> None:
    """启动真实屏幕，注入合成事件，再走两次 Ctrl+C 的公开退出路径。"""
    env = build(tmp_path, size=(60, 20))
    task = asyncio.create_task(env.screen.run())
    await pump(lambda: env.fake.is_attached, message="attach")
    try:
        await body(env)  # type: ignore[operator]
    finally:
        if not task.done():
            env.fake.feed(CTRL_C, CTRL_C)
            await asyncio.wait_for(task, timeout=TIMEOUT)


# -- §8 身份与顺序 ----------------------------------------------------------


def test_events_without_an_active_run_are_dropped(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(TextDelta(RUN, "orphan", 1))
        assert env.screen.composer_text == ""
        assert "orphan" not in frame_of(env)
        assert "event without active run" in env.screen.diagnostics

    asyncio.run(with_screen(tmp_path, body))


def test_events_for_another_run_are_dropped(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(TextDelta("other-run", "intruder", 1))
        assert "intruder" not in frame_of(env)
        assert "event for unknown run" in env.screen.diagnostics

    asyncio.run(with_screen(tmp_path, body))


def test_duplicate_and_out_of_order_events_are_dropped(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(TextDelta(RUN, "one", 1))
        await env.screen.handle_event(TextDelta(RUN, "DUP", 1))
        await env.screen.handle_event(TextDelta(RUN, "OLD", 0))
        await env.screen.handle_event(TextDelta(RUN, "two", 2))

        frame = frame_of(env)
        assert "one" in frame and "two" in frame
        assert "DUP" not in frame and "OLD" not in frame
        assert env.screen.diagnostics.count("event out of order") == 2

    asyncio.run(with_screen(tmp_path, body))


def test_events_after_the_terminal_state_are_dropped(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(TextDelta(RUN, "final", 1))
        await env.screen.handle_event(RunCompleted(RUN, seq=2))
        assert env.screen.status == "done"

        await env.screen.handle_event(TextDelta(RUN, "late", 3))
        await env.screen.handle_event(ToolStarted(RUN, 4, "call-x", "read"))
        assert "late" not in frame_of(env)
        assert env.screen.diagnostics.count("event after terminal state") == 2

    asyncio.run(with_screen(tmp_path, body))


def test_tool_events_with_the_wrong_call_id_do_not_touch_the_record(
    tmp_path: Path,
) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(prepared(RUN, "call-A", 1))
        await env.screen.handle_event(ToolStarted(RUN, 2, "call-B", "read"))
        await env.screen.handle_event(
            ToolOutputDelta(RUN, 3, "call-B", "read", "noise")
        )
        await env.screen.handle_event(
            ToolFinished(RUN, 4, "call-B", "read", ToolResultStatus.SUCCESS)
        )

        frame = frame_of(env)
        assert "○ read" in frame
        assert "noise" not in frame
        assert "✓" not in frame
        assert env.screen.diagnostics.count("tool event for wrong call") == 3

    asyncio.run(with_screen(tmp_path, body))


def test_tool_finished_uses_the_final_snapshot(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(prepared(RUN, "call-1", 1))
        final = complete_tool_presentation(
            ToolRequest("call-1", "read", {"path": "/b.txt"}),
            ToolResult("call-1", "read", ToolResultStatus.SUCCESS, "ok"),
        )
        await env.screen.handle_event(
            ToolFinished(RUN, 2, "call-1", "read", ToolResultStatus.SUCCESS, final)
        )

        frame = frame_of(env)
        assert "✓ read" in frame
        # 终态不再拿 pending 快照冒充：/a.txt 来自 PENDING 展示，不应保留。
        assert "/a.txt" not in frame
        assert "○" not in frame

    asyncio.run(with_screen(tmp_path, body))


def test_tool_presentation_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(prepared(RUN, "call-1", 1))
        # 事件自身的 call_id/tool_name/status 一致，但快照身份与 pending 记录不同。
        final = complete_tool_presentation(
            ToolRequest("call-1", "read", {"path": "/b.txt"}),
            ToolResult("call-1", "read", ToolResultStatus.SUCCESS, "ok"),
        )
        event = ToolFinished(RUN, 2, "call-1", "read", ToolResultStatus.SUCCESS, final)
        forged = replace(final)
        object.__setattr__(forged, "kind", ToolPresentationKind.BASH)
        object.__setattr__(event, "presentation", forged)
        await env.screen.handle_event(event)

        frame = frame_of(env)
        # 整条 ToolFinished 被拒绝：卡片未完成、仍可被后续合法终态收口。
        assert "✓" not in frame
        assert "○ read" in frame
        assert "/b.txt" not in frame
        assert "tool presentation identity mismatch" in env.screen.diagnostics
        assert env.screen._pending_call_id == "call-1"

        await env.screen.handle_event(
            ToolFinished(RUN, 3, "call-1", "read", ToolResultStatus.SUCCESS, final)
        )
        completed = frame_of(env)
        assert "✓ read" in completed
        assert "○" not in completed

    asyncio.run(with_screen(tmp_path, body))


def test_tool_presentation_status_mismatch_is_rejected(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(prepared(RUN, "call-1", 1))
        final = complete_tool_presentation(
            ToolRequest("call-1", "read", {"path": "/b.txt"}),
            ToolResult("call-1", "read", ToolResultStatus.SUCCESS, "ok"),
        )
        event = ToolFinished(RUN, 2, "call-1", "read", ToolResultStatus.SUCCESS, final)
        # 契约要求 SUCCESS 的快照配 SUCCESS 的终态；伪造出不一致组合。
        object.__setattr__(event, "status", ToolResultStatus.EXECUTION_FAILED)
        await env.screen.handle_event(event)

        frame = frame_of(env)
        assert "○ read" in frame
        assert "✓" not in frame
        assert "✗" not in frame
        assert "tool presentation identity mismatch" in env.screen.diagnostics

    asyncio.run(with_screen(tmp_path, body))


def test_tool_presentation_call_id_mismatch_is_rejected(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(prepared(RUN, "call-1", 1))
        other = complete_tool_presentation(
            ToolRequest("call-9", "read", {"path": "/b.txt"}),
            ToolResult("call-9", "read", ToolResultStatus.SUCCESS, "ok"),
        )
        event = ToolFinished(RUN, 2, "call-1", "read", ToolResultStatus.SUCCESS)
        # 公共构造会拒绝不一致的快照，这里验证前端内部的纵深校验。
        object.__setattr__(event, "presentation", other)
        await env.screen.handle_event(event)

        frame = frame_of(env)
        assert "○ read" in frame
        assert "/b.txt" not in frame
        assert "✓" not in frame
        assert "tool presentation identity mismatch" in env.screen.diagnostics

    asyncio.run(with_screen(tmp_path, body))


def test_tool_events_with_the_wrong_tool_name_do_not_touch_the_record(
    tmp_path: Path,
) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(prepared(RUN, "call-1", 1))
        await env.screen.handle_event(ToolStarted(RUN, 2, "call-1", "bash"))
        await env.screen.handle_event(
            ToolOutputDelta(RUN, 3, "call-1", "bash", "noise")
        )
        final = complete_tool_presentation(
            ToolRequest("call-1", "bash", {"command": "ls"}),
            ToolResult("call-1", "bash", ToolResultStatus.SUCCESS, "ok"),
        )
        await env.screen.handle_event(
            ToolFinished(RUN, 4, "call-1", "bash", ToolResultStatus.SUCCESS, final)
        )

        frame = frame_of(env)
        assert "○ read" in frame
        assert "noise" not in frame
        assert "bash" not in frame
        assert "✓" not in frame
        assert env.screen.diagnostics.count("tool event for wrong tool") == 3
        assert "tool presentation identity mismatch" not in env.screen.diagnostics

    asyncio.run(with_screen(tmp_path, body))


def test_finished_tool_is_not_repainted_by_a_later_run(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(prepared(RUN, "call-1", 1))
        await env.screen.handle_event(
            ToolFinished(RUN, 2, "call-1", "read", ToolResultStatus.SUCCESS)
        )
        await env.screen.handle_event(RunCompleted(RUN, seq=3))
        before = frame_of(env)

        await env.screen.handle_event(RunStarted("synth-run-2"))
        await env.screen.handle_event(
            RunFailed("synth-run-2", "boom", ErrorCategory.UNKNOWN, seq=1)
        )
        after = frame_of(env)

        # 陌生 Run 的事件不能重染当前记录，也不能改变当前终态。
        assert after.count("✓ read") == 1
        assert after.index("✓ read") == before.index("✓ read")
        assert "boom" not in after
        assert env.screen.status == "done"
        assert env.screen.diagnostics.count("event for unknown run") == 2

    asyncio.run(with_screen(tmp_path, body))


def test_model_step_order_is_preserved(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(TextDelta(RUN, "thinking out loud", 1))
        await env.screen.handle_event(prepared(RUN, "call-1", 2))
        await env.screen.handle_event(ToolStarted(RUN, 3, "call-1", "read"))
        await env.screen.handle_event(
            ToolFinished(RUN, 4, "call-1", "read", ToolResultStatus.SUCCESS)
        )
        await env.screen.handle_event(TextDelta(RUN, "after the tool", 5))

        frame = frame_of(env)
        assert frame.index("thinking out loud") < frame.index("✓ read")
        assert frame.index("✓ read") < frame.index("after the tool")

    asyncio.run(with_screen(tmp_path, body))


# -- §9 stop 与 error -------------------------------------------------------


def test_user_cancellation_maps_to_stop(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(InterruptedMarker("cancelled"))
        assert env.screen.status == "stop"
        assert env.screen.mode == "idle"
        assert "Mica · stop" in frame_of(env)

    asyncio.run(with_screen(tmp_path, body))


def test_stream_end_and_runtime_errors_map_to_error(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(InterruptedMarker("stream_ended"))
        assert env.screen.status == "error"
        assert "Mica · error" in frame_of(env)

    asyncio.run(with_screen(tmp_path, body))


def test_runtime_error_marker_never_shows_internal_detail(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(InterruptedMarker("runtime_error"))
        frame = frame_of(env)
        assert "Mica · error" in frame
        assert "Traceback" not in frame
        assert "RuntimeError" not in frame
        assert env.screen.diagnostics == []

    asyncio.run(with_screen(tmp_path, body))


def test_run_failed_never_shows_raw_message_or_paths(tmp_path: Path) -> None:
    secrets = (
        "C:"
        + chr(92)
        + "Users"
        + chr(92)
        + "10601"
        + chr(92)
        + ".mica"
        + chr(92)
        + "key.txt",
        "/home/deploy/.mica/sessions/2026.jsonl",
        '429 Too Many Requests: {"error": {"message": "quota", "key": "sk-live-abc"}}',
        chr(27) + "[31mboom" + chr(27) + "[0m",
        chr(27) + "[?1049h",
    )
    categories = (
        ErrorCategory.AUTHENTICATION,
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.UNKNOWN,
    )

    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        for index, (secret, category) in enumerate(
            zip(secrets, categories, strict=False)
        ):
            env.screen._run_id = None
            env.screen._terminal_run_id = None
            env.screen._last_seq = -1
            await env.screen.handle_event(RunStarted(RUN))
            await env.screen.handle_event(
                RunFailed(RUN, "boom: " + secret, category, seq=1)
            )
            frame = frame_of(env)
            assert env.screen.status == "error"
            assert "Mica · error" in frame
            assert "boom" not in frame
            for candidate in secrets:
                assert candidate not in frame
                assert candidate not in env.screen._terminal.emitted_text()
        # 只留下按 ErrorCategory 映射的固定文案。
        assert "Provider authentication failed." in frame
        assert "Provider rate limit reached." in frame
        assert "The run stopped because of an error." in frame

    asyncio.run(with_screen(tmp_path, body))


def test_run_failed_category_maps_to_fixed_text(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(
            RunFailed(RUN, "boom", ErrorCategory.TIMEOUT, seq=1)
        )
        frame = frame_of(env)
        assert env.screen.status == "error"
        assert "Mica · error" in frame
        assert "The provider request timed out." in frame
        assert "error: " in frame
        assert "boom" not in frame

    asyncio.run(with_screen(tmp_path, body))


def test_public_run_cancelled_event_wins_over_marker(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(RunCancelled(RUN, seq=1))
        assert env.screen.status == "stop"

    asyncio.run(with_screen(tmp_path, body))


# -- §8 交互响应被拒绝时的真实回归 -------------------------------------------


def test_rejected_ask_response_keeps_the_interaction_and_the_selection(
    tmp_path: Path,
) -> None:
    """响应被业务侧拒绝（responder 返回 False）时不得关闭面板或伪造终态。"""

    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        prompt = AskPrompt(
            "继续吗？",
            title="确认",
            options=(AskOption("是"), AskOption("否")),
            allow_custom=True,
        )
        # 合成事件打开的交互没有对应的 Broker 待答请求，因此响应会被拒绝。
        await env.screen.handle_event(
            UserInputRequested(RUN, 1, "ask-1", "继续吗？", prompt)
        )
        assert env.screen.mode == "ask"
        interaction = env.screen.interaction
        assert interaction is not None

        env.fake.feed(DOWN)
        await pump(lambda: interaction.selected == 1, message="selection")
        draft_before = env.screen.composer_text
        frame_before = frame_of(env)
        assert "sessions:" not in frame_before
        assert "Type your request or command..." not in frame_before

        # Enter 仍走真实输入队列；用「交互收到按键」这一可观察信号等待，
        # 不用固定 sleep：拒绝响应是同步完成的，路由到即已处理完毕。
        routed: list[Key] = []
        frames_before = len(env.terminal.emitted)
        original_handle = interaction.handle

        def counting_handle(key: Key) -> InteractionOutcome:
            routed.append(key)
            return original_handle(key)

        interaction.handle = counting_handle  # type: ignore[method-assign]
        env.fake.feed(ENTER)
        await pump(lambda: bool(routed), message="enter routed to the interaction")
        await pump(
            lambda: len(env.terminal.emitted) > frames_before,
            message="enter produced a new frame",
        )

        # 交互对象身份、模式与选择都不变，草稿也没被动过。
        assert env.screen.interaction is interaction
        assert env.screen.mode == "ask"
        assert interaction.selected == 1
        assert env.screen.composer_text == draft_before
        # composer 与事实行仍然被交互区替代，面板还在。
        frame = frame_of(env)
        assert "sessions:" not in frame
        assert "Type your request or command..." not in frame
        assert "继续吗？" in frame
        assert "是" in frame and "否" in frame
        # 还能继续用方向键操作，并且没有伪造任何业务终态。
        env.fake.feed(UP)
        await pump(lambda: interaction.selected == 0, message="navigate again")
        assert env.screen.status == "working"
        assert env.screen._terminal_run_id is None

        env.fake.feed(ESCAPE)
        await pump(lambda: env.screen.mode != "ask", message="cancelled")

    asyncio.run(with_screen(tmp_path, body))
