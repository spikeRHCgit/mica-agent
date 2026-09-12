"""不可信正文的终端安全边界测试。

覆盖两层：

1. :func:`sanitize_text` 的单元规则（删除控制内容、保留正常 Unicode）；
2. 把同样的载荷注入每一个不可信入口（用户草稿、模型正文、工具字段、
   交互文案、动态事实），再断言真实发出的字节流里既没有原始危险序列，
   也没有任何被注入的控制效果。
"""

from __future__ import annotations

import io

import pytest

from mica_agent.interactions import AskOption, AskPrompt
from mica_agent.tui.main_buffer.components import (
    assistant_block,
    composer_frame,
    facts_lines,
    reasoning_block,
    startup_blocks,
    tool_block,
    user_block,
)
from mica_agent.tui.main_buffer.interactions import (
    AskInteraction,
    PermissionInteraction,
)
from mica_agent.tui.main_buffer.renderer import Block, Palette, wrap_blocks
from mica_agent.tui.main_buffer.sanitize import sanitize_scalar, sanitize_text
from mica_agent.tui.main_buffer.terminal import (
    Terminal,
    find_forbidden_sequences,
)

# 任务单列举的对抗载荷：终端状态、剪贴板、光标、覆写与普通控制字符。
PAYLOADS = {
    "alt-screen-on": "\x1b[?1049h",
    "alt-screen-off": "\x1b[?1049l",
    "alt-47": "\x1b[?47h",
    "alt-47-off": "\x1b[?47l",
    "alt-1047": "\x1b[?1047h",
    "alt-1047-off": "\x1b[?1047l",
    "clear-scrollback": "\x1b[3J",
    "clear-screen": "\x1b[2J",
    "mouse-on": "\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h",
    "mouse-off": "\x1b[?1000l",
    "osc-title": "\x1b]0;owned\x07",
    "osc-title-2": "\x1b]2;owned\x1b\\",
    "osc-clipboard": "\x1b]52;c;aGVsbG8=\x07",
    "cursor-move": "\x1b[3A\x1b[10C\x1b[1;1H",
    "erase-line": "\x1b[2K",
    "bel": "\x07",
    "backspace-overwrite": "safe\x08\x08\x08evil",
    "cr-overwrite": "safe\rEVIL",
    "delete": "abc\x7f",
    "c1-csi": "\x9b?1049h",
    "dcs": "\x1bP1;2$qdata\x1b\\",
    "ris": "\x1bc",
}


def make_terminal(environ: dict[str, str] | None = None) -> Terminal:
    return Terminal(
        output=io.StringIO(),
        input_factory=lambda: None,  # type: ignore[arg-type]
        size=(100, 30),
        interactive=True,
        environ={} if environ is None else environ,
    )


def emitted(terminal: Terminal) -> str:
    return terminal.emitted_text()


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_sanitize_removes_whole_control_payload(name: str) -> None:
    raw = PAYLOADS[name]
    cleaned = sanitize_text(raw)
    assert "\x1b" not in cleaned
    assert "\x07" not in cleaned
    assert "\x9b" not in cleaned
    # 序列被整段删除，而不是只删掉 ESC 留下可见残渣。
    assert "[?1049h" not in cleaned
    assert "]52;c;" not in cleaned
    assert "[3A" not in cleaned
    assert find_forbidden_sequences(cleaned) == []


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_sanitized_payload_cannot_reform_a_sequence(name: str) -> None:
    raw = PAYLOADS[name]
    once = sanitize_text(raw)
    assert sanitize_text(once) == once


def test_sanitize_preserves_unicode_and_expected_layout() -> None:
    text = "中文标题 🙂 👨‍👩‍👧 e\u0301\n第二行\tTab"
    assert sanitize_text(text) == text
    assert sanitize_scalar("  中文  ") == "中文"
    assert sanitize_scalar("\x1b[?1049h", fallback="none") == "none"


def test_sanitize_normalises_crlf_and_flatens_single_line_fields() -> None:
    assert sanitize_text("a\r\nb\rc") == "a\nb\nc"
    assert sanitize_text("a\r\nb", newlines=False) == "a b"


