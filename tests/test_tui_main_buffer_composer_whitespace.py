"""P6-11A-MR1-2 §4：composer 空白保留与 Tab 显示。

这些测试断言的是「草稿原文如何进入显示层」：前后空格与空白行不被丢掉，
制表符按 ``TAB_WIDTH`` 确定性展开，且软换行、显示文本与物理光标列用的是
同一套宽度计算。
"""

from __future__ import annotations

from mica_agent.tui.main_buffer.components import (
    composer_frame,
    composer_inner_width,
)
from mica_agent.tui.main_buffer.editor import ComposerEditor, windowed_lines
from mica_agent.tui.main_buffer.renderer import (
    TAB_WIDTH,
    Palette,
    cell_len,
    expand_tabs,
)
from mica_agent.tui.main_buffer.sanitize import sanitize_text

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
