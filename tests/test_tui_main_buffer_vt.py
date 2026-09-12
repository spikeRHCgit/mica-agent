"""主缓冲区测试用的最小虚拟终端（ANSI 回放器）。

它不是一个真终端，也不追求完整 VT 兼容：它只实现 ``main_buffer`` 实际
会发送的那一小部分序列，并把每一次字符写入都落到网格上，因此可以断言
**物理结果**（shell sentinel 是否还在、有没有旧尾巴、光标落在哪个单元格），
而不是只断言字符串包含关系。

安全扫描与产品侧共用 :func:`mica_agent.tui.main_buffer.terminal.find_forbidden_sequences`，
另外记录本回放器识别到的危险效果。
"""

from __future__ import annotations

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
