"""物理重绘状态机的回放测试。

这些测试把渲染器**实际发出的字节流**回放到最小的虚拟终端上，因此断言的
是物理结果：启动前的 shell 内容是否还在、帧的行数增减是否留下旧尾巴、
光标最后落在哪个单元格。帧高变化时，旧帧擦除和光标复位不能仅按新帧行数
计算，否则会破坏 sentinel。
"""

from __future__ import annotations

import io
import re

import pytest
from test_tui_main_buffer_vt import VirtualTerminal

from mica_agent.tui.main_buffer.renderer import MainBufferRenderer, Palette
from mica_agent.tui.main_buffer.terminal import (
    FORBIDDEN_SEQUENCES,
    Terminal,
    find_forbidden_sequences,
)

SHELL_LINES = ("shell one", "shell two", "$ run mica --preview")
CURSOR_UP = re.compile(r"\x1b\[\d*A")


def make_terminal(*, rows: int, cols: int, ansi: bool = False) -> Terminal:
    return Terminal(
        output=io.StringIO(),
        input_factory=lambda: None,  # type: ignore[arg-type]
        size=(cols, rows),
        interactive=True,
        environ={} if ansi else {"NO_COLOR": "1"},
    )


class Harness:
    """渲染器 + 虚拟终端 + 增量回放。"""

    def __init__(self, *, rows: int = 20, cols: int = 60, ansi: bool = False) -> None:
        self.rows = rows
        self.cols = cols
        self.terminal = make_terminal(rows=rows, cols=cols, ansi=ansi)
        self.renderer = MainBufferRenderer(
            self.terminal, width=cols, height=rows, palette=Palette(ansi)
        )
        self.vt = VirtualTerminal(rows=rows, cols=cols)
        self.vt.feed("\x1b[?7l")  # 与真实启动一致：关闭自动换行
        self._seen = 0
        self.frames: list[str] = []

    # -- 操作 ---------------------------------------------------------------

    def shell(self, *lines: str) -> Harness:
        self.vt.write_shell(*lines)
        return self

    def frame(self) -> Harness:
        """绘制一帧并把新增字节回放到虚拟终端。"""
        before = len(self.terminal.emitted)
        self.renderer.repaint()
        chunk = "".join(self.terminal.emitted[before:])
        self.frames.append(chunk)
        self._seen = len(self.terminal.emitted)
        self.vt.feed(chunk)
        return self

    def chrome(self, lines: list[str], cursor: tuple[int, int] | None) -> Harness:
        self.renderer.set_chrome(lines, cursor=cursor)
        return self.frame()

    def clear(self) -> Harness:
        before = len(self.terminal.emitted)
        self.renderer.clear_chrome()
        chunk = "".join(self.terminal.emitted[before:])
        self._seen = len(self.terminal.emitted)
        self.vt.feed(chunk)
        return self

    # -- 断言 ---------------------------------------------------------------

    def row_of(self, needle: str) -> int:
        return self.vt.row_index(needle)

    def assert_cursor_at(self, needle: str, column: int) -> None:
        row = self.vt.row_index(needle)
        assert row >= 0, self.vt.screen()
        assert self.vt.cursor_cell() == (row, column), (
            self.vt.cursor_cell(),
            self.vt.screen(),
        )

    def assert_safe(self) -> None:
        assert self.vt.violations == [], self.vt.violations
        assert find_forbidden_sequences(self.terminal.emitted_text()) == []
        for forbidden in FORBIDDEN_SEQUENCES:
            assert forbidden not in self.terminal.emitted_text()


def test_first_frame_does_not_move_up_or_touch_shell_content() -> None:
    harness = Harness().shell(*SHELL_LINES)
    sentinel = harness.vt.screen()[:3]
    harness.renderer.add_block("assistant", "hello from mica")
    harness.chrome(["Mica · working", "› hello", "facts"], (1, 3))

    head = harness.frames[0][: harness.frames[0].index("hello from mica")]
    assert head.startswith(chr(13) + chr(10))
    assert not CURSOR_UP.search(head)
    assert harness.vt.screen()[:3] == sentinel
    assert harness.row_of("hello from mica") > 2
    assert harness.row_of("› hello") > harness.row_of("hello from mica")
    harness.assert_cursor_at("› hello", 3)
    harness.assert_safe()


