"""主缓冲区 Markdown 渲染测试（P5-11B §4.4 / §9）。

覆盖：结构要素、Unicode 与宽度、流式增量与终态稳定渲染、resize 重排、
NO_COLOR 结构可读性，以及终端注入与超链接防护。
"""

from __future__ import annotations

import pytest
from rich.console import Console

from mica_agent.tui.main_buffer import markdown
from mica_agent.tui.main_buffer.markdown import (
    MAX_SOURCE_CHARS,
    MarkdownBlock,
    StreamingMarkdownBlock,
    render_markdown,
    render_markdown_preview,
)
from mica_agent.tui.main_buffer.renderer import Palette, cell_len, strip_ansi
from mica_agent.tui.main_buffer.terminal import find_forbidden_sequences

SAMPLE = """# 一级标题

普通段落包含 **粗体**、*斜体*、`inline code` 和 [链接文本](https://example.com/a)。

## 二级标题

- 无序一
- 无序二

1. 有序一
2. 有序二

> 引用内容

```python
def add(a, b):
    return a + b
```

### 三级标题
"""


def visible(rows: list[str]) -> list[str]:
    """去掉样式序列后的可见行。"""
    return [strip_ansi(row) for row in rows]


def test_markdown_renders_all_required_blocks_with_cjk_width() -> None:
    rows = render_markdown(SAMPLE, 40, Palette(True))
    text = visible(rows)

    assert text[0] == "一级标题"
    assert any("粗体" in line and "斜体" in line for line in text)
    assert any("inline code" in line for line in text)
    assert any("二级标题" in line for line in text)
    assert any(line.startswith(" • ") for line in text)
    assert any(
        line.strip().startswith("1 ") or line.strip().startswith("1.") for line in text
    )
    assert any(line.startswith("▌ ") for line in text)
    assert any("三级标题" in line for line in text)
    assert any("def add(a, b):" in line for line in text)
    assert all(len(line) <= 40 for line in text if line)


def test_markdown_never_leaks_raw_syntax_or_control_sequences() -> None:
    rows = render_markdown(SAMPLE, 40, Palette(True))
    text = "\n".join(visible(rows))

    assert "**" not in text
    assert "```" not in text
    assert "]" not in text.replace("```", "")
    assert find_forbidden_sequences("\n".join(rows)) == []
    assert "\x1b]8;" not in "\n".join(rows)


def test_inline_code_and_code_block_use_semantic_backgrounds() -> None:
    rows = render_markdown(SAMPLE, 40, Palette(True))
    joined = "\n".join(rows)
    inline_rows = [row for row in rows if "inline code" in strip_ansi(row)]

    # 行内代码只用协调前景色，不再使用背景色块。
    assert inline_rows
    assert all("\x1b[48;" not in row and ";48;" not in row for row in inline_rows)
    assert "38;2;137;180;250" in "\n".join(inline_rows)
    # 围栏代码块仍保留完整深灰背景并覆盖可用宽度。
    assert "48;2;40;44;52" in joined
    code_rows = [row for row in rows if "48;2;40;44;52" in row]
    assert code_rows
    assert any(len(strip_ansi(row)) == 40 for row in code_rows)


def test_dense_inline_code_has_no_background_blocks() -> None:
    source = (
        "Open `path/to/file.py`, run `bash`, then set `MICA_API_URL` "
        "and `mica cli` before editing `src/mica_agent/tui/main_buffer/screen.py`."
    )
    rows = render_markdown(source, 80, Palette(True))
    joined = "\n".join(rows)
    assert "\x1b[48;" not in joined and ";48;" not in joined
    assert find_forbidden_sequences(joined) == []
    text = "\n".join(visible(rows))
    assert "path/to/file.py" in text
    assert "MICA_API_URL" in text
    assert "mica cli" in text


def test_no_color_keeps_structure_without_color_codes() -> None:
    rows = render_markdown(SAMPLE, 40, Palette(False))
    joined = "\n".join(rows)

    assert "\x1b[48;" not in joined and "\x1b[38;" not in joined
    assert any(row.startswith("│ ") for row in visible(rows))
    assert any(row.startswith("▌ ") for row in visible(rows))
    assert any(row.startswith(" • ") for row in visible(rows))
    assert "\x1b[1m一级标题" in joined
    assert all("**" not in row and "```" not in row for row in visible(rows))


