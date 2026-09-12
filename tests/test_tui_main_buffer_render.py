"""P6-11A 主缓冲区渲染、编辑与交互模式单元测试。"""

from __future__ import annotations

import io

import pytest
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

from mica_agent.interactions import AskOption, AskPrompt, PermissionAnswer
from mica_agent.tui.main_buffer.components import (
    IDLE_HINT,
    composer_frame,
    facts_lines,
    logo_lines,
    startup_blocks,
    status_line,
    tool_block,
)
from mica_agent.tui.main_buffer.editor import (
    ComposerEditor,
    normalize_key,
    windowed_lines,
)
from mica_agent.tui.main_buffer.interactions import (
    AskInteraction,
    PermissionInteraction,
)
from mica_agent.tui.main_buffer.terminal import FORBIDDEN_SEQUENCES, Terminal


def make_terminal(**kwargs) -> Terminal:
    defaults = {
        "output": io.StringIO(),
        "size": (40, 12),
        "environ": {"NO_COLOR": "1"},
    }
    defaults.update(kwargs)
    return Terminal(**defaults)


def emitted(terminal: Terminal) -> str:
    return "".join(terminal.emitted)


# -- 换行与宽度 -------------------------------------------------------------


def test_wrap_line_hard_wraps_and_keeps_explicit_lines() -> None:
    from mica_agent.tui.main_buffer.renderer import wrap_line

    assert wrap_line("abcdef", 3) == ["abc", "def"]
    assert wrap_line("a\nb", 10) == ["a", "b"]
    assert wrap_line("", 10) == [""]
    assert wrap_line("abc", 3) == ["abc"]


def test_wrap_line_counts_wide_characters_by_cells() -> None:
    from mica_agent.tui.main_buffer.renderer import cell_len, wrap_line

    assert cell_len("中文") == 4
    assert wrap_line("中文字", 4) == ["中文", "字"]
    assert wrap_line("a中b", 3) == ["a中", "b"]


def test_truncate_helpers_keep_requested_side() -> None:
    from mica_agent.tui.main_buffer.renderer import truncate_head, truncate_tail

    assert truncate_head("abcdef", 3) == "abc"
    assert truncate_head("中文字", 4) == "中文"
    assert truncate_tail("abcdef", 3) == "…ef"
    assert truncate_tail("short", 10) == "short"


# -- 渲染器 -----------------------------------------------------------------


def test_renderer_keeps_document_order_and_chrome_last() -> None:
    from mica_agent.tui.main_buffer.renderer import MainBufferRenderer, Palette

    terminal = make_terminal()
    renderer = MainBufferRenderer(terminal, width=40, height=12, palette=Palette(False))
    renderer.add_block("user", "first")
    renderer.add_block("assistant", "second")
    renderer.set_chrome(["chrome"], cursor=None)
    renderer.repaint()

    assert renderer.frame() == ["first", "second", "chrome"]


def test_renderer_streaming_updates_single_pending_block() -> None:
    from mica_agent.tui.main_buffer.renderer import MainBufferRenderer, Palette

    terminal = make_terminal()
    renderer = MainBufferRenderer(terminal, width=40, height=12, palette=Palette(False))
    renderer.update_pending("assistant", "Hel")
    renderer.repaint()
    renderer.update_pending("assistant", "Hello")
    renderer.repaint()

    assert renderer.document_lines() == ["Hello"]
    assert renderer.frame() == ["Hello"]


def test_renderer_never_emits_forbidden_terminal_sequences() -> None:
    from mica_agent.tui.main_buffer.renderer import MainBufferRenderer, Palette

    terminal = make_terminal()
    renderer = MainBufferRenderer(terminal, width=20, height=8, palette=Palette(False))
    renderer.add_block("user", "prompt")
    renderer.update_pending("assistant", "streaming text that wraps")
    renderer.set_chrome(["a", "b"], cursor=(0, 0))
    renderer.repaint()
    renderer.repaint()
    renderer.clear_chrome()

    text = emitted(terminal)
    for sequence in FORBIDDEN_SEQUENCES:
        assert sequence not in text
    assert "\x1b[?7l" not in text  # autowrap 只在 open_input 时切换


