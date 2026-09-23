"""P5-11A 主缓冲区渲染、编辑与交互模式单元测试。"""

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

    assert renderer.frame() == ["first", "", "second", "chrome"]


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


def test_read_tool_view_is_one_line_with_safe_reason() -> None:
    """read 视图：单行、只显示工具名与安全相对路径；失败只追加固定原因。"""
    from mica_agent.tool_presentation import (
        ToolPresentationKind,
        ToolPresentationSnapshot,
        ToolPresentationStatus,
    )
    from mica_agent.tui.main_buffer.renderer import Palette
    from mica_agent.tui.main_buffer.tool_views import ToolViewFactory

    factory = ToolViewFactory()
    ok = ToolPresentationSnapshot(
        "c1",
        "read",
        ToolPresentationKind.READ,
        ToolPresentationStatus.SUCCESS,
        {"path": "docs/a.md"},
    )
    ok_view = factory.create(ok, state="success")
    assert ok_view is not None
    assert ok_view.render(30, Palette(False), False) == ["✓ read  docs/a.md"]

    failed = ToolPresentationSnapshot(
        "c2",
        "read",
        ToolPresentationKind.READ,
        ToolPresentationStatus.FAILED,
        {"path": "docs/a.md"},
        "tool execution failed",
    )
    failed_view = factory.create(failed, state="failed")
    assert failed_view is not None
    assert failed_view.render(40, Palette(False), False) == [
        "× read  docs/a.md  tool execution failed"
    ]


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


# -- P5-11C-MR2：超高交互面板的紧凑降级 --------------------------------------


def tall_ask_prompt() -> AskPrompt:
    """一个远超可视高度的 ask 面板：4 个带长说明的选项 + 自定义行。"""
    return AskPrompt(
        "选择接下来要执行的操作。",
        title="后续任务",
        options=tuple(
            AskOption(f"option {index}", f"description {index} " * 24)
            for index in range(1, 5)
        ),
        allow_custom=True,
    )


def test_ask_panel_without_a_budget_keeps_the_full_layout() -> None:
    """未给出可用行数时布局不变：说明、标题、footer 与分隔线都还在。"""
    from mica_agent.tui.main_buffer.renderer import Palette

    palette = Palette(False)
    ask = AskInteraction("i-1", tall_ask_prompt())
    lines = ask.panel_lines(80, palette)
    assert len(lines) > 20
    assert any("后续任务" in line for line in lines)
    assert any("description 1" in line for line in lines)
    assert any("Enter 选择" in line for line in lines)
    assert "─" in lines[0] and "─" in lines[-1]


@pytest.mark.parametrize("selected", [0, 2, 4])
def test_super_tall_ask_panel_keeps_the_selected_row_visible(selected: int) -> None:
    """80×12 终端的可用高度下，当前选中行必须在可见行里。"""
    from mica_agent.tui.main_buffer.renderer import Palette

    palette = Palette(False)
    ask = AskInteraction("i-1", tall_ask_prompt())
    ask.selected = selected
    view = ask.panel(80, palette, limit=11)
    assert len(view.lines) <= 11
    assert 0 <= view.selected_row < len(view.lines)
    marker = view.lines[view.selected_row]
    assert marker.startswith("›")
    if selected == 4:
        assert "Type something." in marker
    else:
        assert f"{selected + 1}. option {selected + 1}" in marker
    # 未进入窗口化：全部固定选项标签仍然保留。
    assert all(
        any(f"{index}. option {index}" in line for line in view.lines)
        for index in range(1, 5)
    )


def test_ask_panel_windows_around_the_selection_and_reports_hidden_rows() -> None:
    """连核心行都放不下时取窗口，并用固定提示说明被隐藏的行数。"""
    from mica_agent.tui.main_buffer.renderer import Palette

    palette = Palette(False)
    # 选中靠上的选项时窗口优先保住问题主句，被隐藏的尾部用下方提示表达。
    ask = AskInteraction("i-1", tall_ask_prompt())
    ask.selected = 2
    view = ask.panel(80, palette, limit=5)
    assert len(view.lines) <= 5
    assert view.lines[view.selected_row].startswith("›")
    assert "3. option 3" in view.lines[view.selected_row]
    text = "\n".join(view.lines)
    assert "选择接下来要执行的操作。" in text
    assert "下方还有" in text
    assert "上方还有" not in text

    # 选中最后一个可选项（自定义行）时改为上方提示，选中行依然可见。
    ask.selected = 4
    view = ask.panel(80, palette, limit=5)
    assert len(view.lines) <= 5
    assert view.lines[view.selected_row].startswith("›")
    assert "Type something." in view.lines[view.selected_row]
    assert "上方还有" in "\n".join(view.lines)