def test_markdown_rewraps_after_resize_without_changing_source() -> None:
    source = "# 标题\n\n" + " ".join(["长内容"] * 40) + "\n"
    narrow = visible(render_markdown(source, 24, Palette(True)))
    wide = visible(render_markdown(source, 60, Palette(True)))

    assert len(narrow) > len(wide)
    assert all(len(line) <= 24 for line in narrow if line)
    assert all(len(line) <= 60 for line in wide if line)
    assert source.startswith("# 标题")


def test_streaming_incomplete_markdown_never_raises_and_keeps_prefix() -> None:
    block = StreamingMarkdownBlock()
    for delta in ("# 标题\n\n", "段落 **未闭合", "粗体", "\n\n```py\nprint(1)"):
        block.append(delta)
        rows = block.render(36, Palette(True), False)
        assert rows
        assert find_forbidden_sequences("\n".join(rows)) == []

    preview = "\n".join(visible(block.render(36, Palette(True), False)))
    assert preview.rstrip().endswith("▌")


def test_finish_replaces_cursor_with_stable_final_render() -> None:
    block = StreamingMarkdownBlock()
    block.append("# 标题\n\n正文 **加粗**。\n")
    streaming = block.render(40, Palette(True), False)
    block.finish()
    final = block.render(40, Palette(True), False)

    assert visible(streaming)[-1].endswith("▌")
    assert not any("▌" in line for line in visible(final))
    assert visible(final) == visible(
        MarkdownBlock(block.source).render(40, Palette(True), False)
    )


def test_streaming_block_split_preserves_logical_prefix() -> None:
    block = StreamingMarkdownBlock()
    block.append("\n\n".join(f"段落 {index} " + "内容" * 8 for index in range(6)))
    split = block.split(3, width=30, palette=Palette(True), expanded=False)
    assert split is not None
    head, tail = split

    head_rows = head.render(30, Palette(True), False)
    assert tail.frozen == len(head_rows)
    assert tail.frozen > 0
    combined = visible(head.render(30, Palette(True), False)) + visible(
        tail.render(30, Palette(True), False)
    )
    assert combined == visible(block.render(30, Palette(True), False))


@pytest.mark.parametrize(
    "payload",
    [
        "恶意 \x1b[2J 清屏",
        "标题 \x1b]0;title\x07",
        "DCS \x1bP1;2;3q\x1b\\ 内容",
        "C1 \x9b31m 内容",
        "C0 \x00\x01\x02 内容",
        "HTML <script>alert(1)</script> 文本",
        "链接 [click](https://evil.example/\x1b]8;;x\x07)",
        "![图片](https://evil.example/a.png)",
        "代码 `\x1b[31m红色` 与\n\n```sh\nrm -rf /\x1b]52;c;x\x07\n```\n",
    ],
)
def test_untrusted_markdown_cannot_reach_the_terminal(payload: str) -> None:
    rows = render_markdown(payload, 60, Palette(True))
    stream = "\n".join(rows)

    assert find_forbidden_sequences(stream) == []
    assert "\x1b]8;" not in stream
    assert "?1049h" not in stream
    assert "52;c;" not in stream


def test_markdown_uses_readable_link_text_without_hyperlinks() -> None:
    rows = render_markdown("见 [文档](https://example.com/doc)。", 60, Palette(True))
    text = "\n".join(visible(rows))

    assert "文档" in text
    assert "https://example.com/doc" in text
    assert "\x1b]8;" not in "\n".join(rows)


def test_markdown_keeps_combining_characters_and_emoji_intact() -> None:
    source = "组合 é 与 emoji 🚀 以及中文混排" * 3
    rows = render_markdown(source, 20, Palette(True))
    text = "".join(visible(rows))

    assert "é" in text and "🚀" in text
    assert text.count("组合") == 3
    assert all("\x00" not in row for row in visible(rows))


