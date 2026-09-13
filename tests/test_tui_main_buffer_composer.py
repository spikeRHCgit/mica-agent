"""composer 基础契约：软换行、八行窗口、长粘贴、空白保留与 ask 自定义项。

这些测试只走真实按键路径（``Key``/``KeyPress``），不直接写内部状态，
因此能证明界面上的自定义行真的可达。
"""

from __future__ import annotations

import re

from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

from mica_agent.interactions import AskOption, AskPrompt
from mica_agent.tui.main_buffer.components import (
    IDLE_HINT,
    composer_body_width,
    composer_frame,
    composer_inner_width,
    startup_blocks,
)
from mica_agent.tui.main_buffer.editor import (
    PASTE_LINE_THRESHOLD,
    ComposerEditor,
    Key,
    normalize_key,
    windowed_lines,
)
from mica_agent.tui.main_buffer.interactions import AskInteraction
from mica_agent.tui.main_buffer.renderer import (
    TAB_WIDTH,
    Block,
    Palette,
    cell_len,
    expand_tabs,
    wrap_blocks,
    wrap_line,
)
from mica_agent.tui.main_buffer.sanitize import sanitize_text

PASTE = "\n".join(f"pasted line {index}" for index in range(PASTE_LINE_THRESHOLD + 3))


def make_ask(*, allow_custom: bool = True) -> AskInteraction:
    prompt = AskPrompt(
        "Pick one",
        title="后续任务",
        options=(AskOption("Alice"), AskOption("Bob"), AskOption("Carol")),
        allow_custom=allow_custom,
    )
    return AskInteraction("i-1", prompt)


# -- 软换行与窗口 -----------------------------------------------------------


def test_soft_wrap_counts_cells_and_keeps_submit_text() -> None:
    editor = ComposerEditor()
    editor.insert("abcdefghij")
    view = editor.wrapped(4)
    assert view.lines == ["abcd", "efgh", "ij"]
    assert (view.cursor_row, view.cursor_column) == (2, 2)
    assert editor.submit_text() == "abcdefghij"


def test_soft_wrap_never_splits_wide_or_combining_characters() -> None:
    editor = ComposerEditor()
    editor.insert("中文字符")
    assert editor.wrapped(4).lines == ["中文", "字符"]
    assert editor.submit_text() == "中文字符"

    editor.clear()
    editor.insert("e\u0301x")
    assert editor.wrapped(1).lines == ["e\u0301", "x"]


def test_window_keeps_cursor_line_visible_and_reports_hidden_rows() -> None:
    editor = ComposerEditor()
    editor.insert("".join(f"{index:02d}" for index in range(60)))  # 120 列
    view = editor.wrapped(10)
    assert len(view.lines) == 12
    visible, start = windowed_lines(view.lines, view.cursor_row)
    assert len(visible) == 8
    assert view.cursor_row - start == len(visible) - 1
    assert start == 4

    frame = composer_frame(
        visible,
        view.cursor_row - start,
        view.cursor_column,
        width=30,
        palette=Palette(False),
        enabled=True,
        hidden_above=start,
        hidden_below=len(view.lines) - start - len(visible),
    )
    assert "↑ 4 more" in frame.lines[0]
    assert "↓ 0 more" not in frame.lines[-1]
    assert frame.cursor[0] <= len(frame.lines) - 2


def test_cursor_cannot_leave_the_composer_border() -> None:
    frame = composer_frame(
        ["a" * 60],
        0,
        60,
        width=30,
        palette=Palette(False),
        enabled=True,
    )
    inner = composer_body_width(30) + 4
    assert frame.cursor[1] <= inner
    assert cell_len(frame.lines[1]) == 30
    assert "Shift+Enter" not in IDLE_HINT
    assert "Ctrl+J" in IDLE_HINT


def test_physical_cursor_line_matches_soft_wrapped_line() -> None:
    editor = ComposerEditor()
    editor.insert("x" * 10)
    editor.move("home")
    for _ in range(6):
        editor.move("right")
    view = editor.wrapped(4)
    assert (view.cursor_row, view.cursor_column) == (1, 2)
    frame = composer_frame(
        view.lines,
        view.cursor_row,
        view.cursor_column,
        width=20,
        palette=Palette(False),
        enabled=True,
    )
    assert frame.cursor == (1 + 1, 1 + 2 + 2)


