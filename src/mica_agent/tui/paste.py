"""TUI 内的长粘贴片段模型与源文本片段。"""

from __future__ import annotations

from dataclasses import dataclass

# 超过该逻辑行数时折叠为长粘贴片段；否则按普通文本插入。
PASTE_LINE_THRESHOLD = 9


@dataclass(frozen=True, slots=True)
class PasteFragment:
    """一个不可展开的长粘贴片段。"""

    original: str
    """用户真实粘贴的完整原始文本。"""

    line_count: int
    """原始文本的逻辑行数。"""

    number: int
    """当前草稿内的粘贴片段序号，从 1 开始。"""

    @property
    def summary(self) -> str:
        """在 Composer 和用户消息卡片中显示的紧凑摘要。"""
        return f"[paste #{self.number} +{self.line_count} lines]"

    @property
    def is_long(self) -> bool:
        return self.line_count >= PASTE_LINE_THRESHOLD


def _logical_line_count(text: str) -> int:
    """计算文本的内容行数。

    以换行符分隔逻辑行，但一个末尾的换行符只是终止最后一行，不再额外计为
    一行；中间的空白行（空行）仍然计入。CRLF 只计一个换行边界。
    """
    # 先统一 CRLF，避免 \r\n 被当作文本中多出的一个字符。
    normalized = text.replace("\r\n", "\n")
    if not normalized:
        return 0
    lines = normalized.split("\n")
    if normalized.endswith("\n"):
        lines = lines[:-1]
    return len(lines)


def make_paste_fragment(original: str, number: int) -> PasteFragment | None:
    """根据是否达到折叠阈值决定是否生成长粘贴片段。

    短粘贴（少于阈值逻辑行）返回 ``None``，由调用方按普通文本插入。
    """
    line_count = _logical_line_count(original)
    if line_count < PASTE_LINE_THRESHOLD:
        return None
    return PasteFragment(
        original=original,
        line_count=line_count,
        number=number,
    )