def test_identical_frames_never_creep_upwards() -> None:
    harness = Harness().shell(*SHELL_LINES)
    sentinel = harness.vt.screen()[:3]
    harness.renderer.add_block("assistant", "stable")
    harness.chrome(["status", "› hello", "facts"], (1, 3))
    top = harness.row_of("stable")

    for _ in range(4):
        harness.frame()

    assert harness.vt.screen()[:3] == sentinel
    assert harness.row_of("stable") == top
    assert harness.vt.scrollback == []
    harness.assert_cursor_at("› hello", 3)


def test_changed_frame_returns_to_the_first_changed_row() -> None:
    """内容变化时，渲染器回到「第一个变化的行」而不是向下开头。"""
    harness = Harness(cols=40).shell(*SHELL_LINES)
    harness.renderer.set_chrome(["status", "› hello", "facts"], cursor=(1, 3))
    harness.renderer.update_pending("assistant", "one")
    harness.frame()
    cursor_row = harness.renderer.frame_cursor_row

    before = len(harness.terminal.emitted)
    harness.renderer.update_pending("assistant", "changed")
    harness.frame()
    chunk = "".join(harness.terminal.emitted[before:])

    assert cursor_row == 1 + 1  # 1 行正文 + chrome 内的第 1 行
    assert chunk.startswith(f"\r\x1b[{cursor_row}A"), repr(chunk)
    assert harness.row_of("changed") == harness.row_of("status") - 1
    harness.assert_cursor_at("› hello", 3)
    harness.assert_safe()


def test_identical_frames_write_nothing() -> None:
    """完全相同的帧不产生任何字节：普通增量只重绘变化尾部。"""
    harness = Harness(cols=40).shell(*SHELL_LINES)
    harness.renderer.set_chrome(["status", "› hello", "facts"], cursor=(1, 3))
    harness.renderer.update_pending("assistant", "stable")
    harness.frame()

    before = len(harness.terminal.emitted)
    harness.frame()

    assert harness.terminal.emitted[before:] == []
    harness.assert_cursor_at("› hello", 3)


def test_pending_block_grows_from_one_row_to_many() -> None:
    harness = Harness(cols=40).shell(*SHELL_LINES)
    harness.renderer.set_chrome(["status", "› hello", "facts"], cursor=(1, 3))
    harness.renderer.update_pending("assistant", "alpha")
    harness.frame()
    top = harness.row_of("alpha")

    harness.renderer.update_pending("assistant", "alpha\nbravo\ncharlie\ndelta")
    harness.frame()

    assert harness.row_of("alpha") == top
    assert harness.row_of("bravo") == top + 1
    assert harness.row_of("charlie") == top + 2
    assert harness.row_of("delta") == top + 3
    assert harness.row_of("status") == top + 4
    assert harness.vt.screen()[:3] == list(SHELL_LINES)
    harness.assert_cursor_at("› hello", 3)


def test_shrinking_pending_leaves_no_old_tail() -> None:
    harness = Harness(cols=40).shell(*SHELL_LINES)
    harness.renderer.set_chrome(["status", "› hello", "facts"], cursor=(1, 3))
    harness.renderer.update_pending("assistant", "alpha\nbravo\ncharlie\ndelta\necho")
    harness.frame()
    harness.renderer.update_pending("assistant", "alpha")
    harness.frame()

    top = harness.row_of("alpha")
    assert harness.vt.screen()[top + 1] == "status"
    assert harness.row_of("echo") == -1
    assert harness.row_of("delta") == -1
    harness.assert_cursor_at("› hello", 3)


def test_composer_cursor_survives_repaints_in_the_middle_of_chrome() -> None:
    harness = Harness().shell(*SHELL_LINES)
    harness.renderer.add_block("assistant", "document")
    for _ in range(3):
        harness.chrome(["s1", "s2", "› hello", "s4", "facts"], (2, 3))
    harness.assert_cursor_at("› hello", 3)