def test_renderer_resize_rewraps_document() -> None:
    from mica_agent.tui.main_buffer.renderer import MainBufferRenderer, Palette

    terminal = make_terminal()
    renderer = MainBufferRenderer(terminal, width=20, height=8, palette=Palette(False))
    renderer.add_block("assistant", "0123456789")
    renderer.repaint()
    assert renderer.document_lines() == ["0123456789"]

    renderer.set_width(5)
    assert renderer.document_lines() == ["01234", "56789"]


def test_styled_blocks_are_never_wrapped_or_split() -> None:
    """预着色行（Logo）必须原样输出，不能按列宽切断 ANSI 序列。"""
    from mica_agent.tui.main_buffer.renderer import MainBufferRenderer, Palette

    terminal = make_terminal(environ={})
    renderer = MainBufferRenderer(terminal, width=6, height=8, palette=Palette(True))
    logo = Palette(True).paint("██", "logo")
    renderer.add_block("raw", logo, styled=True)
    renderer.repaint()

    assert renderer.frame() == [logo]
    text = emitted(terminal)
    assert logo in text
    assert "[38;2;59;130;246" in text


def test_renderer_paints_styles_only_when_enabled() -> None:
    from mica_agent.tui.main_buffer.renderer import MainBufferRenderer, Palette

    plain_terminal = make_terminal()
    plain = MainBufferRenderer(
        plain_terminal, width=20, height=8, palette=Palette(False)
    )
    plain.add_block("user", "hello")
    plain.repaint()
    assert "\x1b[" not in emitted(plain_terminal).replace("\x1b[J", "")

    colored_terminal = make_terminal(environ={})
    colored = MainBufferRenderer(
        colored_terminal, width=20, height=8, palette=Palette(True)
    )
    colored.add_block("user", "hello")
    colored.set_chrome(["c"], cursor=None)
    colored.repaint()
    assert "\x1b[38;2;" in emitted(colored_terminal)


# -- chrome 组件 ------------------------------------------------------------


def test_composer_frame_reports_cursor_after_prompt() -> None:
    from mica_agent.tui.main_buffer.renderer import Palette

    frame = composer_frame(
        ["hey"],
        0,
        3,
        width=30,
        palette=Palette(False),
        enabled=True,
        hint="",
    )
    assert frame.lines[0].startswith("┌")
    assert frame.lines[-1].startswith("└")
    assert "› hey" in frame.lines[1]
    assert frame.cursor == (1, 1 + 2 + 3)


def test_composer_hint_degrades_before_placeholder_is_cramped() -> None:
    from mica_agent.tui.main_buffer.renderer import Palette

    narrow = composer_frame(
        [""],
        0,
        0,
        width=40,
        palette=Palette(False),
        enabled=True,
        hint=IDLE_HINT,
    )
    assert "Type your request or command..." in narrow.lines[1]
    assert "Ctrl+J" not in narrow.lines[1]

    wide = composer_frame(
        [""],
        0,
        0,
        width=110,
        palette=Palette(False),
        enabled=True,
        hint=IDLE_HINT,
    )
    assert "Ctrl+J for new line" in wide.lines[1]
    assert "Shift+Enter" not in wide.lines[1]


def test_composer_frame_shows_placeholder_and_hint() -> None:
    from mica_agent.tui.main_buffer.renderer import Palette

    frame = composer_frame(
        [""],
        0,
        0,
        width=70,
        palette=Palette(False),
        enabled=True,
        hint="Enter to send",
    )
    body = frame.lines[1]
    assert "Type your request or command..." in body
    assert "Enter to send" in body


def test_facts_lines_place_workspace_left_and_model_right() -> None:
    from mica_agent.tui.main_buffer.renderer import Palette

    lines = facts_lines(
        "~/work",
        "main",
        "model-x",
        "title",
        width=40,
        palette=Palette(False),
    )
    assert len(lines) == 2
    assert lines[0].startswith("~/work (main)")
    assert lines[0].rstrip().endswith("model-x")
    assert lines[1] == "sessions: title"