def test_permission_panel_keeps_the_current_choice_with_a_long_detail() -> None:
    """权限详情很长时也不能把 Yes/No 的当前选择裁掉。"""
    from mica_agent.tui.main_buffer.renderer import Palette

    palette = Palette(False)
    detail = " ".join(f"segment{index}" for index in range(80))
    for selected in (0, 1):
        permission = PermissionInteraction("i-2", "bash", detail)
        permission.selected = selected
        view = permission.panel(80, palette, limit=12)
        assert len(view.lines) <= 12
        assert 0 <= view.selected_row < len(view.lines)
        assert view.lines[view.selected_row].startswith(f"› {selected + 1}.")
        assert any("Do you want to proceed?" in line for line in view.lines)


def test_panel_budget_never_drops_the_question_or_the_hint() -> None:
    from mica_agent.tui.main_buffer.interactions import (
        HINT_RESPONSE_REJECTED,
    )
    from mica_agent.tui.main_buffer.interactions import (
        AskInteraction as Ask,
    )
    from mica_agent.tui.main_buffer.renderer import Palette

    palette = Palette(False)
    ask = Ask("i-1", tall_ask_prompt())
    ask.fail()
    view = ask.panel(80, palette, limit=7)
    assert len(view.lines) <= 7
    text = "\n".join(view.lines)
    # 提示、问题主句与当前选中行是这一屏必须同时看到的三件事。
    assert HINT_RESPONSE_REJECTED in text
    assert "选择接下来要执行的操作。" in text
    assert view.lines[view.selected_row].startswith("›")

    # 高度再低时至少保证当前选中行：窗口化不允许丢掉锚点。
    tight = ask.panel(80, palette, limit=3)
    assert len(tight.lines) <= 3
    assert tight.lines[tight.selected_row].startswith("›")


# -- P5-11C-MR2 返工：选中行是窗口选择的硬约束 --------------------------------


@pytest.mark.parametrize("limit", [1, 2, 3])
@pytest.mark.parametrize("anchor", [5, 8])
def test_fit_panel_rows_never_drops_the_anchor_for_distant_hints(
    limit: int, anchor: int
) -> None:
    """多行提示离选中行很远时，锚点仍然是窗口选择的硬约束。

    「命中多少必留行」只能作为次级评分：这里提示行在最上面、选中行在中间或
    靠下，旧评分会为了两条提示行而挑出不含选中行的窗口。
    """
    from mica_agent.tui.main_buffer.interactions import (
        RANK_CORE,
        PanelRow,
        fit_panel_rows,
    )

    rows = [PanelRow(f"row {index}", RANK_CORE) for index in range(11)]
    rows[anchor] = PanelRow(f"anchor {anchor}", RANK_CORE)
    preferred = [0, 1]
    assert min(abs(anchor - index) for index in preferred) >= 4

    visible, selected = fit_panel_rows(rows, limit, anchor, preferred)
    assert len(visible) <= limit
    assert 0 <= selected < len(visible)
    assert visible[selected] is rows[anchor]
    assert visible[selected].text == f"anchor {anchor}"


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5])
def test_fit_panel_rows_keeps_the_anchor_next_to_wrapped_hints(limit: int) -> None:
    """拒绝提示换成两行时，窗口不能改成只显示提示行与计数行。"""
    from mica_agent.tui.main_buffer.interactions import (
        RANK_CORE,
        PanelRow,
        fit_panel_rows,
    )

    rows = [
        PanelRow("question", RANK_CORE),
        PanelRow("yes", RANK_CORE),
        PanelRow("no", RANK_CORE),
        PanelRow("hint line 1", RANK_CORE),
        PanelRow("hint line 2", RANK_CORE),
    ]
    visible, selected = fit_panel_rows(rows, limit, 2, [3, 4])
    assert len(visible) <= limit
    assert 0 <= selected < len(visible)
    assert visible[selected] is rows[2]
    assert visible[selected].text == "no"


@pytest.mark.parametrize("width", [20, 24, 30])
@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5])
def test_permission_panel_keeps_the_selected_row_after_a_wrapped_rejection(
    width: int, limit: int
) -> None:
    """业务拒绝后的多行提示不得把当前选项挤出面板（含极窄与极矮预算）。"""
    from mica_agent.tui.main_buffer.interactions import HINT_RESPONSE_REJECTED
    from mica_agent.tui.main_buffer.renderer import Palette, wrap_line

    # 场景本身必须成立：这条提示在窄终端里真的会换行。
    assert len(wrap_line(HINT_RESPONSE_REJECTED, 20)) >= 2

    palette = Palette(False)
    for selected, marker in ((0, "› 1. Yes"), (1, "› 2. No")):
        permission = PermissionInteraction("perm-1", "bash", "run the command")
        permission.selected = selected
        permission.fail()
        view = permission.panel(width, palette, limit=limit)
        assert len(view.lines) <= limit
        assert 0 <= view.selected_row < len(view.lines)
        assert view.lines[view.selected_row].startswith(marker)