def test_chrome_height_changes_leave_no_leftovers() -> None:
    harness = Harness().shell(*SHELL_LINES)
    sentinel = harness.vt.screen()[:3]
    harness.renderer.add_block("assistant", "document")
    harness.chrome(["c1", "c2", "› hello"], (2, 3))
    top = harness.row_of("document")
    assert harness.row_of("c1") == top + 1

    harness.chrome(["b1", "b2", "b3", "b4", "› hello"], (4, 3))
    assert harness.row_of("b4") == top + 1 + 3
    harness.assert_cursor_at("› hello", 3)

    harness.chrome(["› hello"], (0, 3))
    assert harness.row_of("document") == top
    assert harness.row_of("b4") == -1
    assert harness.row_of("c2") == -1
    assert harness.vt.screen()[top + 1] == "› hello"
    assert harness.vt.screen()[:3] == sentinel
    harness.assert_cursor_at("› hello", 3)
    harness.assert_safe()


def test_clear_chrome_keeps_document_and_lands_on_a_clean_row() -> None:
    harness = Harness().shell(*SHELL_LINES)
    harness.renderer.add_block("assistant", "keep me")
    harness.chrome(["status", "› hello", "facts"], (1, 3))
    harness.renderer.commit_pending()
    harness.clear()

    assert "keep me" in harness.vt.all_lines()
    assert harness.row_of("status") == -1
    assert harness.row_of("facts") == -1
    row = harness.row_of("keep me")
    assert harness.vt.screen()[row + 1] == ""
    assert harness.vt.cursor_cell() == (row + 1, 0)
    assert harness.vt.screen()[:3] == list(SHELL_LINES)
    harness.assert_safe()


def test_wide_document_stays_in_scrollback_when_frame_is_taller_than_screen() -> None:
    harness = Harness(rows=8, cols=40).shell(*SHELL_LINES)
    harness.renderer.add_block(
        "assistant", "\n".join(f"row-{index}" for index in range(20))
    )
    harness.chrome(["status", "› hello", "facts"], (1, 3))

    combined = harness.vt.all_lines()
    assert "row-0" in combined
    assert "row-19" in combined
    assert harness.vt.scrollback[:3] == list(SHELL_LINES)
    assert harness.row_of("› hello") >= 0
    harness.assert_safe()


def test_width_change_reflows_only_the_repaint_region() -> None:
    harness = Harness(cols=60).shell(*SHELL_LINES)
    harness.renderer.update_pending("assistant", "abcdefghij")
    harness.chrome(["› hello"], (0, 3))
    assert harness.row_of("abcdefghij") >= 3

    harness.renderer.set_width(6)
    harness.chrome(["› hello"], (0, 3))
    assert harness.row_of("abcdefghij") == -1
    assert harness.row_of("abcdef") >= 3
    assert harness.vt.screen()[:3] == list(SHELL_LINES)
    harness.assert_safe()


def test_shrinking_height_never_erases_content_above_the_screen() -> None:
    harness = Harness(rows=14, cols=40).shell(*SHELL_LINES)
    harness.renderer.add_block("assistant", "body")
    harness.chrome(["status", "› hello", "facts"], (1, 3))
    harness.renderer.set_height(6)
    harness.frame()
    harness.assert_safe()
    assert harness.row_of("› hello") >= 0


def test_renderer_never_emits_forbidden_effects_at_any_step() -> None:
    harness = Harness(ansi=True).shell(*SHELL_LINES)
    harness.renderer.add_block("assistant", "正文" * 20)
    harness.chrome(["Mica · working", "› hello", "facts"], (1, 3))
    for _ in range(3):
        harness.renderer.update_pending("assistant", "streaming " * 6)
        harness.frame()
    harness.renderer.commit_pending()
    harness.clear()
    harness.assert_safe()


# 短帧后 pending 超高

OVERFLOW_BODY = [f"row-{index}" for index in range(20)]
BODY_PATTERN = re.compile(r"row-\d+")


def _overflow_scenario(*, ansi: bool, rows: int = 12) -> Harness:
    """首帧 pending 只有一行，第二帧把同一 pending 扩展到远超屏幕预算。"""
    harness = Harness(rows=rows, cols=40, ansi=ansi).shell(*SHELL_LINES)
    harness.renderer.set_chrome(["status", "› hello", "facts"], cursor=(1, 3))
    harness.renderer.update_pending("assistant", "one")
    harness.frame()
    harness.renderer.update_pending("assistant", chr(10).join(OVERFLOW_BODY))
    harness.frame()
    return harness