def test_status_line_only_renders_active_states() -> None:
    assert status_line("idle") is None
    assert status_line("working", elapsed=0.0).startswith("Mica · working")
    assert status_line("done", elapsed=8).endswith("done(8s)")
    assert status_line("done", elapsed=84).endswith("done(1m 24s)")
    assert status_line("stop") == "Mica · stop"
    assert status_line("error") == "Mica · error"


def test_startup_blocks_include_sections_and_none_placeholders() -> None:
    from mica_agent.tui.main_buffer.renderer import Palette
    from mica_agent.tui.models import StartupResourceSnapshot

    snapshot = StartupResourceSnapshot(
        system_prompt_name="Mica built-in system prompt",
        tool_names=("ask", "bash"),
    )
    blocks = startup_blocks(snapshot, 60, Palette(False))
    text = "\n".join(block.text for block in blocks)
    assert "Mica is ready." in text
    assert "System prompt: Mica built-in system prompt" in text
    assert "Global AGENTS.md: none" in text
    assert "2 available: ask, bash" in text
    assert "Skills" in text and "Extensions" in text


def test_logo_lines_switch_modes_by_width() -> None:
    from mica_agent.tui.main_buffer.renderer import Palette

    palette = Palette(False)
    assert logo_lines(20, palette) == ["Mica"]
    compact = logo_lines(30, palette)
    full = logo_lines(80, palette)
    assert len(compact) == 4
    assert len(full) == 6


def test_tool_block_uses_status_symbol_and_kind() -> None:
    block = tool_block("read", "success", "docs/a.md")
    assert block.text.startswith(" ✓ read")
    assert block.kind == "tool_success"
    recorded = tool_block("bash", "recorded", "")
    assert recorded.kind == "tool_cancelled"


# -- 编辑器 -----------------------------------------------------------------


def test_editor_supports_insert_move_and_delete() -> None:
    editor = ComposerEditor()
    editor.insert("abc")
    assert editor.text == "abc"
    assert editor.cursor_column == 3

    editor.move("left")
    editor.insert("X")
    assert editor.text == "abXc"

    editor.backspace()
    assert editor.text == "abc"

    editor.move("home")
    editor.delete()
    assert editor.text == "bc"


def test_editor_tracks_rows_for_real_newlines() -> None:
    editor = ComposerEditor()
    editor.insert("one\ntwo")
    assert editor.lines == ["one", "two"]
    assert editor.cursor_row == 1
    assert editor.cursor_column == 3


def test_editor_blank_detection_and_clear() -> None:
    editor = ComposerEditor()
    editor.insert("   \n  ")
    assert editor.is_blank
    editor.insert("x")
    assert not editor.is_blank
    assert editor.submit_text() == "   \n  x"
    editor.clear()
    assert editor.text == ""
    assert editor.submit_text() == ""


def test_windowed_lines_keeps_cursor_visible() -> None:
    lines = [f"line-{index}" for index in range(20)]
    visible, start = windowed_lines(lines, 15, limit=8)
    assert len(visible) == 8
    assert start == 8
    assert visible[15 - start] == "line-15"
    short, start = windowed_lines(["only"], 0, limit=8)
    assert (short, start) == (["only"], 0)


def test_normalize_key_maps_terminal_keys() -> None:
    assert normalize_key(KeyPress("a", "a")).name == "text"
    assert normalize_key(KeyPress(Keys.ControlM, "\r")).name == "enter"
    assert normalize_key(KeyPress(Keys.ControlC, "\x03")).name == "ctrl_c"
    assert normalize_key(KeyPress(Keys.Escape, "\x1b")).name == "escape"
    assert normalize_key(KeyPress(Keys.Up, "\x1b[A")).name == "up"
    assert normalize_key(KeyPress(Keys.ControlH, "\x7f")).name == "backspace"
    paste = normalize_key(KeyPress(Keys.BracketedPaste, "a\r\nb"))
    assert (paste.name, paste.data) == ("paste", "a\nb")