def test_move_and_delete_follow_the_submitted_position() -> None:
    editor = ComposerEditor()
    editor.insert("hello world")
    for _ in range(5):
        editor.move("left")
    editor.backspace()
    assert editor.submit_text() == "helloworld"
    editor.move("home")
    editor.delete()
    assert editor.submit_text() == "elloworld"
    editor.move("end")
    editor.insert("!")
    assert editor.submit_text() == "elloworld!"


# -- 长粘贴 -----------------------------------------------------------------


def test_long_paste_folds_into_one_atomic_fragment() -> None:
    editor = ComposerEditor()
    editor.insert(PASTE)
    expected = f"[paste #1 +{PASTE_LINE_THRESHOLD + 3} lines]"
    assert editor.text == expected
    assert editor.submit_text() == PASTE
    assert editor.fragment_summaries == [expected]


def test_short_paste_goes_in_as_plain_text() -> None:
    editor = ComposerEditor()
    editor.insert("one\ntwo")
    assert editor.text == "one\ntwo"
    assert editor.fragment_summaries == []


def test_fragment_is_traversed_and_deleted_as_one_unit() -> None:
    editor = ComposerEditor()
    editor.insert(PASTE)
    editor.move("home")
    editor.move("right")  # 整体跨过片段，不停在片段中间
    editor.backspace()  # 一接触就整体删除
    assert editor.text == ""
    assert editor.submit_text() == ""
    assert editor.fragment_summaries == []


def test_backspace_deletes_the_whole_fragment_once_touched() -> None:
    editor = ComposerEditor()
    editor.insert("head ")
    editor.insert(PASTE)
    assert editor.text.startswith("head [paste #1")
    editor.backspace()  # 光标紧贴片段末尾，一接触就整体删除
    assert editor.text == "head "
    assert editor.submit_text() == "head "
    assert editor.fragment_summaries == []
    editor.backspace()  # 之后才是普通字符删除
    assert editor.text == "head"


def test_delete_removes_the_whole_fragment_when_touched() -> None:
    editor = ComposerEditor()
    editor.insert(PASTE)
    editor.insert(" tail")
    editor.move("home")
    assert editor.text.startswith("[paste #1")
    editor.delete()
    assert editor.text == " tail"
    assert editor.submit_text() == " tail"
    assert editor.fragment_summaries == []


def test_typed_summary_is_plain_text_not_a_fragment() -> None:
    editor = ComposerEditor()
    editor.insert("[paste #1 +12 lines]")
    assert editor.fragment_summaries == []
    assert editor.submit_text() == "[paste #1 +12 lines]"


def test_paste_fragments_are_numbered_and_reset_on_clear() -> None:
    editor = ComposerEditor()
    editor.insert(PASTE)
    editor.insert(PASTE)
    assert editor.fragment_summaries == [
        f"[paste #1 +{PASTE_LINE_THRESHOLD + 3} lines]",
        f"[paste #2 +{PASTE_LINE_THRESHOLD + 3} lines]",
    ]
    editor.clear()
    editor.insert(PASTE)
    assert editor.fragment_summaries == [
        f"[paste #1 +{PASTE_LINE_THRESHOLD + 3} lines]"
    ]


def test_bracketed_paste_key_payload_is_sanitized_before_folding() -> None:
    editor = ComposerEditor()
    key = normalize_key(KeyPress(Keys.BracketedPaste, f"\x1b[?1049h\n{PASTE}"))
    editor.insert(key.data)
    assert "\x1b" not in editor.text
    assert "\x1b" not in editor.submit_text()
    assert "[?1049h" not in editor.submit_text()


# -- ask 自定义项 -----------------------------------------------------------


def test_arrow_keys_reach_the_custom_row_and_enter_starts_text_entry() -> None:
    ask = make_ask()
    assert ask.row_count == 4

    for _ in range(3):
        ask.handle(Key("down"))
    assert ask.is_custom_row

    assert ask.handle(Key("enter")).action == "none"
    assert ask.editing == "custom"

    for character in "hi":
        ask.handle(Key("text", character))
    outcome = ask.handle(Key("enter"))
    assert outcome.action == "response"
    assert outcome.response is not None
    assert outcome.response.answer == "hi"
    assert outcome.response.selected_option_index == 3