def _body_rows(harness: Harness) -> list[str]:
    return [line for line in harness.vt.all_lines() if BODY_PATTERN.fullmatch(line)]


def row_of(lines: list[str], needle: str) -> int:
    for index, line in enumerate(lines):
        if needle in line:
            return index
    return -1


def _assert_composer_not_overwritten(harness: Harness) -> None:
    composer_row = harness.vt.row_index("› hello")
    assert composer_row >= 0, harness.vt.screen()
    assert harness.vt.screen()[composer_row] == "› hello", harness.vt.screen()
    assert all("row-" not in line for line in harness.vt.screen()[composer_row:]), (
        harness.vt.screen()
    )


@pytest.mark.parametrize("ansi", [False, True])
def test_overlong_pending_never_writes_into_the_composer(ansi: bool) -> None:
    harness = _overflow_scenario(ansi=ansi)
    screen = harness.vt.screen()
    scrollback = harness.vt.scrollback

    # shell sentinel 完整保留，且仍位于全部输出的最上方
    assert harness.vt.all_lines()[:3] == list(SHELL_LINES)
    assert "one" not in harness.vt.all_lines()
    # 旧 chrome 不残留，新 chrome 只有一份（三行 chrome 各出现一次）
    chrome_lines = harness.vt.all_lines()
    assert chrome_lines.count("status") == 1
    assert chrome_lines.count("› hello") == 1
    assert chrome_lines.count("facts") == 1
    # 新正文顺序正确，不侵入 composer
    assert _body_rows(harness) == OVERFLOW_BODY
    _assert_composer_not_overwritten(harness)
    # 溢出正文自然进入 scrollback
    assert scrollback, (screen, scrollback)
    assert "row-0" in scrollback
    # 光标回到新 composer 的正确单元格
    harness.assert_cursor_at("› hello", 3)
    harness.assert_safe()


def test_overlong_pending_keeps_growing_into_scrollback() -> None:
    harness = _overflow_scenario(ansi=False)
    harness.renderer.update_pending(
        "assistant", chr(10).join([*OVERFLOW_BODY, *[f"more-{i}" for i in range(8)]])
    )
    harness.frame()
    lines = harness.vt.all_lines()

    assert harness.vt.all_lines()[:3] == list(SHELL_LINES)
    assert [line for line in lines if BODY_PATTERN.fullmatch(line)] == OVERFLOW_BODY
    assert [line for line in lines if line.startswith("more-")] == [
        f"more-{i}" for i in range(8)
    ]
    assert lines.index("row-0") < lines.index("more-7")
    assert lines.count("status") == 1
    _assert_composer_not_overwritten(harness)
    harness.assert_cursor_at("› hello", 3)
    harness.assert_safe()


def test_overlong_pending_shrink_leaves_no_remnant() -> None:
    harness = _overflow_scenario(ansi=False)
    harness.renderer.update_pending("assistant", "kept")
    harness.frame()
    lines = harness.vt.all_lines()
    screen = harness.vt.screen()
    composer_row = harness.row_of("› hello")
    # 屏高 12、chrome 3 → 预算 8：前 12 行已经永久写出，剩下的属于可重写尾部。
    released = OVERFLOW_BODY[: len(OVERFLOW_BODY) - 8]

    assert harness.vt.all_lines()[:3] == list(SHELL_LINES)
    # 已永久写出的正文留在历史里，且不重复
    assert _body_rows(harness) == released
    # 被收回的可重写尾部不再残留
    assert "row-19" not in lines
    # 新帧紧接在已收口正文之下，旧帧尾部被擦干净
    assert "kept" in screen
    assert row_of(screen, "status") == row_of(screen, "kept") + 1
    assert all("row-" not in line for line in screen[harness.row_of("kept") :])
    assert composer_row == harness.row_of("kept") + 2
    assert all(line == "" for line in screen[harness.row_of("facts") + 1 :])
    assert lines.count("status") == 1
    assert lines.count("› hello") == 1
    harness.assert_cursor_at("› hello", 3)
    harness.assert_safe()


