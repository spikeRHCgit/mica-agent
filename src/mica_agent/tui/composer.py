"""Mica TUI 的可编辑输入与提交边界。"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from typing import ClassVar

from rich.segment import Segment
from rich.style import Style
from textual.binding import Binding
from textual.events import MouseDown, MouseMove, Paste, Resize
from textual.message import Message
from textual.strip import Strip
from textual.widgets import TextArea

from .paste import PasteFragment, make_paste_fragment
from .theme import MUTED

# TextArea 用 (row, col) 作为光标/选区位置，等价于 `textual.widgets._text_area` 的
# `Location`；这里用本地别名避免依赖私有模块。
Location = tuple[int, int]


@dataclass(slots=True)
class _PasteSpan:
    """记录一个长粘贴片段在当前文档索引空间中的位置。"""

    fragment: PasteFragment
    start: int
    """片段在文档线性索引中的起始位置。"""
    end: int
    """片段在文档线性索引中的结束位置（不含）。"""

    @property
    def summary(self) -> str:
        return self.fragment.summary


class MicaComposer(TextArea):
    """支持软换行，并以消息提交原始输入快照。"""

    BINDINGS: ClassVar = [
        *TextArea.BINDINGS,
        Binding("enter", "submit", show=False, priority=True),
        Binding("shift+enter", "insert_newline", show=False, priority=True),
    ]

    class Submitted(Message):
        """一次有效 Composer 提交，同时提供完整原文与紧凑显示快照。"""

        __slots__ = ("_display_text", "_text")

        def __init__(self, text: str, *, display_text: str | None = None) -> None:
            super().__init__()
            self._text = text
            self._display_text = text if display_text is None else display_text

        @property
        def text(self) -> str:
            """恢复所有粘贴片段后的完整原始文本。"""
            return self._text

        @property
        def display_text(self) -> str:
            """Composer 当前看到的紧凑显示文本，长粘贴保留摘要。"""
            return self._display_text

    def __init__(self, *, id: str | None = None) -> None:
        self._paste_spans: list[_PasteSpan] = []
        self._next_paste_number = 1
        self._pending_paste: tuple[PasteFragment, int] | None = None
        self._normalizing_selection = False
        self._editing = False
        super().__init__(id=id, soft_wrap=True, show_line_numbers=False)

    @property
    def display_fragments(self) -> tuple[PasteFragment, ...]:
        """当前草稿中已折叠的长粘贴片段（按文档顺序）。"""
        return tuple(
            span.fragment
            for span in sorted(self._paste_spans, key=lambda span: span.start)
        )

    def clear(self) -> object:
        """清空草稿并同时丢弃片段身份、原文引用和编号状态。"""
        result = super().clear()
        self._reset_paste_state()
        return result

    def _reset_paste_state(self) -> None:
        """清理当前草稿的全部粘贴状态。"""
        self._paste_spans.clear()
        self._pending_paste = None
        self._next_paste_number = 1

    def on_mount(self) -> None:
        super().on_mount()
        self._sync_max_height()
        self.focus()

    def on_resize(self, event: Resize) -> None:
        self._sync_max_height()

    def _sync_max_height(self) -> None:
        """按可用高度限制内容行，保证 transcript 至少一行可见。"""
        if self.app is None:
            return
        # 上部保留 work-status 一行和 transcript 至少一行可见区域。
        max_chrome_height = self.app.size.height - 2
        target = min(10, max(3, max_chrome_height))
        if self.styles.max_height != target:
            self.styles.max_height = target

    @property
    def _base_hidden_above(self) -> int:
        return int(self.scroll_y)

    @property
    def _base_hidden_below(self) -> int:
        return max(
            0,
            self.virtual_size.height
            - self._base_hidden_above
            - self.content_region.height,
        )

    @property
    def hidden_above(self) -> int:
        """当前滚动位置上方不可见的显示行数。"""
        return self._base_hidden_above

    @property
    def hidden_below(self) -> int:
        """当前滚动位置下方不可见的显示行数。"""
        return self._base_hidden_below

    @property
    def hint_above(self) -> str:
        """上方被隐藏内容时显示 \u2191 N more。"""
        return f"\u2191 {self.hidden_above} more" if self.hidden_above else ""

    @property
    def hint_below(self) -> str:
        """下方未显示内容时显示 \u2193 N more。"""
        return f"\u2193 {self.hidden_below} more" if self.hidden_below else ""

    def render_line(self, y: int) -> Strip:
        """在正文行上叠加溢出提示，不整行吞掉正文。"""
        strip = super().render_line(y)
        content_top = self.content_region.y - self.region.y
        content_height = self.content_region.height
        relative = y - content_top
        if not 0 <= relative < content_height:
            return strip

        hints: list[str] = []
        if relative == 0 and self.hint_above:
            hints.append(self.hint_above)
        if relative == content_height - 1 and self.hint_below:
            hints.append(self.hint_below)
        if not hints:
            return strip

        hint_text = " ".join(hints)
        hint_style = Style(color=MUTED, bold=True)
        hint_cells = min(strip.cell_length, len(hint_text))
        body_cells = max(0, strip.cell_length - hint_cells - 1)
        body = strip.crop_extend(0, body_cells, hint_style)
        gap = Strip([Segment(" ", hint_style)]) if body_cells else Strip([])
        hint = Strip([Segment(hint_text[:hint_cells], hint_style)])
        return Strip.join([body, gap, hint]).crop_extend(
            0, strip.cell_length, hint_style
        )

    def _on_paste(self, event: Paste) -> None:
        """拦截 bracketed paste：长粘贴折叠为原子片段，短粘贴按普通文本插入。

        该方法在 TextArea 基类 `_on_paste` 之前被调度。对长粘贴，先把
        `event.text` 替换为摘要占位文本；基类随后把（可能是折叠后的）文本插入
        文档一次。插入完成后，用插入前光标位置定位片段边界。
        """
        if self.read_only:
            return
        fragment = make_paste_fragment(event.text, self._next_paste_number)
        if fragment is None:
            return
        # 记录插入前的光标位置，基类将在此处插入折叠摘要。
        start = self.selection[0]
        before_cursor = self.document.get_index_from_location(start)
        event.text = fragment.summary
        # 基类接下来会在 before_cursor 处插入摘要文本；edit() 会据此定位片段。
        self._pending_paste = (fragment, before_cursor)

    def edit(self, edit: object) -> object:
        """在文本变更前扩展原子片段，变更后同步其边界。"""
        old_start = self.document.get_index_from_location(edit.from_location)
        old_end = self.document.get_index_from_location(edit.to_location)
        old_spans = list(self._paste_spans)
        edit_start, edit_end = self._expand_edit_range(old_start, old_end)
        if (edit_start, edit_end) != (old_start, old_end):
            edit = dataclass_replace(
                edit,
                from_location=self.document.get_location_from_index(edit_start),
                to_location=self.document.get_location_from_index(edit_end),
            )
            old_start, old_end = edit_start, edit_end
        insert_len = len(edit.text)
        delta = insert_len - (old_end - old_start)
        self._editing = True
        try:
            result = super().edit(edit)
        finally:
            self._editing = False
        self._adjust_spans(old_spans, old_start, old_end, delta)
        self._finalize_pending_paste(old_start, insert_len)
        if not self.text:
            self._reset_paste_state()
        self._normalize_selection()
        return result

    def _finalize_pending_paste(self, old_start: int, insert_len: int) -> None:
        """基类插入折叠摘要后，把片段边界记录到 _paste_spans。"""
        pending = self._pending_paste
        if pending is None:
            return
        fragment, _before_cursor = pending
        self._pending_paste = None
        # old_start 已包含可能被原子替换的旧片段边界。
        self._paste_spans.append(
            _PasteSpan(
                fragment=fragment,
                start=old_start,
                end=old_start + insert_len,
            )
        )
        self._next_paste_number += 1

    def _expand_edit_range(self, start: int, end: int) -> tuple[int, int]:
        """把接触片段的编辑范围扩展为完整片段。"""
        lo, hi = sorted((start, end))
        changed = True
        while changed:
            changed = False
            for span in self._paste_spans:
                touches = (span.start < hi and span.end > lo) or (
                    lo == hi and span.start < lo < span.end
                )
                if touches:
                    new_range = min(lo, span.start), max(hi, span.end)
                    if new_range != (lo, hi):
                        changed = True
                        lo, hi = new_range
        return lo, hi

    def _adjust_spans(
        self,
        old_spans: list[_PasteSpan],
        old_start: int,
        old_end: int,
        delta: int,
    ) -> None:
        """根据一次文本编辑，调整仍存活片段的位置，剔除被删除的片段。"""
        updated: list[_PasteSpan] = []
        for span in old_spans:
            if span.end <= old_start:
                # 片段完全位于编辑范围之前，不移动。
                updated.append(span)
            elif span.start >= old_end:
                # 片段完全位于编辑范围之后，整体平移 delta。
                updated.append(
                    _PasteSpan(
                        fragment=span.fragment,
                        start=span.start + delta,
                        end=span.end + delta,
                    )
                )
            # 接触范围已在 edit() 前扩展到片段整体；被触碰的身份直接移除。
        self._paste_spans = updated

    # ------------------------------------------------------------------ #
    # 粘贴片段的原子性：光标整体跨过，删除整体移除。
    # ------------------------------------------------------------------ #

    def _span_at_index(self, index: int) -> _PasteSpan | None:
        """返回严格包含该线性索引的片段，若索引正好落在边界则不判为内部。"""
        for span in self._paste_spans:
            if span.start < index < span.end:
                return span
        return None

    def _clamp_cursor_index(self, index: int, direction: int) -> int:
        """把光标线性索引钳出所有片段内部。

        ``direction`` 取 -1（向左）或 +1（向右）：向左落在片段内部时退回片段
        起点，向右落在片段内部时推到片段终点，从而整体跨过片段。
        """
        span = self._span_at_index(index)
        if span is None:
            return index
        return span.start if direction < 0 else span.end

    def _clamp_cursor_location(self, location: Location, direction: int) -> Location:
        """把光标位置钳出片段内部。"""
        index = self.document.get_index_from_location(location)
        clamped = self._clamp_cursor_index(index, direction)
        if clamped == index:
            return location
        return self.document.get_location_from_index(clamped)

    def _normalize_selection(self) -> None:
        """把公开定位或鼠标产生的选区扩展到完整片段边界。"""
        if (
            self._normalizing_selection
            or self._editing
            or not hasattr(self, "_paste_spans")
        ):
            return
        selection = self.selection
        document_length = len(self.document.text)
        try:
            start_index = self.document.get_index_from_location(selection.start)
        except (IndexError, ValueError):
            start_index = document_length
        try:
            end_index = self.document.get_index_from_location(selection.end)
        except (IndexError, ValueError):
            end_index = document_length
        start_index = max(0, min(document_length, start_index))
        end_index = max(0, min(document_length, end_index))
        lo, hi = sorted((start_index, end_index))
        changed = True
        while changed:
            changed = False
            for span in self._paste_spans:
                touches = (span.start < hi and span.end > lo) or (
                    lo == hi and span.start < lo < span.end
                )
                if touches:
                    expanded = min(lo, span.start), max(hi, span.end)
                    if expanded != (lo, hi):
                        changed = True
                        lo, hi = expanded
        if start_index == end_index:
            normalized_start = normalized_end = lo
        elif start_index <= end_index:
            normalized_start, normalized_end = lo, hi
        else:
            normalized_start, normalized_end = hi, lo
        normalized = type(selection)(
            self.document.get_location_from_index(normalized_start),
            self.document.get_location_from_index(normalized_end),
        )
        if normalized != selection:
            self._normalizing_selection = True
            try:
                self.selection = normalized
            finally:
                self._normalizing_selection = False

    def watch_selection(self, old: object, new: object) -> None:
        self._normalize_selection()

    def move_cursor(
        self,
        location: Location,
        select: bool = False,
        center: bool = False,
        record_width: bool = True,
    ) -> None:
        """公开定位入口也必须遵守片段边界。"""
        if select and not self.selection.is_empty:
            current = self.document.get_index_from_location(self.cursor_location)
            target = self.document.get_index_from_location(location)
            direction = -1 if target < current else 1
        else:
            direction = 1 if select else -1
        location = self._clamp_cursor_location(location, direction)
        super().move_cursor(
            location,
            select=select,
            center=center,
            record_width=record_width,
        )
        self._normalize_selection()

    def _on_mouse_down(self, event: MouseDown) -> None:
        super()._on_mouse_down(event)
        self._normalize_selection()

    async def _on_mouse_move(self, event: MouseMove) -> None:
        await super()._on_mouse_move(event)
        self._normalize_selection()

    def action_cursor_left(self, select: bool = False) -> None:
        if not self._has_cursor:
            self.scroll_left()
            return
        target = (
            self.get_cursor_left_location()
            if select or self.selection.is_empty
            else min(*self.selection)
        )
        self.move_cursor(self._clamp_cursor_location(target, -1), select=select)

    def action_cursor_right(self, select: bool = False) -> None:
        if not self._has_cursor:
            self.scroll_right()
            return
        if self.suggestion:
            self.insert(self.suggestion)
            return
        target = (
            self.get_cursor_right_location()
            if select or self.selection.is_empty
            else max(*self.selection)
        )
        self.move_cursor(self._clamp_cursor_location(target, 1), select=select)

    def action_cursor_word_left(self, select: bool = False) -> None:
        if not self.show_cursor:
            return
        if self.cursor_at_start_of_text:
            return
        target = self.get_cursor_word_left_location()
        self.move_cursor(self._clamp_cursor_location(target, -1), select=select)

    def action_cursor_word_right(self, select: bool = False) -> None:
        if not self.show_cursor:
            return
        if self.cursor_at_end_of_text:
            return
        target = self.get_cursor_word_right_location()
        self.move_cursor(self._clamp_cursor_location(target, 1), select=select)

    def _delete_via_keyboard(self, start: Location, end: Location) -> object:
        """删除范围由 edit() 扩展到完整片段，并保留基类返回值。"""
        if self.read_only:
            return None
        return super()._delete_via_keyboard(start, end)

    def action_delete_left(self) -> None:
        if self.read_only:
            return
        selection = self.selection
        start, end = selection
        if selection.is_empty:
            end = self.get_cursor_left_location()
        self._delete_via_keyboard(start, end)

    def action_delete_right(self) -> None:
        if self.read_only:
            return
        selection = self.selection
        start, end = selection
        if selection.is_empty:
            end = self.get_cursor_right_location()
        self._delete_via_keyboard(start, end)

    def action_submit(self) -> None:
        display_text = self.text
        fragments = list(self._paste_spans)
        restored = self.restored_text(display_text, fragments)
        self.clear()
        if restored is not None and restored.strip():
            self.post_message(self.Submitted(restored, display_text=display_text))

    @staticmethod
    def restored_text(text: str, fragments: list[_PasteSpan]) -> str | None:
        """按可信片段边界恢复原文；边界不一致时安全失败。"""
        if not fragments:
            return text
        parts: list[str] = []
        cursor = 0
        for span in sorted(fragments, key=lambda s: s.start):
            if (
                span.start < cursor
                or span.end <= span.start
                or span.end > len(text)
                or text[span.start : span.end] != span.summary
            ):
                return None
            parts.append(text[cursor : span.start])
            parts.append(span.fragment.original)
            cursor = span.end
        parts.append(text[cursor:])
        return "".join(parts)

    def action_insert_newline(self) -> None:
        start, end = self.selection
        self.replace("\n", start, end, maintain_selection_offset=False)


__all__ = ["MicaComposer"]