# -- 交互模式 ---------------------------------------------------------------


def make_ask(**kwargs) -> AskInteraction:
    prompt = AskPrompt(
        "Pick one",
        title="后续任务",
        options=kwargs.pop("options", (AskOption("Alice"), AskOption("Bob"))),
        **kwargs,
    )
    return AskInteraction("i-1", prompt)


def test_ask_interaction_navigates_and_submits_selection() -> None:
    ask = make_ask()
    assert ask.handle(normalize_key(KeyPress(Keys.Down, "\x1b[B"))).action == "none"
    assert ask.selected == 1
    outcome = ask.handle(normalize_key(KeyPress(Keys.ControlM, "\r")))
    assert outcome.action == "response"
    assert outcome.response is not None
    assert outcome.response.answer == "Bob"
    assert outcome.response.selected_option_index == 1


def test_ask_interaction_digit_selects_and_escape_cancels() -> None:
    ask = make_ask()
    ask.handle(normalize_key(KeyPress("2", "2")))
    assert ask.selected == 1
    outcome = ask.handle(normalize_key(KeyPress(Keys.Escape, "\x1b")))
    assert outcome.action == "cancel"


def test_ask_interaction_supports_custom_answer_and_notes() -> None:
    ask = make_ask(allow_custom=True)
    ask.selected = 2
    assert ask.handle(normalize_key(KeyPress(Keys.ControlM, "\r"))).action == "none"
    assert ask.editing == "custom"
    for character in "typed":
        ask.handle(normalize_key(KeyPress(character, character)))
    outcome = ask.handle(normalize_key(KeyPress(Keys.ControlM, "\r")))
    assert outcome.action == "response"
    assert outcome.response is not None
    assert outcome.response.answer == "typed"

    noted = make_ask()
    noted.handle(normalize_key(KeyPress("n", "n")))
    for character in "why":
        noted.handle(normalize_key(KeyPress(character, character)))
    noted.handle(normalize_key(KeyPress(Keys.ControlM, "\r")))
    assert noted.editing == "options"
    outcome = noted.handle(normalize_key(KeyPress(Keys.ControlM, "\r")))
    assert outcome.response is not None
    assert outcome.response.notes == "why"


def test_ask_interaction_free_text_without_options() -> None:
    ask = AskInteraction("i-2", AskPrompt("Type a value"))
    assert ask.is_text_entry
    for character in "42":
        ask.handle(normalize_key(KeyPress(character, character)))
    outcome = ask.handle(normalize_key(KeyPress(Keys.ControlM, "\r")))
    assert outcome.response is not None
    assert outcome.response.answer == "42"


def test_permission_interaction_confirms_once_and_cancels() -> None:
    permission = PermissionInteraction("i-3", "bash", "$ git push")
    outcome = permission.handle(normalize_key(KeyPress(Keys.ControlM, "\r")))
    assert outcome.response is not None
    assert outcome.response.answer is PermissionAnswer.ALLOW_ONCE

    permission.handle(normalize_key(KeyPress(Keys.Down, "\x1b[B")))
    outcome = permission.handle(normalize_key(KeyPress(Keys.ControlM, "\r")))
    assert outcome.response is not None
    assert outcome.response.answer is PermissionAnswer.DENY

    outcome = permission.handle(normalize_key(KeyPress(Keys.Escape, "\x1b")))
    assert outcome.action == "cancel"


@pytest.mark.parametrize("width", [24, 40, 80])
def test_interaction_panels_fit_declared_width(width: int) -> None:
    from mica_agent.tui.main_buffer.renderer import Palette

    palette = Palette(False)
    ask_lines = make_ask().panel_lines(width, palette)
    permission_lines = PermissionInteraction("i", "bash", "$ ls").panel_lines(
        width, palette
    )
    assert ask_lines and permission_lines
    assert any("Pick one" in line for line in ask_lines)
    assert any("Do you want to proceed?" in line for line in permission_lines)
