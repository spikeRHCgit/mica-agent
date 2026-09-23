"""主缓冲区内容层级、思考模式、全局 Ctrl+O 与历史复用测试（P5-11B §4.2/4.3/4.6/4.7）。"""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from test_tui_main_buffer_screen import (
    ENTER,
    FakeInput,
    ScreenHarness,
    ScriptedProvider,
    make_app,
    pump,
    typed,
)

from mica_agent.checkpoint_contracts import (
    RunCheckpoint,
    RunCheckpointKind,
    RunCheckpointProgress,
    RunCheckpointToolProgress,
    RunCheckpointToolStatus,
)
from mica_agent.events import (
    ReasoningDelta,
    RunStarted,
    TextDelta,
    ToolFinished,
    ToolOutputDelta,
    ToolPrepared,
    ToolStarted,
)
from mica_agent.models import Message, MessageRole
from mica_agent.session_contracts import TurnEntry
from mica_agent.tool_contracts import ToolCall, ToolResultStatus
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)
from mica_agent.tui.main_buffer import MainBufferScreen
from mica_agent.tui.main_buffer.components import (
    USER_MARKER,
    ReasoningBlock,
    UserBlock,
)
from mica_agent.tui.main_buffer.markdown import (
    STREAM_CURSOR,
    MarkdownBlock,
    StreamingMarkdownBlock,
)
from mica_agent.tui.main_buffer.renderer import (
    MainBufferRenderer,
    Palette,
    cell_len,
    strip_ansi,
)
from mica_agent.tui.main_buffer.screen import CHECKPOINT_INCOMPLETE_MARKER
from mica_agent.tui.main_buffer.terminal import Terminal
from mica_agent.tui.main_buffer.tool_views import ToolViewFactory
from mica_agent.tui.models import ReasoningDisplayMode, StartupResourceSnapshot

CTRL_O = KeyPress(Keys.ControlO, "\x0f")
RUN = "run-1"


def visible(rows: list[str]) -> list[str]:
    return [strip_ansi(row).rstrip() for row in rows]


def document(screen: MainBufferScreen) -> list[str]:
    assert screen.renderer is not None
    return visible(screen.renderer.document_lines())


def make_terminal(input_factory: object | None = None) -> Terminal:
    return Terminal(
        output=io.StringIO(),
        input_factory=(lambda: None) if input_factory is None else input_factory,  # type: ignore[arg-type]
        size=(60, 24),
        interactive=True,
        environ={"NO_COLOR": "1"},
    )


def make_renderer() -> MainBufferRenderer:
    return MainBufferRenderer(
        make_terminal(), width=60, height=24, palette=Palette(False)
    )


def read_view() -> object:
    snapshot = ToolPresentationSnapshot(
        "call-read",
        "read",
        ToolPresentationKind.READ,
        ToolPresentationStatus.SUCCESS,
        {"path": "docs/a.md"},
    )
    return ToolViewFactory().create(snapshot, state="success")


def write_snapshot(
    lines: int, *, call_id: str = "call-write"
) -> ToolPresentationSnapshot:
    content = "\n".join(f"line {index}" for index in range(lines))
    return ToolPresentationSnapshot(
        call_id,
        "write",
        ToolPresentationKind.WRITE,
        ToolPresentationStatus.SUCCESS,
        {"path": "docs/a.md", "content": content},
    )


def make_screen_with(
    tmp_path: Path,
    fake: FakeInput,
    *,
    mode: ReasoningDisplayMode = ReasoningDisplayMode.COLLAPSED,
    stored: object | None = None,
) -> MainBufferScreen:
    provider = ScriptedProvider([])
    app = make_app(tmp_path, provider)
    return MainBufferScreen(
        app,
        terminal=make_terminal(lambda: fake),
        startup_resources=StartupResourceSnapshot(tool_names=("ask", "bash")),
        stored_session=stored,
        reasoning_mode=mode,
    )


# -- 内容层级 ---------------------------------------------------------------


def _plain(row: str) -> str:
    return strip_ansi(row).rstrip()


