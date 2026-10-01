"""公共事件身份、顺序、终态与 stop/error 分类测试。

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
from test_tui_main_buffer_screen import CTRL_C, DOWN, ENTER, ESCAPE, pump, typed

from mica_agent.events import (
    ErrorCategory,
    PermissionRequested,
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
from mica_agent.tui.main_buffer.interactions import (
    HINT_RESPONSE_REJECTED,
    InteractionOutcome,
)
from mica_agent.tui.main_buffer.runtime import InterruptedMarker

TIMEOUT = 5.0
UP = KeyPress(Keys.Up, chr(27) + chr(91) + "A")
F5 = KeyPress(Keys.F5, chr(27) + "[15~")
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


def test_rejected_ask_response_keeps_the_interaction_and_shows_a_hint(
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
        # 不用固定 sleep：拒绝响应在同一按键处理内同步完成，路由到即已处理。
        routed: list[Key] = []
        original_handle = interaction.handle

        def counting_handle(key: Key) -> InteractionOutcome:
            routed.append(key)
            return original_handle(key)

        interaction.handle = counting_handle  # type: ignore[method-assign]
        env.fake.feed(ENTER)
        await pump(lambda: bool(routed), message="enter routed to the interaction")

        # 交互对象身份、模式与选择都不变，草稿也没被动过；面板内出现固定提示。
        assert env.screen.interaction is interaction
        assert env.screen.mode == "ask"
        assert interaction.selected == 1
        assert env.screen.composer_text == draft_before
        frame = frame_of(env)
        assert "sessions:" not in frame
        assert "Type your request or command..." not in frame
        assert "继续吗？" in frame
        assert "是" in frame and "否" in frame
        assert HINT_RESPONSE_REJECTED in frame
        # 提示只说明下一步能做什么，不回显响应、交互 ID 或 Broker 内部状态。
        assert "ask-1" not in frame
        assert "False" not in frame

        # 可以再次提交：重试仍然打到同一个交互对象。
        routed.clear()
        env.fake.feed(ENTER)
        await pump(lambda: bool(routed), message="retry routed")
        assert env.screen.interaction is interaction
        assert HINT_RESPONSE_REJECTED in frame_of(env)

        # 未被消费的按键（PageUp/PageDown/未知功能键/越界数字）不改变交互状态，
        # 因此不得清掉拒绝提示，也不得移动选择。
        assert interaction.selected == 1
        routed.clear()
        env.fake.feed(PAGE_UP, PAGE_DOWN, F5, *typed("9"))
        await pump(lambda: len(routed) == 4, message="unconsumed keys routed")
        # 前三个是未被识别的功能键，第四个是越界数字（都不会改变交互状态）。
        assert [key.name for key in routed] == ["unknown", "unknown", "unknown", "text"]
        assert routed[-1].data == "9"
        assert interaction.selected == 1
        assert HINT_RESPONSE_REJECTED in frame_of(env)

        # 导航是新的有效输入：旧提示按清晰规则消失。
        env.fake.feed(UP)
        await pump(lambda: interaction.selected == 0, message="navigate again")
        assert HINT_RESPONSE_REJECTED not in frame_of(env)

        # 再次提交仍被拒绝时提示重新出现，不被上一次的清除吞掉。
        routed.clear()
        env.fake.feed(ENTER)
        await pump(lambda: bool(routed), message="second retry routed")
        assert HINT_RESPONSE_REJECTED in frame_of(env)
        assert env.screen.status == "working"
        assert env.screen._terminal_run_id is None

        env.fake.feed(ESCAPE)
        await pump(lambda: env.screen.mode != "ask", message="cancelled")

    asyncio.run(with_screen(tmp_path, body))


def test_rejected_ask_response_keeps_notes_and_the_custom_draft(
    tmp_path: Path,
) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        prompt = AskPrompt(
            "继续吗？",
            options=(AskOption("是"), AskOption("否")),
            allow_custom=True,
        )
        await env.screen.handle_event(
            UserInputRequested(RUN, 1, "ask-1", "继续吗？", prompt)
        )
        interaction = env.screen.interaction
        assert interaction is not None

        env.fake.feed(*typed("n"))
        await pump(lambda: interaction.editing == "notes", message="notes mode")
        env.fake.feed(*typed("why"))
        await pump(lambda: interaction.note == "why", message="note text")
        env.fake.feed(ENTER)
        await pump(lambda: interaction.editing == "options", message="options")

        env.fake.feed(DOWN, DOWN, ENTER)
        await pump(lambda: interaction.editing == "custom", message="custom")
        env.fake.feed(*typed("draft"))
        await pump(lambda: interaction.custom == "draft", message="draft")

        routed: list[Key] = []
        original_handle = interaction.handle

        def counting_handle(key: Key) -> InteractionOutcome:
            routed.append(key)
            return original_handle(key)

        interaction.handle = counting_handle  # type: ignore[method-assign]
        env.fake.feed(ENTER)
        await pump(lambda: bool(routed), message="enter routed")

        assert env.screen.interaction is interaction
        assert env.screen.mode == "ask"
        assert interaction.editing == "custom"
        assert interaction.custom == "draft"
        assert interaction.note == "why"
        assert HINT_RESPONSE_REJECTED in frame_of(env)

        env.fake.feed(ESCAPE)
        await pump(lambda: env.screen.mode != "ask", message="cancelled")

    asyncio.run(with_screen(tmp_path, body))


def test_rejected_permission_response_keeps_the_panel_and_shows_a_hint(
    tmp_path: Path,
) -> None:
    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(
            PermissionRequested(RUN, 1, "perm-1", "call-1", "bash", "run the command")
        )
        assert env.screen.mode == "permission"
        interaction = env.screen.interaction
        assert interaction is not None

        env.fake.feed(DOWN)
        await pump(lambda: interaction.selected == 1, message="selection")

        routed: list[Key] = []
        original_handle = interaction.handle

        def counting_handle(key: Key) -> InteractionOutcome:
            routed.append(key)
            return original_handle(key)

        interaction.handle = counting_handle  # type: ignore[method-assign]
        env.fake.feed(ENTER)
        await pump(lambda: bool(routed), message="enter routed")

        assert env.screen.interaction is interaction
        assert env.screen.mode == "permission"
        assert interaction.selected == 1
        frame = frame_of(env)
        assert "Do you want to proceed?" in frame
        assert "1. Yes" in frame and "2. No" in frame
        assert HINT_RESPONSE_REJECTED in frame
        assert "perm-1" not in frame
        assert env.screen.status == "working"
        assert env.screen._terminal_run_id is None

        # 未被消费的按键不清提示，有效数字定位才清除。
        routed.clear()
        env.fake.feed(PAGE_DOWN, F5, *typed("7"))
        await pump(lambda: len(routed) == 3, message="unconsumed keys routed")
        assert interaction.selected == 1
        assert HINT_RESPONSE_REJECTED in frame_of(env)

        env.fake.feed(*typed("1"))
        await pump(lambda: interaction.selected == 0, message="digit positioning")
        assert HINT_RESPONSE_REJECTED not in frame_of(env)

        env.fake.feed(ESCAPE)
        await pump(lambda: env.screen.mode != "permission", message="cancelled")

    asyncio.run(with_screen(tmp_path, body))


# 名称与 kind、全局展开、活动工具预览

CTRL_O = KeyPress(Keys.ControlO, "\x0f")
PAGE_UP = KeyPress(Keys.PageUp, chr(27) + chr(91) + "5~")
PAGE_DOWN = KeyPress(Keys.PageDown, chr(27) + chr(91) + "6~")


def test_tool_prepared_with_mismatched_kind_is_rejected(tmp_path: Path) -> None:
    """名称与 presentation kind 不一致时拒绝投影，且不回退成 Generic。"""

    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        prepared_event = prepared(RUN, "call-1", 1, "bash")
        # 伪造一个「bash 名称 + read kind」的非法快照。
        object.__setattr__(
            prepared_event.presentation, "kind", ToolPresentationKind.READ
        )
        object.__setattr__(
            prepared_event.presentation, "payload", {"path": "docs/a.md"}
        )
        await env.screen.handle_event(prepared_event)

        await env.screen.handle_event(ToolStarted(RUN, 2, "call-1", "bash"))
        frame = frame_of(env)
        assert "bash" not in frame
        assert "read" not in frame
        assert "tool presentation rejected" in env.screen.diagnostics
        assert "tool event without pending record" in env.screen.diagnostics

    asyncio.run(with_screen(tmp_path, body))


def _assistant_slots(env: SimpleNamespace) -> list[object]:
    renderer = env.screen.renderer
    assert renderer is not None
    return [
        slot.block
        for slot in renderer._slots
        if getattr(slot.block, "kind", "") == "assistant"
    ]


def _document(env: SimpleNamespace) -> list[str]:
    renderer = env.screen.renderer
    assert renderer is not None
    return [row.rstrip() for row in renderer.document_lines()]


def test_rejected_tool_prepared_does_not_split_the_assistant_stream(
    tmp_path: Path,
) -> None:
    """被拒绝的 ToolPrepared 不得切断当前模型步骤；后续 TextDelta 仍追加同一流。"""

    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(TextDelta(RUN, "alpha", 1))
        before_kind = env.screen._pending_kind
        before_block = env.screen._assistant_block
        before_slots = len(_assistant_slots(env))

        mismatched = prepared(RUN, "call-bad", 2, "bash")
        object.__setattr__(mismatched.presentation, "kind", ToolPresentationKind.READ)
        object.__setattr__(mismatched.presentation, "payload", {"path": "docs/a.md"})
        await env.screen.handle_event(mismatched)

        assert env.screen._pending_kind == before_kind == "assistant"
        assert env.screen._assistant_block is before_block
        assert env.screen._pending_call_id is None
        assert env.screen._pending_presentation is None
        assert len(_assistant_slots(env)) == before_slots == 1
        assert "tool presentation rejected" in env.screen.diagnostics
        assert "call-bad" not in " ".join(env.screen.diagnostics)
        assert "docs/a.md" not in " ".join(env.screen.diagnostics)

        await env.screen.handle_event(TextDelta(RUN, "beta", 3))
        lines = _document(env)
        joined = "\n".join(lines)
        assert "alpha" in joined and "beta" in joined
        assert env.screen._assistant_block is before_block
        assert env.screen._assistant_block is not None
        assert env.screen._assistant_block.source == "alphabeta"
        assert len(_assistant_slots(env)) == 1
        assert "\n\n" not in joined
        assert not any("bash" in row or "read" in row for row in lines)
        assert "▌" in joined

        await env.screen.handle_event(prepared(RUN, "call-read", 4, "read"))
        after = _document(env)
        joined = "\n".join(after)
        assert any("read" in row for row in after)
        assert env.screen._pending_kind == "tool"
        assert env.screen._pending_call_id == "call-read"
        assert env.screen._assistant_block is None
        assistants = _assistant_slots(env)
        assert len(assistants) == 1
        finished = assistants[0]
        assert getattr(finished, "streaming", True) is False
        assert "alpha" in joined and "beta" in joined
        alpha_at = next(i for i, row in enumerate(after) if "alpha" in row)
        read_at = next(i for i, row in enumerate(after) if "read" in row)
        assert alpha_at < read_at
        assert after[read_at - 1] == ""

    asyncio.run(with_screen(tmp_path, body))


def test_missing_presentation_does_not_split_the_assistant_stream(
    tmp_path: Path,
) -> None:
    """公共类型要求 presentation，但投影入口仍按缺失快照拒绝且不切断回复流。"""

    async def body(env: SimpleNamespace) -> None:
        await env.screen.handle_event(RunStarted(RUN))
        await env.screen.handle_event(TextDelta(RUN, "alpha", 1))
        before_block = env.screen._assistant_block
        missing = prepared(RUN, "call-missing", 2, "bash")
        object.__setattr__(missing, "presentation", None)
        await env.screen.handle_event(missing)
        assert env.screen._assistant_block is before_block
        assert env.screen._pending_kind == "assistant"
        await env.screen.handle_event(TextDelta(RUN, "beta", 3))
        assert env.screen._assistant_block is before_block
        assert env.screen._assistant_block is not None
        assert env.screen._assistant_block.source == "alphabeta"
        assert len(_assistant_slots(env)) == 1
        assert "tool presentation rejected" in env.screen.diagnostics
        assert not any("bash" in row for row in _document(env))

    asyncio.run(with_screen(tmp_path, body))


def test_new_tool_record_follows_the_current_expand_state(tmp_path: Path) -> None:
    """Ctrl+O 后再完成的工具直接按当前全局状态展开。"""

    async def body(env: SimpleNamespace) -> None:
        env.fake.feed(CTRL_O)
        await pump(
            lambda: env.screen.renderer is not None and env.screen.renderer.expanded,
            message="expanded",
        )
        await env.screen.handle_event(RunStarted(RUN))
        request = ToolRequest("call-write", "write", {"path": "/a.txt"})
        prepared_event = ToolPrepared(RUN, 1, prepare_tool_presentation(request))
        content = chr(10).join(f"line {index}" for index in range(14))
        result = ToolResult(
            call_id="call-write",
            tool_name="write",
            status=ToolResultStatus.SUCCESS,
            message="write completed",
            data={"content": content},
        )
        await env.screen.handle_event(prepared_event)
        snapshot = complete_tool_presentation(
            request,
            result,
            detail={"content": content},
        )
        object.__setattr__(
            snapshot, "payload", {"path": "docs/a.md", "content": content}
        )
        await env.screen.handle_event(
            ToolFinished(
                RUN,
                2,
                "call-write",
                "write",
                ToolResultStatus.SUCCESS,
                snapshot,
            )
        )
        frame = frame_of(env)

        assert "line 13" in frame
        assert "ctrl+o to collapse" in frame

    asyncio.run(with_screen(tmp_path, body))


def test_active_bash_preview_ignores_page_keys_and_keeps_no_scroll(
    tmp_path: Path,
) -> None:
    """活动 Bash 只保留有界预览，不建立内部滚动，也不抢翻页键。"""

    async def body(env: SimpleNamespace) -> None:
        await start_bash(env)
        for index in range(20):
            await env.screen.handle_event(
                ToolOutputDelta(RUN, 3 + index, "call-bash", "bash", f"out {index}\n")
            )
        before = frame_of(env)
        outputs_before = [row for row in before.split(chr(10)) if row.startswith("out")]
        assert 0 < len(outputs_before) <= 8

        env.fake.feed(PAGE_UP, DOWN, PAGE_DOWN)
        # 不等固定时长：等到输入替身队列变空（屏幕已取走这批按键）。
        await pump(lambda: _keys_consumed(env), message="page keys consumed")
        after = frame_of(env)
        outputs_after = [row for row in after.split(chr(10)) if row.startswith("out")]

        assert outputs_after == outputs_before
        assert env.screen.composer_text == ""

    asyncio.run(with_screen(tmp_path, body))


# Bash 动态输出的逻辑行语义


def _keys_consumed(env: SimpleNamespace) -> bool:
    """测试输入替身的待处理队列已空 = 屏幕已取走这批按键。

    替身定义在屏幕测试文件中（本轮不在修改范围），因此这里只读取其内部
    队列；本模块其余断言一律走公开屏幕接口。
    """
    return not env.fake._pending


def bash_prepared(seq: int, command: str = "printf a") -> ToolPrepared:
    return ToolPrepared(
        run_id=RUN,
        seq=seq,
        presentation=prepare_tool_presentation(
            ToolRequest("call-bash", "bash", {"command": command})
        ),
    )


async def start_bash(env: SimpleNamespace) -> None:
    await env.screen.handle_event(RunStarted(RUN))
    await env.screen.handle_event(bash_prepared(1))
    await env.screen.handle_event(ToolStarted(RUN, 2, "call-bash", "bash"))
    env.seq = 3  # 后续增量按真实顺序递增编号，避免被乱序保护丢弃


async def push_bash_output(env: SimpleNamespace, *chunks: str) -> None:
    for chunk in chunks:
        await env.screen.handle_event(
            ToolOutputDelta(RUN, env.seq, "call-bash", "bash", chunk)
        )
        env.seq += 1


def test_active_bash_keeps_blank_logical_lines(tmp_path: Path) -> None:
    """``first\\n`` + ``\\n`` + ``last`` 必须保留为三条逻辑行。"""

    async def body(env: SimpleNamespace) -> None:
        await start_bash(env)
        await push_bash_output(env, "first\n", "\n", "last")
        frame = frame_of(env)

        assert "first\n\nlast" in frame

    asyncio.run(with_screen(tmp_path, body))


def test_active_bash_keeps_crlf_and_unterminated_tail(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await start_bash(env)
        await push_bash_output(env, "head\r\n\r\n", "tail")
        frame = frame_of(env)

        # CRLF 与连续空行归一为逻辑行；尾部无换行的片段仍作为一条进行中行。
        assert "head\n\ntail" in frame
        assert "\r" not in frame

    asyncio.run(with_screen(tmp_path, body))


def test_active_bash_joins_fragments_split_across_events(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await start_bash(env)
        await push_bash_output(env, "abc", "def\n")
        frame = frame_of(env)

        assert "abcdef" in frame
        # 两个片段不能被当成两条逻辑行。
        assert "abc\n" not in frame
        assert frame.count("abcdef") == 1

    asyncio.run(with_screen(tmp_path, body))


def test_active_bash_window_and_hidden_count_are_exact(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await start_bash(env)
        await push_bash_output(env, *[f"out {index:02d}\n" for index in range(20)])
        frame = frame_of(env)

        assert "out 19" in frame and "out 12" in frame
        assert "out 11" not in frame
        assert "(12 earlier lines)" in frame

    asyncio.run(with_screen(tmp_path, body))


def test_active_bash_counts_the_in_flight_line(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await start_bash(env)
        await push_bash_output(env, *[f"out {index:02d}\n" for index in range(8)])
        await push_bash_output(env, "partial tail")
        frame = frame_of(env)

        # 进行中行也占一条逻辑行：八条可见行里包含它，更早行数仍准确。
        assert "partial tail" in frame
        assert "out 01" in frame
        assert "out 00" not in frame
        assert "(1 earlier lines)" in frame

    asyncio.run(with_screen(tmp_path, body))


def test_finished_bash_uses_the_final_snapshot_only(tmp_path: Path) -> None:
    async def body(env: SimpleNamespace) -> None:
        await start_bash(env)
        await push_bash_output(env, "stale line\n")
        final = complete_tool_presentation(
            ToolRequest("call-bash", "bash", {"command": "printf a"}),
            ToolResult(
                "call-bash",
                "bash",
                ToolResultStatus.SUCCESS,
                "ok",
                data={"output": "authoritative line\n", "exit_code": 0},
            ),
        )
        await env.screen.handle_event(
            ToolFinished(
                RUN, env.seq, "call-bash", "bash", ToolResultStatus.SUCCESS, final
            )
        )
        frame = frame_of(env)

        assert "authoritative line" in frame
        assert "stale line" not in frame
        assert "✓ bash" in frame

    asyncio.run(with_screen(tmp_path, body))
