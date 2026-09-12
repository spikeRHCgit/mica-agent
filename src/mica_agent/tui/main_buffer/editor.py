"""composer 草稿、光标与按键解释。

编辑器只是输入状态的容器：它不绘制、不写 stdout、不导入 Textual，也不
知道 Run 或会话。按键解释把 prompt_toolkit 的 ``KeyPress`` 归一成稳定的
``Key``，使上层模式机可以在不依赖具体终端库的情况下测试。

草稿文本分两层：

* **显示文本**（``Buffer`` 里的内容）：长粘贴折叠为 ``[paste #N +M lines]``；
* **提交文本**（``submit_text()``）：把已登记的粘贴片段还原成完整原文。

长粘贴片段是不可拆分的编辑单元：光标整体跨过它，Backspace/Delete 一接触
就整体删除。片段身份由内部的登记表决定，因此手打一段同名摘要只是普通
文本，不会被当作真实粘贴。片段折叠阈值与摘要格式沿用既有 Textual
composer 的纯逻辑规则（``mica_agent.tui.paste``），但这里不导入该模块——
它位于 Textual 产品包内，导入会连带加载旧前端。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.keys import Keys

from .renderer import cell_len, expand_tabs, wrap_line
from .sanitize import sanitize_text

MAX_DRAFT_LINES = 8
PASTE_LINE_THRESHOLD = 9


@dataclass(frozen=True, slots=True)
class Key:
    """归一化后的按键。"""

    name: str
    data: str = ""

    @property
    def is_text(self) -> bool:
        return self.name == "text"


_KEY_NAMES: dict[Any, str] = {
    Keys.ControlM: "enter",
    Keys.ControlJ: "newline",
    Keys.Escape: "escape",
    Keys.Up: "up",
    Keys.Down: "down",
    Keys.Left: "left",
    Keys.Right: "right",
    Keys.Home: "home",
    Keys.End: "end",
    Keys.ControlH: "backspace",
    Keys.ControlI: "tab",
    Keys.Delete: "delete",
    Keys.ControlC: "ctrl_c",
    Keys.ControlO: "ctrl_o",
    Keys.ControlD: "ctrl_d",
    Keys.BracketedPaste: "paste",
}


def normalize_key(keypress: Any) -> Key:
    """把 prompt_toolkit ``KeyPress`` 归一为稳定按键。"""
    key = getattr(keypress, "key", None)
    data = getattr(keypress, "data", "") or ""
    if key in _KEY_NAMES:
        name = _KEY_NAMES[key]
        if name == "paste":
            return Key("paste", data.replace("\r\n", "\n").replace("\r", "\n"))
        return Key(name, data)
    if isinstance(key, str):
        if len(key) == 1 and key.isprintable():
            return Key("text", key)
        if data and data.isprintable():
            return Key("text", data)
    return Key("unknown", data)


def logical_line_count(text: str) -> int:
    """按换行边界计算内容行数；末尾换行不再额外计一行。"""
    normalized = text.replace("\r\n", "\n")
    if not normalized:
        return 0
    lines = normalized.split("\n")
    if normalized.endswith("\n"):
        lines = lines[:-1]
    return len(lines)


@dataclass(frozen=True, slots=True)
class PasteFragment:
    """一个不可展开的长粘贴片段。"""

    original: str
    line_count: int
    number: int

    @property
    def summary(self) -> str:
        return f"[paste #{self.number} +{self.line_count} lines]"


@dataclass(slots=True)
class _Span:
    start: int
    end: int
    fragment: PasteFragment

    def shifted(self, at: int, delta: int) -> _Span:
        if self.start >= at:
            return _Span(self.start + delta, self.end + delta, self.fragment)
        return self


@dataclass(frozen=True, slots=True)
class ComposerView:
    """按终端列宽排版后的草稿视图。"""

    lines: list[str]
    cursor_row: int
    cursor_column: int


class ComposerEditor:
    """基于 prompt_toolkit ``Buffer`` 的草稿编辑器。"""

    def __init__(self) -> None:
        self._buffer = Buffer(multiline=True, enable_history_search=False)
        self._spans: list[_Span] = []
        self._next_number = 1

    # -- 只读视图 -----------------------------------------------------------

    @property
    def text(self) -> str:
        """显示文本（长粘贴为摘要形式）。"""
        return self._buffer.text

    @property
    def is_blank(self) -> bool:
        return not self._buffer.text.strip()

    @property
    def cursor(self) -> int:
        return self._buffer.document.cursor_position

    @property
    def fragment_summaries(self) -> list[str]:
        return [span.fragment.summary for span in self._spans]

    @property
    def lines(self) -> list[str]:
        return self._buffer.text.split("\n")

    @property
    def cursor_row(self) -> int:
        return self._buffer.document.cursor_position_row

    @property
    def cursor_column(self) -> int:
        """返回光标在当前逻辑行内的终端列偏移。"""
        return cell_len(self._buffer.document.current_line_before_cursor)

    def wrapped(self, width: int) -> ComposerView:
        """按终端列宽软换行，并给出光标所在的显示行与列。

        软换行只影响显示：``submit_text()`` 返回的仍是未换行的原文。
        制表符先按 :func:`expand_tabs` 确定性展开，因此软换行、光标列
        与显示层用同一套宽度计算。
        """
        text = expand_tabs(self._buffer.text)
        lines = wrap_line(text, width)
        before = expand_tabs(self._buffer.text[: self._buffer.document.cursor_position])
        before_lines = wrap_line(before, width)
        return ComposerView(
            lines=lines,
            cursor_row=len(before_lines) - 1,
            cursor_column=cell_len(before_lines[-1]),
        )

    def submit_text(self) -> str:
        """返回提交给 MicaApp 的完整原文（粘贴片段还原为原始正文）。"""
        if not self._spans:
            return self._buffer.text
        parts: list[str] = []
        last = 0
        for span in sorted(self._spans, key=lambda item: item.start):
            parts.append(self._buffer.text[last : span.start])
            parts.append(span.fragment.original)
            last = span.end
        parts.append(self._buffer.text[last:])
        return "".join(parts)

    # -- 编辑 ---------------------------------------------------------------

    def insert(self, text: str) -> None:
        """插入文本；达到阈值的长粘贴折叠为不可拆分的摘要片段。"""
        cleaned = sanitize_text(text, newlines=True)
        if not cleaned:
            return
        at = self._buffer.document.cursor_position
        fragment = self._fold(cleaned)
        payload = fragment.summary if fragment is not None else cleaned
        self._apply(
            self._buffer.text[:at] + payload + self._buffer.text[at:],
            at + len(payload),
            at,
            len(payload),
            fragment,
        )

    def backspace(self) -> None:
        at = self._buffer.document.cursor_position
        span = self._span_before(at)
        if span is not None:
            self._delete_span(span)
            return
        if at == 0:
            return
        text = self._buffer.text
        self._apply(text[: at - 1] + text[at:], at - 1, at - 1, -1, None)

    def delete(self) -> None:
        at = self._buffer.document.cursor_position
        span = self._span_after(at)
        if span is not None:
            self._delete_span(span)
            return
        text = self._buffer.text
        if at >= len(text):
            return
        self._apply(text[:at] + text[at + 1 :], at, at, -1, None)

    def move(self, name: str) -> None:
        document = self._buffer.document
        text = document.text
        position = document.cursor_position
        if name == "left":
            span = self._span_before(position)
            position = span.start if span is not None else max(0, position - 1)
        elif name == "right":
            span = self._span_after(position)
            position = span.end if span is not None else min(len(text), position + 1)
        elif name == "up":
            position = self._vertical(document, -1)
            position = self._snap(position, direction=-1)
        elif name == "down":
            position = self._vertical(document, 1)
            position = self._snap(position, direction=1)
        elif name == "home":
            position = position - document.cursor_position_col
        elif name == "end":
            position += len(document.current_line_after_cursor)
        self._set(text, position)

    def clear(self) -> None:
        """清空草稿、片段与编号。"""
        self._spans = []
        self._next_number = 1
        self._buffer.reset()

    # -- 片段维护 -----------------------------------------------------------

    def _fold(self, text: str) -> PasteFragment | None:
        count = logical_line_count(text)
        if count < PASTE_LINE_THRESHOLD:
            return None
        fragment = PasteFragment(text, count, self._next_number)
        self._next_number += 1
        return fragment

    def _span_before(self, position: int) -> _Span | None:
        for span in self._spans:
            if span.start < position <= span.end:
                return span
        return None

    def _span_after(self, position: int) -> _Span | None:
        for span in self._spans:
            if span.start <= position < span.end:
                return span
        return None

    def _inside_span(self, position: int) -> _Span | None:
        for span in self._spans:
            if span.start < position < span.end:
                return span
        return None

    def _snap(self, position: int, *, direction: int) -> int:
        span = self._inside_span(position)
        if span is None:
            return position
        return span.end if direction > 0 else span.start

    def _delete_span(self, span: _Span) -> None:
        current = self._buffer.text
        new_text = current[: span.start] + current[span.end :]
        removed = span.end - span.start
        self._spans = [item for item in self._spans if item is not span]
        self._apply(new_text, span.start, span.start, -removed, None)

    def _apply(
        self,
        text: str,
        cursor: int,
        at: int,
        delta: int,
        fragment: PasteFragment | None,
    ) -> None:
        if delta:
            self._spans = [span.shifted(at, delta) for span in self._spans]
        if fragment is not None:
            self._spans.append(_Span(at, at + len(fragment.summary), fragment))
        self._set(text, cursor)

    def _set(self, text: str, position: int) -> None:
        clamped = min(max(position, 0), len(text))
        self._buffer.set_document(
            Document(text, cursor_position=clamped), bypass_readonly=True
        )

    def _vertical(self, document: Document, direction: int) -> int:
        row = min(
            max(document.cursor_position_row + direction, 0), document.line_count - 1
        )
        line = document.lines[row]
        column = min(document.cursor_position_col, len(line))
        return document.translate_row_col_to_index(row, column)


def windowed_lines(
    lines: list[str],
    cursor_row: int,
    *,
    limit: int = MAX_DRAFT_LINES,
) -> tuple[list[str], int]:
    """返回围绕光标的可见草稿窗口与窗口起始行。"""
    if limit <= 0 or len(lines) <= limit:
        return list(lines), 0
    cursor = min(max(cursor_row, 0), len(lines) - 1)
    start = min(max(0, cursor - limit + 1), len(lines) - limit)
    return lines[start : start + limit], start


__all__ = [
    "MAX_DRAFT_LINES",
    "PASTE_LINE_THRESHOLD",
    "ComposerEditor",
    "ComposerView",
    "Key",
    "PasteFragment",
    "logical_line_count",
    "normalize_key",
    "windowed_lines",
]
