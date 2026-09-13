"""主缓冲区测试用的最小虚拟终端（ANSI 回放器）。

它不是一个真终端，也不追求完整 VT 兼容：它只实现 ``main_buffer`` 实际
会发送的那一小部分序列，并把每一次字符写入都落到网格上，因此可以断言
**物理结果**（shell sentinel 是否还在、有没有旧尾巴、光标落在哪个单元格），
而不是只断言字符串包含关系。

安全扫描与产品侧共用 :func:`mica_agent.tui.main_buffer.terminal.find_forbidden_sequences`，
另外记录本回放器识别到的危险效果。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

from mica_agent.events import RunStarted, UserInputRequested
from mica_agent.interactions import AskOption, AskPrompt
from mica_agent.tui.main_buffer.renderer import cell_len
from mica_agent.tui.main_buffer.terminal import find_forbidden_sequences

_ESC = "\x1b"


class VirtualTerminal:
    """带 scrollback 的最小屏幕模型。"""

    def __init__(self, *, rows: int = 24, cols: int = 80) -> None:
        self.rows = rows
        self.cols = cols
        self.scrollback: list[str] = []
        self._grid: list[list[str]] = [[" "] * cols for _ in range(rows)]
        self.row = 0
        self.col = 0
        self.autowrap = True
        self.cursor_visible = True
        self.violations: list[str] = []

    # -- 读取 ---------------------------------------------------------------

    def screen(self) -> list[str]:
        """可见屏幕每一行的文本。"""
        return ["".join(row).rstrip() for row in self._grid]

    def all_lines(self) -> list[str]:
        """scrollback 加可见屏幕。"""
        return self.scrollback + self.screen()

    def row_index(self, needle: str) -> int:
        """返回包含 ``needle`` 的第一行在可见屏幕中的下标；找不到返回 -1。"""
        for index, line in enumerate(self.screen()):
            if needle in line:
                return index
        return -1

    def column_of(self, needle: str, *, row: int | None = None) -> int:
        lines = self.screen()
        targets = range(len(lines)) if row is None else [row]
        for index in targets:
            position = lines[index].find(needle)
            if position >= 0:
                return cell_len(lines[index][:position])
        return -1

    def cursor_cell(self) -> tuple[int, int]:
        return self.row, self.col

    # -- 预置内容 -----------------------------------------------------------

    def write_shell(self, *lines: str) -> None:
        """写入启动前的 shell 内容，模拟真实终端的既有输出。"""
        for line in lines:
            self.feed(line + "\r\n")

    # -- 回放 ---------------------------------------------------------------

    def feed(self, text: str) -> None:
        index = 0
        length = len(text)
        while index < length:
            character = text[index]
            if character == _ESC and index + 1 < length:
                index = self._escape(text, index + 1)
                continue
            if character == "\r":
                self.col = 0
            elif character == "\n":
                self._newline()
            elif character == "\t":
                self.col = min(self.cols - 1, self.col + 8 - (self.col % 8))
            elif character == "\b":
                self.col = max(0, self.col - 1)
            elif ord(character) < 0x20 or ord(character) == 0x7F:
                self.violations.append(f"C0 0x{ord(character):02x}")
            else:
                self._put(character)
            index += 1
        self.violations.extend(find_forbidden_sequences(text))

    # -- 内部 ---------------------------------------------------------------

    def _put(self, character: str) -> None:
        width = cell_len(character)
        if width == 0:
            # 组合字符：追加到前一格，不推进光标。
            if self.col > 0:
                self._grid[self.row][self.col - 1] += character
            return
        if self.col + width > self.cols:
            if self.autowrap:
                self._newline()
            else:
                self.col = max(0, self.cols - width)
        self._grid[self.row][self.col] = character
        for offset in range(1, width):
            self._grid[self.row][self.col + offset] = ""
        self.col = min(self.cols, self.col + width)

    def _newline(self) -> None:
        if self.row == self.rows - 1:
            self.scrollback.append("".join(self._grid[0]).rstrip())
            self._grid.pop(0)
            self._grid.append([" "] * self.cols)
        else:
            self.row += 1

    def _escape(self, text: str, index: int) -> int:
        introducer = text[index]
        if introducer == "[":
            return self._csi(text, index + 1)
        if introducer == "]":
            return self._osc(text, index + 1)
        if introducer in {"P", "X", "^", "_"}:
            self.violations.append(f"ESC {introducer}")
            return self._string_end(text, index + 1)
        if introducer == "c":
            self.violations.append("ESC c")
            return index + 1
        return index + 1

    def _csi(self, text: str, index: int) -> int:
        length = len(text)
        cursor = index
        while cursor < length and 0x20 <= ord(text[cursor]) <= 0x3F:
            cursor += 1
        params = text[index:cursor]
        final = text[cursor] if cursor < length else ""
        next_index = cursor + 1
        amount = self._amount(params.lstrip("?"))
        if final == "A":
            self.row = max(0, self.row - amount)
        elif final == "B":
            self.row = min(self.rows - 1, self.row + amount)
        elif final == "C":
            self.col = min(self.cols, self.col + amount)
        elif final == "D":
            self.col = max(0, self.col - amount)
        elif final in {"H", "f"}:
            parts = params.split(";")
            self.row = min(self.rows - 1, max(0, int(parts[0] or 1) - 1))
            self.col = (
                min(self.cols, max(0, int(parts[1] or 1) - 1)) if len(parts) > 1 else 0
            )
        elif final == "J":
            self._erase_display(params)
        elif final == "K":
            self._erase_line()
        elif final in {"h", "l"} and params.startswith("?"):
            self._set_mode(params[1:], final == "h")
        return next_index

    def _osc(self, text: str, index: int) -> int:
        end = self._string_end(text, index)
        body = text[index : end - 1]
        number = body.split(";", 1)[0]
        if number in {"0", "1", "2"}:
            self.violations.append("OSC title")
        elif number == "52":
            self.violations.append("OSC clipboard")
        return end

    def _string_end(self, text: str, index: int) -> int:
        length = len(text)
        while index < length:
            if text[index] == "\x07":
                return index + 1
            if text[index] == _ESC and index + 1 < length and text[index + 1] == "\\":
                return index + 2
            index += 1
        return length

    def _erase_display(self, params: str) -> None:
        if params in {"2", "3"}:
            self.violations.append(f"CSI {params}J")
            if params == "3":
                self.scrollback.clear()
            for row in range(self.rows):
                self._grid[row] = [" "] * self.cols
            return
        if params == "1":
            self.violations.append("CSI 1J")
            return
        for column in range(self.col, self.cols):
            self._grid[self.row][column] = " "
        for row in range(self.row + 1, self.rows):
            self._grid[row] = [" "] * self.cols

    def _erase_line(self) -> None:
        for column in range(self.col, self.cols):
            self._grid[self.row][column] = " "

    def _set_mode(self, params: str, enabled: bool) -> None:
        for part in params.split(";"):
            if part == "7":
                self.autowrap = enabled
            elif part == "25":
                self.cursor_visible = enabled
            elif part in {"1049", "47", "1047"}:
                self.violations.append(f"alternate screen {part}")
            elif part in {
                "1000",
                "1001",
                "1002",
                "1003",
                "1005",
                "1006",
                "1015",
                "1016",
            }:
                self.violations.append(f"mouse capture {part}")
            else:
                self.violations.append(f"private mode {part}")

    def _amount(self, params: str) -> int:
        first = params.split(";")[0]
        try:
            return max(1, int(first))
        except ValueError:
            return 1


def test_virtual_terminal_scrolls_and_keeps_scrollback() -> None:
    terminal = VirtualTerminal(rows=3, cols=10)
    terminal.write_shell("one", "two", "three", "four")
    assert terminal.scrollback == ["one", "two"]
    assert terminal.screen() == ["three", "four", ""]


def test_virtual_terminal_honours_cursor_moves_and_erase() -> None:
    terminal = VirtualTerminal(rows=4, cols=10)
    terminal.write_shell("keep", "old1", "old2")
    terminal.feed("\x1b[2A\r\x1b[Jok")
    assert terminal.screen()[0] == "keep"
    assert terminal.screen()[1] == "ok"
    assert terminal.screen()[2] == ""
    assert terminal.cursor_cell() == (1, 2)


def test_virtual_terminal_flags_dangerous_sequences() -> None:
    terminal = VirtualTerminal(rows=4, cols=10)
    terminal.feed("\x1b[?1049h\x1b[3J\x1b]0;title\x07\x1b[?1000h")
    assert "alternate screen 1049" in terminal.violations
    assert "CSI 3J" in terminal.violations
    assert "OSC title" in terminal.violations
    assert "mouse capture 1000" in terminal.violations


def test_virtual_terminal_keeps_wide_characters_in_one_row() -> None:
    terminal = VirtualTerminal(rows=2, cols=8)
    terminal.feed("中文\r\n")
    assert terminal.screen()[0] == "中文"
    assert terminal.cursor_cell() == (1, 0)


# -- P6-11C-MR2：超高交互面板在真实物理帧里的可见性 ---------------------------

RUN = "synth-run"
DOWN = KeyPress(Keys.Down, _ESC + "[B")
PAGE_UP = KeyPress(Keys.PageUp, _ESC + "[5~")
PAGE_DOWN = KeyPress(Keys.PageDown, _ESC + "[6~")


def tall_ask_prompt() -> AskPrompt:
    """远超 12 行终端可用高度的 ask 面板。"""
    return AskPrompt(
        "选择接下来要执行的操作。",
        title="后续任务",
        options=tuple(
            AskOption(f"option {index}", f"description {index} " * 24)
            for index in range(1, 5)
        ),
        allow_custom=True,
    )


def replay(terminal: object, *, rows: int, cols: int) -> VirtualTerminal:
    """把真实写出过的完整字节流回放到虚拟终端上。"""
    view = VirtualTerminal(rows=rows, cols=cols)
    view.feed(terminal.emitted_text())  # type: ignore[attr-defined]
    return view


def test_short_terminal_keeps_the_selected_ask_row_in_the_physical_frame(
    tmp_path: Path,
) -> None:
    """80×12 终端 + 超高 Ask 面板：当前选中行必须留在最终物理帧里。"""
    from test_tui_main_buffer_lifecycle import build
    from test_tui_main_buffer_screen import DOWN as DOWN_KEY
    from test_tui_main_buffer_screen import pump

    async def scenario() -> None:
        env = build(tmp_path, size=(80, 12))
        task = asyncio.create_task(env.screen.run())
        await pump(lambda: env.fake.is_attached, message="attach")
        try:
            await env.screen.handle_event(RunStarted(RUN))
            await env.screen.handle_event(
                UserInputRequested(
                    RUN, 1, "ask-1", "选择接下来要执行的操作。", tall_ask_prompt()
                )
            )
            interaction = env.screen.interaction
            assert interaction is not None

            for expected, marker in ((0, "› 1. option 1"), (2, "› 3. option 3")):
                if interaction.selected != expected:
                    env.fake.feed(DOWN_KEY, DOWN_KEY)
                    await pump(
                        lambda expected=expected: interaction.selected == expected,
                        message=marker,
                    )
                env.screen.render()
                view = replay(env.terminal, rows=12, cols=80)
                assert view.row_index(marker) >= 0, view.screen()
                assert view.violations == []

            # 最后一个可选行就是自定义行，同样必须可见。
            env.fake.feed(DOWN_KEY, DOWN_KEY)
            await pump(lambda: interaction.is_custom_row, message="custom row")
            env.screen.render()
            view = replay(env.terminal, rows=12, cols=80)
            assert view.row_index("› Type something.") >= 0, view.screen()

            # 安全边界：不进入 alternate screen、不清 scrollback、不捕获鼠标。
            assert find_forbidden_sequences(env.terminal.emitted_text()) == []
            assert view.autowrap is False
            assert view.violations == []
        finally:
            env.fake.feed(*[KeyPress(Keys.ControlC, "\x03")] * 2)
            await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(scenario())


def test_page_keys_do_not_scroll_or_move_the_selection(tmp_path: Path) -> None:
    """交互面板没有内部 transcript 滚动区：PageUp/PageDown 不改变选中行。"""
    from test_tui_main_buffer_lifecycle import build
    from test_tui_main_buffer_screen import DOWN as DOWN_KEY
    from test_tui_main_buffer_screen import pump

    async def scenario() -> None:
        env = build(tmp_path, size=(80, 12))
        task = asyncio.create_task(env.screen.run())
        await pump(lambda: env.fake.is_attached, message="attach")
        try:
            await env.screen.handle_event(RunStarted(RUN))
            await env.screen.handle_event(
                UserInputRequested(
                    RUN, 1, "ask-1", "选择接下来要执行的操作。", tall_ask_prompt()
                )
            )
            interaction = env.screen.interaction
            assert interaction is not None
            env.fake.feed(DOWN_KEY)
            await pump(lambda: interaction.selected == 1, message="down")

            routed: list[str] = []
            original = interaction.handle

            def counting_handle(key: object) -> object:
                routed.append(getattr(key, "name", ""))
                return original(key)  # type: ignore[arg-type]

            interaction.handle = counting_handle  # type: ignore[method-assign]
            env.fake.feed(PAGE_UP, PAGE_DOWN)
            await pump(lambda: len(routed) == 2, message="page keys routed")
            assert routed == ["unknown", "unknown"]
            assert interaction.selected == 1
            env.screen.render()
            view = replay(env.terminal, rows=12, cols=80)
            assert view.row_index("› 2. option 2") >= 0
        finally:
            env.fake.feed(*[KeyPress(Keys.ControlC, "\x03")] * 2)
            await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(scenario())


def test_resize_keeps_the_selected_ask_row_visible(tmp_path: Path) -> None:
    from test_tui_main_buffer_lifecycle import build
    from test_tui_main_buffer_screen import DOWN as DOWN_KEY
    from test_tui_main_buffer_screen import pump

    async def scenario() -> None:
        env = build(tmp_path, size=(80, 12))
        task = asyncio.create_task(env.screen.run())
        await pump(lambda: env.fake.is_attached, message="attach")
        try:
            await env.screen.handle_event(RunStarted(RUN))
            await env.screen.handle_event(
                UserInputRequested(
                    RUN, 1, "ask-1", "选择接下来要执行的操作。", tall_ask_prompt()
                )
            )
            interaction = env.screen.interaction
            assert interaction is not None
            env.fake.feed(DOWN_KEY, DOWN_KEY, DOWN_KEY)
            await pump(lambda: interaction.selected == 3, message="selection")

            env.terminal.set_size((80, 8))
            env.screen.handle_resize()
            view = replay(env.terminal, rows=8, cols=80)
            assert view.row_index("› 4. option 4") >= 0, view.all_lines()
            assert view.violations == []

            env.terminal.set_size((60, 10))
            env.screen.handle_resize()
            view = replay(env.terminal, rows=10, cols=60)
            assert any("› 4. option 4" in line for line in view.all_lines())
        finally:
            env.fake.feed(*[KeyPress(Keys.ControlC, "\x03")] * 2)
            await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(scenario())


def test_permission_panel_keeps_yes_no_visible_in_a_short_terminal(
    tmp_path: Path,
) -> None:
    from test_tui_main_buffer_lifecycle import build
    from test_tui_main_buffer_screen import pump

    from mica_agent.events import PermissionRequested

    async def scenario() -> None:
        env = build(tmp_path, size=(80, 12))
        task = asyncio.create_task(env.screen.run())
        await pump(lambda: env.fake.is_attached, message="attach")
        try:
            await env.screen.handle_event(RunStarted(RUN))
            await env.screen.handle_event(
                PermissionRequested(RUN, 1, "perm-1", "call-1", "bash", "run it")
            )
            assert env.screen.mode == "permission"
            env.screen.render()
            view = replay(env.terminal, rows=12, cols=80)
            assert view.row_index("› 1. Yes") >= 0, view.screen()

            env.fake.feed(DOWN)
            await pump(
                lambda: (
                    env.screen.interaction is not None
                    and env.screen.interaction.selected == 1
                ),
                message="deny selected",
            )
            env.screen.render()
            view = replay(env.terminal, rows=12, cols=80)
            assert view.row_index("› 2. No") >= 0, view.screen()
            assert view.violations == []
        finally:
            env.fake.feed(*[KeyPress(Keys.ControlC, "\x03")] * 2)
            await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(scenario())


def test_narrow_terminal_keeps_the_selected_row_after_a_rejection(
    tmp_path: Path,
) -> None:
    """极窄极矮终端 + 业务拒绝：物理帧仍标出当前选择，且不破坏主缓冲区。"""
    from test_tui_main_buffer_lifecycle import build
    from test_tui_main_buffer_screen import DOWN as DOWN_KEY
    from test_tui_main_buffer_screen import ENTER as ENTER_KEY
    from test_tui_main_buffer_screen import pump

    from mica_agent.events import PermissionRequested
    from mica_agent.tui.main_buffer.interactions import HINT_RESPONSE_REJECTED

    async def scenario() -> None:
        env = build(tmp_path, size=(30, 5))
        task = asyncio.create_task(env.screen.run())
        await pump(lambda: env.fake.is_attached, message="attach")
        try:
            await env.screen.handle_event(RunStarted(RUN))
            # 合成事件没有对应的 Broker 待答请求：提交一定会被业务侧拒绝。
            await env.screen.handle_event(
                PermissionRequested(RUN, 1, "perm-1", "call-1", "bash", "run it")
            )
            assert env.screen.mode == "permission"
            env.fake.feed(DOWN_KEY)
            await pump(
                lambda: (
                    env.screen.interaction is not None
                    and env.screen.interaction.selected == 1
                ),
                message="deny selected",
            )
            env.fake.feed(ENTER_KEY)
            await pump(
                lambda: (
                    env.screen.interaction is not None
                    and env.screen.interaction.hint == HINT_RESPONSE_REJECTED
                ),
                message="rejection hint",
            )

            # 屏幕自己的排版结果：锚点是合法下标，并且就是当前选中行。
            lines, _cursor, anchor = env.screen._build_chrome()
            assert anchor is not None
            assert 0 <= anchor < len(lines)
            assert lines[anchor].startswith("› 2. No")

            # 物理帧：当前选择标记必须真的落在终端画面上。
            view = replay(env.terminal, rows=5, cols=30)
            assert view.row_index("› 2. No") >= 0, view.screen()
            assert "提交未被接受" in "\n".join(view.all_lines())
            assert view.autowrap is False
            assert view.violations == []
            assert find_forbidden_sequences(env.terminal.emitted_text()) == []

            # PageUp/PageDown/未知功能键不滚动、不清提示、不移动选择。
            interaction = env.screen.interaction
            assert interaction is not None
            routed: list[str] = []
            original = interaction.handle

            def counting_handle(key: object) -> object:
                routed.append(getattr(key, "name", ""))
                return original(key)  # type: ignore[arg-type]

            interaction.handle = counting_handle  # type: ignore[method-assign]
            env.fake.feed(PAGE_UP, PAGE_DOWN, KeyPress(Keys.F5, _ESC + "[15~"))
            await pump(lambda: len(routed) == 3, message="unconsumed keys routed")
            assert routed == ["unknown", "unknown", "unknown"]
            assert interaction.selected == 1
            assert interaction.hint == HINT_RESPONSE_REJECTED
            env.screen.render()
            view = replay(env.terminal, rows=5, cols=30)
            assert view.row_index("› 2. No") >= 0, view.screen()
            assert "提交未被接受" in "\n".join(view.all_lines())
            assert view.violations == []

            # 换到 Yes 再被拒绝一次：极矮预算同样只能把锚点放在当前选择上。
            env.fake.feed(KeyPress(Keys.Up, _ESC + "[A"))
            await pump(lambda: interaction.selected == 0, message="allow selected")
            env.fake.feed(ENTER_KEY)
            await pump(
                lambda: interaction.hint == HINT_RESPONSE_REJECTED,
                message="second rejection",
            )
            lines, _cursor, anchor = env.screen._build_chrome()
            assert anchor is not None
            assert 0 <= anchor < len(lines)
            assert lines[anchor].startswith("› 1. Yes")
            env.screen.render()
            view = replay(env.terminal, rows=5, cols=30)
            assert view.row_index("› 1. Yes") >= 0, view.screen()
            assert view.violations == []
        finally:
            env.fake.feed(*[KeyPress(Keys.ControlC, "\x03")] * 2)
            await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(scenario())