def test_overlong_pending_with_changing_chrome_height() -> None:
    harness = _overflow_scenario(ansi=False)
    harness.chrome(["status", "› hello", "note", "facts"], (1, 3))
    assert all(
        "row-" not in line for line in harness.vt.screen()[harness.row_of("› hello") :]
    )

    harness.chrome(["› hello"], (0, 3))
    assert harness.vt.screen().count("status") == 0
    assert "facts" not in harness.vt.screen()
    assert harness.vt.screen()[harness.row_of("› hello") + 1] == ""
    assert _body_rows(harness) == OVERFLOW_BODY
    _assert_composer_not_overwritten(harness)
    harness.assert_cursor_at("› hello", 3)
    harness.assert_safe()


# 全局 Ctrl+O 的物理重排


def _write_view(lines: int):
    from mica_agent.tool_presentation import (
        ToolPresentationKind,
        ToolPresentationSnapshot,
        ToolPresentationStatus,
    )
    from mica_agent.tui.main_buffer.tool_views import ToolViewFactory

    content = chr(10).join(f"line {index}" for index in range(lines))
    snapshot = ToolPresentationSnapshot(
        "call-write",
        "write",
        ToolPresentationKind.WRITE,
        ToolPresentationStatus.SUCCESS,
        {"path": "docs/a.md", "content": content},
    )
    view = ToolViewFactory().create(snapshot, state="success")
    assert view is not None
    return view


def test_global_expansion_reflows_earlier_rows_in_the_main_buffer() -> None:
    """Ctrl+O 改变较早工具高度时从该处受控重排，不丢 sentinel 与草稿。"""
    harness = Harness(rows=30, cols=48).shell(*SHELL_LINES)
    sentinel = harness.vt.screen()[:3]
    harness.renderer.append(_write_view(14))
    harness.renderer.set_chrome(["status", "› hello", "facts"], cursor=(1, 3))
    harness.frame()
    # 再多写两帧，让工具块从「上一帧区域」退到更早的已写出行。
    harness.renderer.update_pending("assistant", "tail one")
    harness.frame()
    harness.renderer.update_pending("assistant", "tail one\ntail two")
    harness.frame()
    assert harness.row_of("line 13") == -1

    harness.renderer.toggle_expanded()
    harness.frame()

    assert harness.row_of("line 13") >= 0
    assert harness.vt.screen()[:3] == sentinel
    assert harness.vt.all_lines().count("line 9") == 1
    harness.assert_cursor_at("› hello", 3)
    harness.assert_safe()

    harness.renderer.toggle_expanded()
    harness.frame()

    assert harness.row_of("line 13") == -1
    assert harness.vt.all_lines().count("line 9") == 1
    harness.assert_cursor_at("› hello", 3)
    harness.assert_safe()


# cursor-only 帧


def _cursor_only_chunk(
    harness: Harness, lines: list[str], cursor: tuple[int, int]
) -> str:
    """只改 chrome 光标（文本逐字不变）并返回这一帧新写出的字节。"""
    before = len(harness.terminal.emitted)
    harness.renderer.set_chrome(lines, cursor=cursor)
    harness.frame()
    return "".join(harness.terminal.emitted[before:])