def test_arrow_wrap_includes_the_custom_row() -> None:
    ask = make_ask()
    ask.handle(Key("up"))
    assert ask.is_custom_row
    ask.handle(Key("down"))
    assert ask.selected == 0
    assert not ask.is_custom_row


def test_digit_keys_only_locate_numbered_options() -> None:
    ask = make_ask()
    ask.handle(Key("text", "9"))
    assert ask.selected == 0
    ask.handle(Key("text", "3"))
    assert ask.selected == 2
    assert not ask.is_custom_row


def test_notes_and_custom_answers_use_real_keys_only() -> None:
    ask = make_ask()
    ask.handle(Key("text", "n"))
    assert ask.editing == "notes"
    for character in "note":
        ask.handle(Key("text", character))
    ask.handle(Key("enter"))
    assert ask.editing == "options"

    outcome = ask.handle(Key("enter"))
    assert outcome.response is not None
    assert outcome.response.notes == "note"
    assert outcome.response.answer == "Alice"


def test_free_text_ask_without_options_still_works() -> None:
    ask = AskInteraction("i-2", AskPrompt("Type anything"))
    assert ask.row_count == 0
    for character in "yes":
        ask.handle(Key("text", character))
    outcome = ask.handle(Key("enter"))
    assert outcome.response is not None
    assert outcome.response.answer == "yes"


# -- startup 面板的 ANSI 排版顺序 -------------------------------------------


class _StartupSnapshot:
    system_prompt_name = "sys"
    global_agent_path = "/global/AGENTS.md"
    workspace_agent_path = "/ws/AGENTS.md"
    tool_names = ("read", "write")
    skill_names = ("ata",)
    extension_names = ("demo",)


def test_painted_blocks_are_always_marked_styled() -> None:
    for width in (110, 60, 40):
        for block in startup_blocks(_StartupSnapshot(), width, Palette(True)):
            if "\x1b" in block.text:
                assert block.styled, block.text


def test_plain_blocks_never_contain_ansi() -> None:
    for block in startup_blocks(_StartupSnapshot(), 110, Palette(False)):
        assert "\x1b" not in block.text


SGR = re.compile(r"\x1b\[[0-9;]*m")


def test_panel_is_measured_as_plain_text_then_painted() -> None:
    palette = Palette(True)
    for width in (110, 40):
        for block in startup_blocks(_StartupSnapshot(), width, palette):
            if block.styled:
                # 去掉样式序列后的可见宽度才是布局宽度。
                assert cell_len(SGR.sub("", block.text)) <= width
    assert len(wrap_line("token " * 12, 20)) > 1
    assert wrap_blocks([Block("text", "token " * 12)], 20) != ["token " * 12]


def test_renderer_never_wraps_styled_lines() -> None:
    painted = Palette(True).paint("x" * 60, "text")
    assert wrap_blocks([Block("raw", painted, styled=True)], 20) == [painted]


# -- 空白保留与 Tab 显示 -----------------------------------------------------

TAB = chr(9)
NL = chr(10)


def frame_for(
    text: str, *, width: int = 40, body: int = 20, cursor_at: int | None = None
):
    editor = ComposerEditor()
    editor.insert(text)
    if cursor_at is not None:
        editor.move("home")
        for _ in range(cursor_at):
            editor.move("right")
    view = editor.wrapped(body)
    visible, start = windowed_lines(view.lines, view.cursor_row)
    frame = composer_frame(
        visible,
        view.cursor_row - start,
        view.cursor_column,
        width=width,
        palette=Palette(False),
        enabled=True,
    )
    return editor, view, frame


def body_of(frame, row: int) -> str:
    return frame.lines[row][1:-1]


def test_leading_spaces_stay_visible_in_the_composer() -> None:
    editor, view, frame = frame_for("  indented")

    assert view.lines == ["  indented"]
    assert body_of(frame, 1).startswith("\u203a " + "  indented")
    assert body_of(frame, 1) == "\u203a " + "  indented" + " " * (
        composer_inner_width(40) - 2 - 10
    )
    assert editor.submit_text() == "  indented"