def test_user_message_is_plain_text_with_identity_marker() -> None:
    block = UserBlock("# 标题 **不是粗体** `code`")
    rows = block.render(60, Palette(False), False)

    assert len(rows) == 1
    assert _plain(rows[0]).startswith("▌ # 标题 **不是粗体** `code`")
    assert cell_len(rows[0]) == 60
    coloured = block.render(60, Palette(True), False)
    assert "48;2;30;41;59" in coloured[0]
    assert cell_len(strip_ansi(coloured[0])) == 60


def test_content_hierarchy_order_and_separation() -> None:
    renderer = make_renderer()
    renderer.append(UserBlock("请修改文档"))
    renderer.append(ReasoningBlock("先看文档结构", ReasoningDisplayMode.COLLAPSED))
    renderer.append(MarkdownBlock("改好了。"))
    view = read_view()
    assert view is not None
    renderer.append(view)

    lines = [_plain(row) for row in renderer.document_lines()]
    joined = "\n".join(lines)
    user = lines.index("▌ 请修改文档")
    think = lines.index("│ 先看文档结构")
    reply = lines.index("改好了。")
    tool = next(index for index, row in enumerate(lines) if "read" in row)
    assert user < think < reply < tool
    assert lines[user + 1] == ""
    assert lines[think - 1] == ""
    assert lines[think + 1] == ""
    assert lines[reply - 1] == ""
    assert lines[reply + 1] == ""
    assert lines[tool - 1] == ""
    assert "\n\n\n" not in joined


def test_reasoning_display_modes_are_injectable() -> None:
    text = "第一行思考\n第二行思考\n第三行思考"
    collapsed = visible(ReasoningBlock(text).render(40, Palette(False), False))
    full = visible(
        ReasoningBlock(text, ReasoningDisplayMode.FULL).render(
            40, Palette(False), False
        )
    )
    hidden = ReasoningBlock(text, ReasoningDisplayMode.HIDDEN).render(
        40, Palette(False), False
    )

    assert collapsed == ["│ 第一行思考", "│ 第二行思考…"]
    assert full == ["│ 第一行思考", "│ 第二行思考", "│ 第三行思考"]
    assert hidden == []


def test_reasoning_mode_is_a_screen_seam_and_ctrl_o_ignores_it(tmp_path: Path) -> None:
    async def scenario(mode: ReasoningDisplayMode) -> None:
        fake = FakeInput()
        screen = make_screen_with(tmp_path, fake, mode=mode)
        async with ScreenHarness(screen, fake) as harness:
            await screen.handle_event(RunStarted(RUN))
            await screen.handle_event(ReasoningDelta(RUN, "思考一\n思考二\n思考三", 1))
            await screen.handle_event(TextDelta(RUN, "回复正文", 2))
            screen.render()
            before = [row for row in document(screen) if row.startswith("│")]

            fake.feed(CTRL_O)
            await pump(lambda: screen.renderer is not None and screen.renderer.expanded)
            screen.render()
            after = [row for row in document(screen) if row.startswith("│")]
            assert after == before
            assert any("回复正文" in row for row in document(screen))
            _ = harness

    asyncio.run(scenario(ReasoningDisplayMode.COLLAPSED))
    asyncio.run(scenario(ReasoningDisplayMode.HIDDEN))