def test_preview_and_final_rendering_share_the_same_source() -> None:
    source = "**加粗** 与 `代码`"
    preview = render_markdown_preview(source, 40, Palette(True), streaming=True)
    final = render_markdown(source, 40, Palette(True))

    assert visible(preview)[0].replace("▌", "") == visible(final)[0]


# -- P5-11B 返工：累计源上限（跨多个 delta） --------------------------------


def test_streaming_source_is_capped_across_many_deltas() -> None:
    """连续追加多个 delta 后，完整累计源文本不得越过上限。"""
    block = StreamingMarkdownBlock()
    chunk = "x" * 8192
    for _ in range(20):  # 20 × 8192 远高于上限
        block.append(chunk)

    assert len(block.source) == MAX_SOURCE_CHARS
    capped = block.source

    # 到达上限后行为幂等：继续追加不再改变累计源。
    for _ in range(5):
        block.append("y" * 4096)
    assert block.source == capped
    assert len(block.source) == MAX_SOURCE_CHARS


def test_capped_streaming_block_keeps_cursor_finish_and_split() -> None:
    block = StreamingMarkdownBlock()
    block.append("标题\n\n")
    block.append("字" * MAX_SOURCE_CHARS)
    assert len(block.source) <= MAX_SOURCE_CHARS
    assert block.source.startswith("标题")

    streaming = visible(block.render(24, Palette(True), False))
    assert streaming and streaming[-1].endswith("▌")

    split = block.split(2, width=24, palette=Palette(True), expanded=False)
    assert split is not None
    head, tail = split
    assert isinstance(tail, StreamingMarkdownBlock)
    assert tail.frozen == len(head.render(24, Palette(True), False))
    assert tail.source == block.source

    block.finish()
    final = visible(block.render(24, Palette(True), False))
    assert not any("▌" in row for row in final)
    assert final == visible(
        MarkdownBlock(block.source).render(24, Palette(True), False)
    )


def test_capped_streaming_block_still_cleans_each_delta() -> None:
    block = StreamingMarkdownBlock()
    block.append("恶意 \x1b[2J 内容\n")
    block.append("in" + "a" * MAX_SOURCE_CHARS + "\x1b]0;title\x07")

    assert "\x1b" not in block.source
    assert len(block.source) <= MAX_SOURCE_CHARS
    assert block.source.startswith("恶意 ")


# -- P5-11B 返工：Rich 异常降级逐物理行 -----------------------------------


def _boom(*args: object, **kwargs: object) -> object:
    raise RuntimeError("rich render failed")


def _interrupt(*args: object, **kwargs: object) -> object:
    raise KeyboardInterrupt


def test_render_failure_degrades_to_bounded_physical_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Console, "render_lines", _boom)
    source = "第一行\n" + "宽" * 40 + "\n\t缩进 结尾"
    rows = render_markdown(source, 12, Palette(True))
    stream = "\n".join(rows)

    # 每行都是一条独立物理行，且不超过终端宽度。
    assert all(cell_len(row) <= 12 for row in rows)
    # 逻辑换行与软换行都真实生效：内容未丢失且未合并成一条超宽行。
    assert len(rows) > 3
    assert "第一行" in stream
    assert "缩进 结尾" in stream.replace("\n", "")
    assert "宽" * 40 in stream.replace("\n", "")
    # 降级行只使用已清洗纯文本：无控制内容、无新增控制序列。
    assert "\x1b" not in stream
    assert "\t" not in stream
    assert find_forbidden_sequences(stream) == []


def test_render_failure_preview_keeps_cursor_and_unicode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Console, "render_lines", _boom)
    rows = render_markdown_preview(
        "组合 é 🚀\n\n尾部\n", 8, Palette(True), streaming=True
    )
    text = visible(rows)

    # 完整物理行（含光标）不得超宽。
    assert all(cell_len(strip_ansi(row)) <= 8 for row in rows)
    assert any("组合 é" in row for row in text)
    assert any("🚀" in row for row in text)
    assert any("尾部" in row for row in text)
    # 空白逻辑行保留为一条空物理行。
    assert "" in [row.replace("▌", "") for row in text]
    joined = "".join(text)
    assert joined.count("▌") == 1
    assert all("\x00" not in row and "\x1b" not in row for row in text)
    assert find_forbidden_sequences("\n".join(rows)) == []