def test_detector_catches_effects_not_only_known_strings() -> None:
    assert find_forbidden_sequences("\x1b[?1049h") == ["CSI ?1049h"]
    assert find_forbidden_sequences("\x1b[?25l\x1b[?7l\x1b[J") == []
    assert find_forbidden_sequences("plain text\r\n") == []
    assert find_forbidden_sequences("\x1b]2;t\x1b\\") == ["OSC 2"]


def _render(terminal: Terminal, blocks: list[Block], width: int = 100) -> str:
    palette = Palette(True)
    for line in wrap_blocks(blocks, width):
        terminal.write(palette.paint(line, "text") + "\r\n")
    return emitted(terminal)


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_untrusted_text_in_document_blocks_never_reaches_the_stream(
    name: str,
) -> None:
    raw = PAYLOADS[name]
    terminal = make_terminal()
    blocks = [
        user_block(f"ask {raw}"),
        assistant_block(f"answer {raw}"),
        reasoning_block(f"think {raw}"),
        tool_block(f"tool{raw}", "success", f"detail{raw}"),
        Block("text", f"raw {raw}"),
    ]
    stream = _render(terminal, blocks)
    assert raw not in stream
    assert "?1049h" not in stream
    assert "52;c;" not in stream
    assert find_forbidden_sequences(stream) == []


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_untrusted_text_in_facts_and_panels_never_reaches_the_stream(
    name: str,
) -> None:
    raw = PAYLOADS[name]
    terminal = make_terminal()
    palette = Palette(True)
    rows: list[str] = []
    rows.extend(
        facts_lines(
            f"/work{raw}",
            f"main{raw}",
            f"model{raw}",
            f"session{raw}",
            width=100,
            palette=palette,
        )
    )
    ask = AskInteraction(
        "ask-1",
        AskPrompt(
            f"question {raw}",
            options=(
                AskOption(f"option {raw}", f"description {raw}"),
                AskOption("second"),
            ),
            allow_custom=True,
        ),
    )
    ask.note = f"note {raw}"
    ask.custom = f"custom {raw}"
    ask.editing = "notes"
    rows.extend(ask.panel_lines(100, palette))
    ask.editing = "custom"
    rows.extend(ask.panel_lines(100, palette))
    permission = PermissionInteraction("perm-1", f"tool{raw}", f"detail{raw}")
    rows.extend(permission.panel_lines(100, palette))
    frame = composer_frame(
        [f"draft {raw}"], 0, 0, width=100, palette=palette, enabled=True
    )
    rows.extend(frame.lines)
    for row in rows:
        terminal.write(row + "\r\n")
    stream = emitted(terminal)
    assert raw not in stream
    assert "?1049h" not in stream
    assert find_forbidden_sequences(stream) == []


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_startup_panel_values_are_sanitized(name: str) -> None:
    raw = PAYLOADS[name]

    class Snapshot:
        system_prompt_name = f"sys{raw}"
        global_agent_path = f"/global{raw}"
        workspace_agent_path = f"/ws{raw}"
        tool_names = (f"read{raw}",)
        skill_names = (f"skill{raw}",)
        extension_names = (f"ext{raw}",)

    terminal = make_terminal()
    palette = Palette(True)
    stream = _render(terminal, startup_blocks(Snapshot(), 100, palette))
    assert raw not in stream
    assert find_forbidden_sequences(stream) == []


def test_no_color_mode_never_leaks_color_sequences_with_injection() -> None:
    terminal = make_terminal(environ={"NO_COLOR": "1"})
    palette = Palette(False)
    rows = facts_lines(
        "/work\x1b[?1049h", "main", "model", "session", width=60, palette=palette
    )
    frame = composer_frame(
        ["draft"], 0, 0, width=60, palette=palette, enabled=True, hidden_above=1
    )
    for row in [*rows, *frame.lines]:
        terminal.write(row + "\r\n")
    stream = emitted(terminal)
    assert "\x1b[38;2;" not in stream
    assert "\x1b[48;2;" not in stream
    assert find_forbidden_sequences(stream) == []