@pytest.mark.parametrize("limit", [2, 3, 4])
@pytest.mark.parametrize(
    ("selected", "marker"),
    [(0, "› 1. option 1"), (3, "› 4. option 4"), (4, "› Type something.")],
)
def test_ask_panel_keeps_the_selected_row_after_a_wrapped_rejection(
    limit: int, selected: int, marker: str
) -> None:
    """固定选项首项/末项与自定义行都必须在拒绝提示换行后仍然可见。"""
    from mica_agent.tui.main_buffer.interactions import HINT_RESPONSE_REJECTED
    from mica_agent.tui.main_buffer.renderer import Palette, wrap_line

    assert len(wrap_line(HINT_RESPONSE_REJECTED, 20)) >= 2

    palette = Palette(False)
    ask = AskInteraction("i-1", tall_ask_prompt())
    ask.selected = selected
    ask.fail()
    view = ask.panel(20, palette, limit=limit)
    assert len(view.lines) <= limit
    assert 0 <= view.selected_row < len(view.lines)
    assert view.lines[view.selected_row].startswith(marker)


def test_only_consumed_keys_clear_the_rejection_hint() -> None:
    """只有被交互消费且改变状态的按键才能清掉拒绝提示。"""
    from mica_agent.tui.main_buffer.editor import Key
    from mica_agent.tui.main_buffer.interactions import (
        HINT_EMPTY_ANSWER,
        HINT_RESPONSE_REJECTED,
    )

    unconsumed = (
        Key("unknown", "\x1b[5~"),  # PageUp
        Key("unknown", "\x1b[6~"),  # PageDown
        Key("unknown", "\x1b[15~"),  # 未知功能键
        Key("left", "\x1b[D"),  # 不适用的方向键
        Key("text", "9"),  # 越界数字
        Key("text", "\x00"),  # 清洗后为空的文本，不改变草稿
    )

    prompt = AskPrompt(
        "继续吗？", options=(AskOption("是"), AskOption("否")), allow_custom=True
    )
    ask = AskInteraction("ask-1", prompt)
    for key in unconsumed:
        ask.fail()
        assert ask.handle(key).action == "none"
        assert ask.selected == 0
        assert ask.editing == "options"
        assert ask.hint == HINT_RESPONSE_REJECTED

    # 有效导航、有效数字定位、进入备注、进入自定义与文本编辑都会清除旧提示。
    for key in (Key("down"), Key("text", "2"), Key("text", "n")):
        ask.fail()
        assert ask.handle(key).action == "none"
        assert ask.hint == ""
        ask.editing = "options"

    ask.fail()
    assert ask.handle(Key("text", "n")).action == "none"
    assert (ask.editing, ask.hint) == ("notes", "")
    ask.fail()
    assert ask.handle(Key("backspace")).action == "none"
    assert ask.hint == ""
    ask.fail()
    assert ask.handle(Key("enter")).action == "none"
    assert (ask.editing, ask.hint) == ("options", "")

    ask.selected = ask.row_count - 1
    ask.fail()
    assert ask.handle(Key("enter")).action == "none"
    assert (ask.editing, ask.hint) == ("custom", "")
    # 清洗后为空的文本不改变草稿，因此也不清除提示。
    ask.fail()
    assert ask.handle(Key("text", "\x00")).action == "none"
    assert ask.hint == HINT_RESPONSE_REJECTED
    assert ask.handle(Key("text", "x")).action == "none"
    assert ask.hint == ""
    ask.fail()
    assert ask.handle(Key("delete")).action == "none"
    assert ask.hint == ""
    ask.fail()
    assert ask.handle(Key("up")).action == "none"
    assert (ask.editing, ask.hint) == ("options", "")

    # 空答案的提示会顶掉旧的拒绝提示。
    free = AskInteraction("ask-2", AskPrompt("Type anything"))
    free.fail()
    assert free.handle(Key("enter")).action == "none"
    assert free.hint == HINT_EMPTY_ANSWER

    permission = PermissionInteraction("perm-1", "bash", "run it")
    for key in unconsumed:
        permission.fail()
        assert permission.handle(key).action == "none"
        assert permission.selected == 0
        assert permission.hint == HINT_RESPONSE_REJECTED
    permission.fail()
    assert permission.handle(Key("down")).action == "none"
    assert (permission.selected, permission.hint) == (1, "")
    permission.fail()
    assert permission.handle(Key("text", "1")).action == "none"
    assert (permission.selected, permission.hint) == (0, "")