def _constructor_boom(*args: object, **kwargs: object) -> object:
    raise RuntimeError("constructor secret")


def _constructor_interrupt(*args: object, **kwargs: object) -> object:
    raise KeyboardInterrupt


def test_markdown_constructor_failure_degrades_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析器构造阶段抛普通异常也要进入同一个安全降级。"""
    monkeypatch.setattr(markdown, "_MicaMarkdown", _constructor_boom)
    source = "第一行\n" + "宽" * 30 + "\n尾部"
    rows = render_markdown(source, 10, Palette(True))
    stream = "\n".join(rows)

    # 逐物理行、宽度受限、保留逻辑换行。
    assert all(cell_len(strip_ansi(row)) <= 10 for row in rows)
    assert len(rows) >= 4
    assert "第一行" in stream
    assert "尾部" in stream
    assert "宽" * 30 in stream.replace("\n", "")
    # 降级行只使用已清洗纯文本：无控制内容，也不泄露异常文本。
    assert "\x1b" not in stream
    assert find_forbidden_sequences(stream) == []
    assert "constructor secret" not in stream


def test_markdown_constructor_interrupt_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(markdown, "_MicaMarkdown", _constructor_interrupt)
    with pytest.raises(KeyboardInterrupt):
        render_markdown("文本", 20, Palette(True))


@pytest.mark.parametrize(
    ("source", "width"),
    [
        ("abcde", 5),
        ("宽宽", 4),
        ("火箭 🚀", 8),
        ("abcd", 5),
        ("abcd", 4),
    ],
)
def test_streaming_cursor_fits_the_physical_line_budget(
    source: str, width: int
) -> None:
    """含光标的完整物理行不得超宽，且正文不得被删或截。"""
    rows = render_markdown_preview(source, width, Palette(True), streaming=True)
    text = visible(rows)

    # 检查真实会写入终端的完整行（不去掉光标）。
    assert all(cell_len(strip_ansi(row)) <= width for row in rows)
    assert "".join(text).count("▌") == 1
    assert text[-1].endswith("▌")
    body = "".join(text).replace("▌", "")
    assert body.replace(" ", "") == source.replace(" ", "")
    assert sum(1 for row in text if "▌" in row) == 1


def test_streaming_cursor_moves_to_a_new_line_only_when_full() -> None:
    full = render_markdown_preview("abcde", 5, Palette(True), streaming=True)
    spare = render_markdown_preview("abcd", 5, Palette(True), streaming=True)

    # 末行占满 width：光标另起一条物理行。
    assert visible(full) == ["abcde", "▌"]
    # 末行尚有一个空余列：光标就放在同一行。
    assert visible(spare) == ["abcd▌"]
    assert cell_len(strip_ansi(spare[0])) == 5


def test_streaming_cursor_counts_width_on_the_fallback_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Console, "render_lines", _boom)
    rows = render_markdown_preview(
        "abcde\n" + "宽" * 6, 5, Palette(True), streaming=True
    )
    text = visible(rows)

    assert all(cell_len(strip_ansi(row)) <= 5 for row in rows)
    assert "".join(text).count("▌") == 1
    assert text[-1].endswith("▌")
    assert "abcde" in text
    assert "".join(text).replace("▌", "").replace("\n", "") == "abcde" + "宽" * 6


def test_streaming_cursor_disappears_after_finish_for_full_lines() -> None:
    block = StreamingMarkdownBlock()
    for delta in ("abcde", "\n", "宽宽"):
        block.append(delta)
    streaming = visible(block.render(5, Palette(True), False))
    block.finish()
    final = visible(block.render(5, Palette(True), False))

    assert "".join(streaming).count("▌") == 1
    assert not any("▌" in row for row in final)
    assert final == visible(MarkdownBlock(block.source).render(5, Palette(True), False))
    assert all(cell_len(row) <= 5 for row in final)


def test_render_failure_never_swallows_base_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Console, "render_lines", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        render_markdown("文本", 20, Palette(True))