def test_cursor_only_frame_moves_the_physical_cursor_without_rewriting_rows() -> None:
    """文本完全相同的第二帧只移动光标：不写正文、不擦除、不重复物理行。"""
    harness = Harness().shell(*SHELL_LINES)
    sentinel = harness.vt.screen()[:3]
    harness.renderer.add_block("assistant", "document")
    chrome = ["status", "› hello", "facts"]
    harness.chrome(chrome, (1, 3))
    lines_before = harness.vt.all_lines()
    harness.assert_cursor_at("› hello", 3)

    # 只改光标列：新帧的物理行文本与上一帧逐字相同。
    chunk = _cursor_only_chunk(harness, chrome, (1, 8))
    assert "› hello" not in chunk, repr(chunk)
    assert "status" not in chunk and "facts" not in chunk, repr(chunk)
    assert "\x1b[J" not in chunk, repr(chunk)  # 不擦除
    assert "\r\n" not in chunk, repr(chunk)  # 不写行、不向下滚动
    harness.assert_cursor_at("› hello", 8)
    assert harness.vt.all_lines() == lines_before
    assert harness.vt.all_lines().count("› hello") == 1
    assert harness.vt.all_lines().count("status") == 1
    assert harness.vt.all_lines().count("facts") == 1
    assert harness.vt.screen()[:3] == sentinel
    assert harness.vt.scrollback == []

    # 只改光标行：向上、向下都必须立即生效。
    chunk = _cursor_only_chunk(harness, chrome, (0, 3))
    assert "\x1b[J" not in chunk and "\r\n" not in chunk, repr(chunk)
    harness.assert_cursor_at("status", 3)
    assert harness.vt.all_lines() == lines_before

    chunk = _cursor_only_chunk(harness, chrome, (2, 4))
    assert "\x1b[J" not in chunk and "\r\n" not in chunk, repr(chunk)
    harness.assert_cursor_at("facts", 4)
    assert harness.vt.all_lines() == lines_before
    assert harness.vt.screen()[:3] == sentinel

    # 文本与光标都没有变化时仍然是真正的 no-op。
    before = len(harness.terminal.emitted)
    harness.frame()
    assert harness.terminal.emitted[before:] == []
    harness.assert_cursor_at("facts", 4)
    harness.assert_safe()


def test_cursor_only_frames_keep_the_next_content_frame_in_sync() -> None:
    """连续 cursor-only 帧之后，内容帧仍回到正确的行并恢复光标。"""
    harness = Harness(cols=40).shell(*SHELL_LINES)
    chrome = ["status", "› hello", "facts"]
    harness.renderer.set_chrome(chrome, cursor=(2, 3))
    harness.renderer.update_pending("assistant", "one")
    harness.frame()
    physical_before = harness.renderer.physical_frame()
    cursor_row = harness.renderer.frame_cursor_row

    for row, column in ((1, 3), (0, 5), (2, 0), (1, 6)):
        chunk = _cursor_only_chunk(harness, chrome, (row, column))
        assert "\x1b[J" not in chunk and "\r\n" not in chunk, repr(chunk)
        harness.assert_cursor_at(chrome[row], column)
        # 只有光标移动：屏幕映射与物理行都没有被重排。
        assert harness.renderer.physical_frame() == physical_before
        assert harness.renderer.frame_cursor_row == cursor_row + (row - 2)

    # 下一次真正的文本变化仍然回到正确的行，并把光标放回最后一次的位置。
    harness.renderer.update_pending("assistant", "changed")
    harness.frame()

    assert harness.row_of("changed") == harness.row_of("status") - 1
    harness.assert_cursor_at("› hello", 6)
    harness.assert_safe()


def test_cursor_only_frame_survives_resize_and_a_scrolled_window() -> None:
    """composer 内部窗口已滚动、以及宽度变化重排之后，cursor-only 帧仍然成立。"""
    harness = Harness(rows=14, cols=40).shell(*SHELL_LINES)
    # 模拟已经滚动的 composer 内部窗口：第 5 行在最上面。
    window = ["status", "› line 5", "  line 6", "  line 7", "facts"]
    harness.chrome(window, (1, 3))
    harness.assert_cursor_at("› line 5", 3)

    chunk = _cursor_only_chunk(harness, window, (3, 4))
    assert "\x1b[J" not in chunk and "\r\n" not in chunk, repr(chunk)
    harness.assert_cursor_at("  line 7", 4)

    # 宽度变化让渲染器重排：这一帧是内容帧，之后的 cursor-only 帧仍必须有效。
    harness.renderer.set_width(24)
    harness.chrome(window, (1, 3))
    harness.assert_cursor_at("› line 5", 3)

    chunk = _cursor_only_chunk(harness, window, (2, 5))
    assert "\x1b[J" not in chunk and "\r\n" not in chunk, repr(chunk)
    harness.assert_cursor_at("  line 6", 5)
    harness.assert_safe()

    slot = harness.renderer.physical_frame()
    before = len(harness.terminal.emitted)
    harness.frame()
    assert harness.terminal.emitted[before:] == []
    assert harness.renderer.physical_frame() == slot