def test_trailing_spaces_still_occupy_cells() -> None:
    editor, view, frame = frame_for("tail  ", width=24, body=20)

    inner = composer_inner_width(24)
    # 显示层不 strip：正文按「› tail  」8 列测量，补白只补剩余的 14 列。
    assert body_of(frame, 1) == "\u203a tail  " + " " * (inner - 8)
    assert cell_len(body_of(frame, 1)) == inner
    assert sanitize_text("tail  ", newlines=False) == "tail  "
    # 尾随空格真的占单元格：光标位于它们之后（比无空格时右移两列）。
    assert view.cursor_column == 6
    assert frame.cursor == (1, 1 + 2 + 6)
    assert editor.move("end") is None
    assert editor.cursor == 6
    assert editor.submit_text() == "tail  "


def test_blank_middle_line_is_preserved() -> None:
    editor, view, frame = frame_for("first" + NL + NL + "third", width=24, body=20)

    inner = composer_inner_width(24)
    assert view.lines == ["first", "", "third"]
    assert body_of(frame, 2) == " " * inner
    assert body_of(frame, 3).startswith("  third")
    assert len(frame.lines) == 5
    assert editor.submit_text() == "first" + NL + NL + "third"


def test_tab_expands_to_the_same_cells_as_the_cursor() -> None:
    editor, view, frame = frame_for(TAB + "value")

    assert expand_tabs(TAB) == " " * TAB_WIDTH
    assert view.lines == [" " * TAB_WIDTH + "value"]
    assert (view.cursor_row, view.cursor_column) == (0, TAB_WIDTH + 5)
    body = body_of(frame, 1)
    assert body.startswith("\u203a " + " " * TAB_WIDTH + "value")
    assert frame.cursor == (1, 1 + 2 + TAB_WIDTH + 5)
    # 显示位置与光标一致：光标左侧的正文列数 = 提示宽 + 光标列。
    assert cell_len(body[: frame.cursor[1] - 1]) == 2 + TAB_WIDTH + 5
    assert editor.submit_text() == TAB + "value"


def test_tab_after_text_keeps_display_and_cursor_in_step() -> None:
    editor, view, frame = frame_for("ab" + TAB + "cd", cursor_at=3)

    assert view.lines == ["ab" + " " * TAB_WIDTH + "cd"]
    assert (view.cursor_row, view.cursor_column) == (0, 2 + TAB_WIDTH)
    body = body_of(frame, 1)
    assert body.startswith("\u203a ab" + " " * TAB_WIDTH + "cd")
    assert frame.cursor == (1, 1 + 2 + 2 + TAB_WIDTH)
    assert cell_len(body[: frame.cursor[1] - 1]) == 2 + 2 + TAB_WIDTH
    assert editor.submit_text() == "ab" + TAB + "cd"


def test_tab_soft_wrap_keeps_the_cursor_on_the_wrapped_line() -> None:
    editor, view, frame = frame_for("abc" + TAB + "zz", width=20, body=5)

    inner = composer_inner_width(20)
    assert view.lines == ["abc  ", "  zz"]
    assert (view.cursor_row, view.cursor_column) == (1, 4)
    assert body_of(frame, 2) == ("  " + "  zz").ljust(inner)
    assert frame.cursor == (2, 1 + 2 + 4)
    assert frame.cursor[0] == 2
    assert editor.submit_text() == "abc" + TAB + "zz"


def test_submit_text_keeps_the_real_tab() -> None:
    editor = ComposerEditor()
    editor.insert(TAB + "indent")
    editor.move("end")
    editor.insert(TAB)

    assert editor.submit_text() == TAB + "indent" + TAB
    assert editor.wrapped(40).lines == [" " * TAB_WIDTH + "indent" + " " * TAB_WIDTH]
    # 窗口与软换行共用展开后的显示文本，不把制表符写进物理终端。
    assert expand_tabs(editor.wrapped(40).lines[0]) == editor.wrapped(40).lines[0]