def test_hidden_reasoning_leaves_no_placeholder(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeInput()
        screen = make_screen_with(tmp_path, fake, mode=ReasoningDisplayMode.HIDDEN)
        async with ScreenHarness(screen, fake) as harness:
            await screen.handle_event(RunStarted(RUN))
            await screen.handle_event(ReasoningDelta(RUN, "隐藏的思考", 1))
            await screen.handle_event(TextDelta(RUN, "正文", 2))
            screen.render()
            rows = document(screen)

            assert not any("隐藏的思考" in row for row in rows)
            # 隐藏模式不产生占位行，也不影响相邻块间距（启动面板本身带有边框行）。
            assert not any(row.startswith("│ ") and "系统提示" in row for row in rows)
            _ = harness

    asyncio.run(scenario())


# -- 全局 Ctrl+O ------------------------------------------------------------


async def prepare_write_tool(
    screen: MainBufferScreen, snapshot: ToolPresentationSnapshot
) -> None:
    pending = ToolPresentationSnapshot(
        snapshot.call_id,
        snapshot.tool_name,
        snapshot.kind,
        ToolPresentationStatus.PENDING,
        {"path": "docs/a.md", "content": ""},
    )
    await screen.handle_event(RunStarted(RUN))
    await screen.handle_event(ToolPrepared(RUN, 1, pending))
    await screen.handle_event(
        ToolFinished(
            RUN,
            2,
            snapshot.call_id,
            snapshot.tool_name,
            ToolResultStatus.SUCCESS,
            snapshot,
        )
    )


def test_ctrl_o_expands_terminal_tools_and_collapses_again(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeInput()
        screen = make_screen_with(tmp_path, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("draft"))
            await pump(lambda: screen.composer_text == "draft", message="draft")
            await prepare_write_tool(screen, write_snapshot(14))
            screen.render()
            assert any("line 9" in row for row in document(screen))
            assert not any("line 13" in row for row in document(screen))
            cursor_before = screen.editor.cursor

            fake.feed(CTRL_O)
            await pump(
                lambda: any("line 13" in row for row in document(screen)),
                message="expanded",
            )
            assert screen.renderer is not None and screen.renderer.expanded is True
            assert screen.composer_text == "draft"
            assert screen.editor.cursor == cursor_before
            assert any("line 13" in row for row in document(screen))

            fake.feed(CTRL_O)
            await pump(
                lambda: not any("line 13" in row for row in document(screen)),
                message="collapsed",
            )
            assert screen.renderer is not None and screen.renderer.expanded is False
            assert screen.composer_text == "draft"
            _ = harness

    asyncio.run(scenario())


def test_ctrl_o_keeps_active_bash_preview_bounded(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeInput()
        screen = make_screen_with(tmp_path, fake)
        async with ScreenHarness(screen, fake) as harness:
            pending = ToolPresentationSnapshot(
                "call-bash",
                "bash",
                ToolPresentationKind.BASH,
                ToolPresentationStatus.PENDING,
                {"command": "uv run pytest -q"},
            )
            await screen.handle_event(RunStarted(RUN))
            await screen.handle_event(ToolPrepared(RUN, 1, pending))
            await screen.handle_event(ToolStarted(RUN, 2, "call-bash", "bash"))
            for index in range(20):
                await screen.handle_event(
                    ToolOutputDelta(RUN, 3 + index, "call-bash", "bash", f"out {index}")
                )
            screen.render()
            before = [row for row in document(screen) if row.startswith("out")]
            assert 0 < len(before) <= 8

            fake.feed(CTRL_O)
            await pump(lambda: screen.renderer is not None and screen.renderer.expanded)
            screen.render()
            after = [row for row in document(screen) if row.startswith("out")]
            assert after == before
            _ = harness

    asyncio.run(scenario())


def test_ctrl_o_does_not_touch_composer_or_facts(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeInput()
        screen = make_screen_with(tmp_path, fake)
        async with ScreenHarness(screen, fake) as harness:
            fake.feed(*typed("keep me"))
            await pump(lambda: screen.composer_text == "keep me", message="draft")
            before = screen.renderer.chrome if screen.renderer else []

            fake.feed(CTRL_O)
            await pump(lambda: screen.renderer is not None and screen.renderer.expanded)
            screen.render()
            after = screen.renderer.chrome if screen.renderer else []

            assert screen.composer_text == "keep me"
            assert screen.submit_text == "keep me"
            assert len(after) == len(before)
            _ = harness

    asyncio.run(scenario())


# -- 历史复用 ---------------------------------------------------------------


def history_session(presentation: ToolPresentationSnapshot | None) -> object:
    write_call = SimpleNamespace(call_id="call-write", name="write")
    other_call = SimpleNamespace(call_id="call-other", name="custom_tool")
    turn = SimpleNamespace(
        user_message=SimpleNamespace(content="写入文档"),
        tool_presentations=() if presentation is None else (presentation,),
        intermediate_messages=(SimpleNamespace(tool_calls=(write_call, other_call)),),
        assistant_message=SimpleNamespace(content="写好了。"),
    )
    return SimpleNamespace(turns=(turn,))


def test_history_uses_same_views_and_degrades_without_snapshot(tmp_path: Path) -> None:
    live_renderer = make_renderer()
    live_view = ToolViewFactory().create(write_snapshot(14), state="success")
    assert live_view is not None
    live_renderer.append(live_view)
    live_rows = visible(live_renderer.document_lines())

    fake = FakeInput()
    screen = make_screen_with(
        tmp_path, fake, stored=history_session(write_snapshot(14))
    )
    asyncio.run(_render_once(screen, fake))
    rows = document(screen)

    assert [row for row in rows if "line" in row] == [
        row for row in live_rows if "line" in row
    ]
    assert "▌ 写入文档" in rows
    assert "写好了。" in rows
    assert "tool custom_tool · recorded" in rows


def test_history_without_snapshot_uses_recorded_records(tmp_path: Path) -> None:
    fake = FakeInput()
    screen = make_screen_with(tmp_path, fake, stored=history_session(None))
    asyncio.run(_render_once(screen, fake))
    rows = document(screen)

    assert "tool write · recorded" in rows
    assert "tool custom_tool · recorded" in rows
    assert not any("line 0" in row for row in rows)


def test_history_restores_turn_checkpoint_turn_from_entries(tmp_path: Path) -> None:
    now = datetime(2026, 8, 22, 3, 4, 5, tzinfo=UTC)
    first = TurnEntry(
        "turn-1",
        "run-1",
        Message(MessageRole.USER, "first prompt"),
        Message(MessageRole.ASSISTANT, "first answer"),
        now,
    )
    call_ok = ToolCall("call-ok", "read", {"path": "notes.md"})
    call_open = ToolCall("call-open", "write", {"path": "notes.md"})
    presentation = ToolPresentationSnapshot(
        "call-ok",
        "read",
        ToolPresentationKind.READ,
        ToolPresentationStatus.SUCCESS,
        {"path": "notes.md"},
    )
    checkpoint = RunCheckpoint(
        "checkpoint-1",
        "run-2",
        RunCheckpointKind.CANCELLED,
        Message(MessageRole.USER, "interrupted prompt"),
        now,
        progress=(
            RunCheckpointProgress(
                assistant_message=Message(MessageRole.ASSISTANT, "working"),
                tools=(
                    RunCheckpointToolProgress(
                        call_ok,
                        RunCheckpointToolStatus.SUCCESS,
                        Message(
                            MessageRole.TOOL,
                            '{"status":"success"}',
                            tool_call_id="call-ok",
                        ),
                        presentation,
                    ),
                    RunCheckpointToolProgress(
                        call_open, RunCheckpointToolStatus.IN_PROGRESS
                    ),
                ),
            ),
        ),
    )
    unfinished_text = RunCheckpoint(
        "checkpoint-2",
        "run-2b",
        RunCheckpointKind.CANCELLED,
        Message(MessageRole.USER, "streaming prompt"),
        now,
        partial_assistant_message=Message(MessageRole.ASSISTANT, "partial reply"),
    )
    empty_checkpoint = RunCheckpoint(
        "checkpoint-3",
        "run-2c",
        RunCheckpointKind.CANCELLED,
        Message(MessageRole.USER, "empty interrupt"),
        now,
    )
    last = TurnEntry(
        "turn-3",
        "run-3",
        Message(MessageRole.USER, "later prompt"),
        Message(MessageRole.ASSISTANT, "later answer"),
        now,
    )
    stored = SimpleNamespace(
        entries=(first, checkpoint, unfinished_text, empty_checkpoint, last),
        turns=(first, last),
    )
    fake = FakeInput()
    screen = make_screen_with(tmp_path, fake, stored=stored)
    asyncio.run(_render_once(screen, fake))
    rows = document(screen)
    first_user = rows.index("▌ first prompt")
    interrupted = rows.index("▌ interrupted prompt")
    streaming = rows.index("▌ streaming prompt")
    empty = rows.index("▌ empty interrupt")
    later = rows.index("▌ later prompt")
    assert first_user < interrupted < streaming < empty < later
    assert "first answer" in rows
    assert "later answer" in rows
    assert any("working" in row for row in rows)
    assert any("read" in row for row in rows)
    write_rows = [row for row in rows if "write" in row]
    assert write_rows
    assert all("notes.md" not in row for row in write_rows)
    assert all(
        "ok" not in row.lower() and "success" not in row.lower() for row in write_rows
    )
    assert any("partial reply" in row for row in rows)
    assert not any("partial reply" in row and STREAM_CURSOR in row for row in rows)
    incomplete_rows = [
        index for index, row in enumerate(rows) if CHECKPOINT_INCOMPLETE_MARKER in row
    ]
    assert len(incomplete_rows) == 3
    assert any(interrupted < index < streaming for index in incomplete_rows)
    assert any(streaming < index < empty for index in incomplete_rows)
    assert any(empty < index < later for index in incomplete_rows)
    assert not any("later answer" in row and STREAM_CURSOR in row for row in rows)


async def _render_once(screen: MainBufferScreen, fake: FakeInput) -> None:
    async with ScreenHarness(screen, fake) as harness:
        screen.render()
        assert harness.task is not None


# -- 诊断不泄漏正文 ---------------------------------------------------------


def test_repr_never_contains_body_text() -> None:
    secret = "super-secret-body"
    renderer = make_renderer()
    block = MarkdownBlock(f"正文 {secret}")
    renderer.append(block)
    renderer.append(UserBlock(secret))
    view = read_view()
    assert view is not None

    assert secret not in repr(renderer)
    assert secret not in repr(block)
    assert secret not in repr(ReasoningBlock(secret))
    assert secret not in repr(view)
    assert ENTER is not None


# -- P5-11B-MR3 视觉层级 -----------------------------------------------------


@pytest.mark.parametrize("width", [10, 40, 80, 110])
def test_user_block_fills_every_physical_row(width: int) -> None:
    block = UserBlock("短句\n第二行还要更长一些，并且包含中文路径 docs/设计.md")
    for palette in (Palette(False), Palette(True)):
        rows = block.render(width, palette, False)
        assert rows
        for row in rows:
            assert cell_len(strip_ansi(row)) == width
        first = strip_ansi(rows[0])
        assert first.startswith(USER_MARKER)
        if len(rows) > 1:
            assert not strip_ansi(rows[1]).startswith(USER_MARKER)
            assert strip_ansi(rows[1]).startswith(" ")


def test_user_markdown_markers_stay_plain_text() -> None:
    block = UserBlock("# 标题 `path/to/file.py` 和 **加粗**")
    text = "\n".join(visible(block.render(40, Palette(False), False)))
    assert "# 标题" in text
    assert "`path/to/file.py`" in text
    assert "**加粗**" in text


def test_adjacent_records_have_exactly_one_blank() -> None:
    renderer = make_renderer()
    renderer.append(UserBlock("用户"))
    renderer.append(ReasoningBlock("思考全文", ReasoningDisplayMode.FULL))
    renderer.append(MarkdownBlock("正式回复"))
    first = read_view()
    second = ToolViewFactory().create(
        ToolPresentationSnapshot(
            "call-write",
            "write",
            ToolPresentationKind.WRITE,
            ToolPresentationStatus.SUCCESS,
            {"path": "a.md", "content": "body"},
        ),
        state="success",
    )
    assert first is not None and second is not None
    renderer.append(first)
    renderer.append(second)
    renderer.append(MarkdownBlock("后续回复"))
    lines = visible(renderer.document_lines())
    labels = ["▌ 用户", "│ 思考全文", "正式回复", "read", "write", "后续回复"]
    indexes = [
        next(i for i, row in enumerate(lines) if label in row) for label in labels
    ]
    for index in range(1, len(indexes)):
        left, right = indexes[index - 1], indexes[index]
        assert left < right
        assert lines[right - 1] == ""
        if right >= 2:
            assert lines[right - 2] != ""
    assert "\n\n\n" not in "\n".join(lines)


def test_streaming_deltas_do_not_insert_extra_blanks() -> None:
    renderer = make_renderer()
    renderer.append(UserBlock("提问"))
    block = StreamingMarkdownBlock()
    renderer.set_pending(block)
    block.append("第一段。")
    first = visible(renderer.document_lines())
    block.append(" 第二段继续。")
    second = visible(renderer.document_lines())
    assert first.count("") == second.count("")
    user = second.index("▌ 提问")
    assert second[user + 1] == ""


def test_tool_in_place_update_keeps_spacing() -> None:
    renderer = make_renderer()
    renderer.append(UserBlock("提问"))
    pending = ToolPresentationSnapshot(
        "call-read",
        "read",
        ToolPresentationKind.READ,
        ToolPresentationStatus.PENDING,
        {"path": "docs/a.md"},
    )
    view = ToolViewFactory().create(pending, state="pending")
    assert view is not None
    renderer.set_pending(view)
    before = visible(renderer.document_lines())
    finished = ToolPresentationSnapshot(
        "call-read",
        "read",
        ToolPresentationKind.READ,
        ToolPresentationStatus.SUCCESS,
        {"path": "docs/a.md"},
    )
    terminal = ToolViewFactory().create(finished, state="success")
    assert terminal is not None
    renderer.set_pending(terminal)
    after = visible(renderer.document_lines())
    assert before.count("") == after.count("")
    user = after.index("▌ 提问")
    assert after[user + 1] == ""


def test_reasoning_uses_muted_dim_secondary_layer() -> None:
    rows = ReasoningBlock("次要思考").render(40, Palette(True), False)
    assert rows
    assert "2;38;2;148;163;184" in rows[0]
    assert "\x1b[48;" not in rows[0] and ";48;" not in rows[0]
    assert "次要思考" in strip_ansi(rows[0])


def test_collapsed_reasoning_ellipsis_fits_width() -> None:
    text = (
        "第一行足够长所以会占满可用宽度ABCDE\n"
        "第二行也足够长会被截断XYZ\n"
        "第三行保证折叠必须省略"
    )
    for width in (10, 16, 24, 40):
        rows = ReasoningBlock(text).render(width, Palette(True), False)
        assert 1 <= len(rows) <= 2
        for row in rows:
            assert cell_len(strip_ansi(row)) <= width
        if len(rows) == 2:
            assert strip_ansi(rows[1]).endswith("…")
            assert cell_len(strip_ansi(rows[1])) <= width


def test_hidden_reasoning_adds_no_body_or_blank() -> None:
    renderer = make_renderer()
    renderer.append(UserBlock("提问"))
    renderer.append(ReasoningBlock("隐藏思考", ReasoningDisplayMode.HIDDEN))
    renderer.append(MarkdownBlock("正式回复"))
    lines = visible(renderer.document_lines())
    assert not any("隐藏思考" in row for row in lines)
    user = lines.index("▌ 提问")
    reply = lines.index("正式回复")
    assert reply == user + 2
    assert lines[user + 1] == ""


def test_no_color_hierarchy_stays_readable() -> None:
    renderer = make_renderer()
    renderer.append(UserBlock("提问"))
    renderer.append(ReasoningBlock("思考", ReasoningDisplayMode.COLLAPSED))
    renderer.append(MarkdownBlock("# 标题\n\n- 列表 `code`"))
    view = read_view()
    assert view is not None
    renderer.append(view)
    lines = visible(renderer.document_lines())
    assert any(row.startswith("▌ ") for row in lines)
    assert any(row.startswith("│ ") for row in lines)
    assert any(row.startswith(" • ") or "标题" in row for row in lines)
    assert any("read" in row for row in lines)


def test_resize_and_expand_keep_single_blank_and_identities() -> None:
    renderer = make_renderer()
    renderer.append(UserBlock("提问"))
    renderer.append(MarkdownBlock("回复"))
    view = ToolViewFactory().create(write_snapshot(14), state="success")
    assert view is not None
    renderer.append(view)
    before = visible(renderer.document_lines())
    keys_before = [(slot.key, slot.base) for slot in renderer._slots]

    renderer.set_width(40)
    resized = visible(renderer.document_lines())
    renderer.toggle_expanded()
    expanded = visible(renderer.document_lines())
    renderer.toggle_expanded()
    collapsed = visible(renderer.document_lines())

    def blanks_around(lines: list[str], needle: str) -> tuple[str, str]:
        index = next(i for i, row in enumerate(lines) if needle in row)
        left = lines[index - 1] if index else None
        right = lines[index + 1] if index + 1 < len(lines) else None
        return left, right

    assert blanks_around(before, "▌ 提问")[1] == ""
    assert blanks_around(resized, "▌ 提问")[1] == ""
    assert blanks_around(expanded, "▌ 提问")[1] == ""
    assert blanks_around(collapsed, "回复")[1] == ""
    assert "\n\n\n" not in "\n".join(resized)
    assert "\n\n\n" not in "\n".join(expanded)
    assert [(slot.key, slot.base) for slot in renderer._slots] == keys_before
    assert before.count("▌ 提问") == resized.count("▌ 提问") == 1
