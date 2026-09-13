"""主缓冲区的纯文本与语义展示组件。

本模块只把已经确认的安全快照和公开状态转成**语义块**或**已排版的
chrome 行**，不做 IO、不读业务对象、不导入 Textual。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from ..models import ReasoningDisplayMode
from .renderer import (
    Block,
    BlockLike,
    Palette,
    cell_len,
    expand_tabs,
    truncate_head,
    truncate_tail,
    wrap_line,
)
from .sanitize import sanitize_scalar, sanitize_text

LOGO_FULL_MIN_WIDTH = 54
LOGO_COMPACT_MIN_WIDTH = 28
RESOURCE_PANEL_WIDTH = 68
COMPOSER_MAX_LINES = 8
IDLE_HINT = "Enter to send · Ctrl+J for new line"
COMPOSER_PLACEHOLDER = "Type your request or command..."
COMPOSER_PROMPT = "› "
# 用户消息的左侧身份标记与思考块的左侧细线。
USER_MARKER = "▌"
REASONING_BAR = "│ "
REASONING_COLLAPSED_ROWS = 2
REASONING_PREVIEW_SUFFIX = "…"

_FULL_ROWS = (
    (" ███        ███ ", "LLLLL......BBBBB"),
    ("██████    ██████", "LLLLLLB..BBBBBDD"),
    ("██ ████  ████ ██", "LLBBBBBBDDDDDDDD"),
    ("██   ██████   ██", "LL..BBBBBBDD..DD"),
    ("██     ██     ██", "LL....BBBB....DD"),
    ("▀              ▀", "L......BB......D"),
)

_COMPACT_ROWS = (
    (" ██    ██ ", "LLLL..BBBB"),
    ("██ █  █ ██", "LLBBBDDDDD"),
    ("██      ██", "LL..BB..DD"),
    ("▀        ▀", "L........D"),
)

_BOX_H = "─"
_BOX_V = "│"
_BOX_TL = "┌"
_BOX_TR = "┐"
_BOX_BL = "└"
_BOX_BR = "┘"
_BOX_LT = "├"
_BOX_RT = "┤"


def _safe_line(value: object | None, *, fallback: str = "none") -> str:
    if value is None:
        return fallback
    return sanitize_scalar(value, fallback=fallback)


def logo_lines(width: int, palette: Palette) -> list[str]:
    """按终端宽度返回 Textual 前端同一几何的 Logo 行。"""
    if width < LOGO_COMPACT_MIN_WIDTH:
        return [palette.paint("Mica", "brand")]
    rows = _FULL_ROWS if width >= LOGO_FULL_MIN_WIDTH else _COMPACT_ROWS
    rendered: list[str] = []
    for row, facets in rows:
        line = ""
        for character, facet in zip(row, facets, strict=True):
            kind = {"L": "brand", "B": "status", "D": "logo"}.get(facet, "")
            line += palette.paint(character, kind) if kind else character
        rendered.append(line)
    return rendered


def _panel_lines(
    sections: list[tuple[str, list[str]]],
    width: int,
    palette: Palette,
) -> list[str]:
    inner = max(8, min(RESOURCE_PANEL_WIDTH, width) - 2)
    output: list[str] = []
    for index, (title, rows) in enumerate(sections):
        if index == 0:
            output.append(_BOX_TL + _rule(title, inner, palette, left="─ ") + _BOX_TR)
        else:
            output.append(_BOX_LT + _rule(title, inner, palette, left="─ ") + _BOX_RT)
        for row in rows:
            cleaned = sanitize_scalar(row, fallback="")
            output.append(
                _BOX_V
                + " "
                + _pad(truncate_tail(cleaned, inner - 2), inner - 2)
                + " "
                + _BOX_V
            )
    output.append(_BOX_BL + _BOX_H * inner + _BOX_BR)
    return output


def _rule(title: str, inner: int, palette: Palette, *, left: str) -> str:
    title = sanitize_scalar(title, fallback="")
    label = f"{left}{title} " if title else ""
    remaining = max(0, inner - cell_len(label))
    return palette.paint(label + _BOX_H * remaining, "muted")


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - cell_len(text))


def startup_blocks(snapshot: object, width: int, palette: Palette) -> list[Block]:
    """启动首屏：Logo、定位文案、资源面板与真实就绪说明。"""

    blocks: list[Block] = []
    for line in logo_lines(width, palette):
        blocks.append(Block("raw", line, styled=True))
    blocks.append(Block("text", "Mica — lightweight AI coding agent"))
    blocks.append(Block("brand", "Ready to build."))
    system_prompt = _safe_line(getattr(snapshot, "system_prompt_name", None))
    global_path = _safe_line(getattr(snapshot, "global_agent_path", None))
    workspace_path = _safe_line(getattr(snapshot, "workspace_agent_path", None))
    tool_names = tuple(getattr(snapshot, "tool_names", ()) or ())
    skill_names = tuple(getattr(snapshot, "skill_names", ()) or ())
    extension_names = tuple(getattr(snapshot, "extension_names", ()) or ())
    sections = [
        (
            "Context",
            [
                f"  System prompt: {system_prompt}",
                f"  Global AGENTS.md: {global_path}",
                f"  Workspace AGENTS.md: {workspace_path}",
            ],
        ),
        ("Skills", ["  " + _join_names(skill_names)]),
        ("Extensions", ["  " + _join_names(extension_names)]),
        (
            "Tools",
            [f"  {len(tool_names)} available: {_join_names(tool_names)}"],
        ),
    ]
    panel = _panel_lines(sections, width, palette)
    # 面板行已按纯文本测量/截断后再着色，且宽度小于终端宽度，
    # 因此声明为可信的不可换行行，避免二次排版切碎 ANSI 序列。
    blocks.extend(Block("raw", line, styled=True) for line in panel)
    blocks.append(Block("brand", "Mica is ready."))
    return blocks


def _join_names(names: tuple[str, ...]) -> str:
    return ", ".join(names) if names else "none"


def user_block(text: str) -> BlockLike:
    """用户消息：左侧短蓝线标识，正文按纯文本（不解释 Markdown）。"""
    return UserBlock(sanitize_text(text, newlines=True))


def assistant_block(text: str) -> BlockLike:
    """朴素的正式回复文本块（Markdown 渲染见 :mod:`markdown`）。"""
    return Block("assistant", sanitize_text(text, newlines=True))


def reasoning_block(
    text: str, mode: ReasoningDisplayMode = ReasoningDisplayMode.COLLAPSED
) -> BlockLike:
    """思考块：默认折叠，隐藏模式不产生任何行。"""
    return ReasoningBlock(sanitize_text(text, newlines=True), mode)


def status_line(
    state: str,
    *,
    elapsed: float | None = None,
) -> str | None:
    """唯一工作状态条的纯文本；空闲时不常驻。"""
    if state == "idle":
        return None
    if state == "working":
        dots = "." * (1 + (int((elapsed or 0.0) * 2) % 3))
        return f"Mica · working{dots}"
    if state == "done":
        return f"Mica · done({_duration(elapsed)})"
    if state == "stop":
        return "Mica · stop"
    return "Mica · error"


def _duration(seconds: float | None) -> str:
    total = max(0, int(seconds or 0.0))
    if total < 60:
        return f"{total}s"
    minutes, remainder = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {remainder}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


@dataclass(frozen=True, slots=True)
class UserBlock:
    """用户消息：整行低对比度深灰背景 + 左侧短蓝色身份标记，正文按纯文本。

    用户原文不被解释为 Markdown：即使正文里出现 ``#``、``**`` 或围栏，
    也只是普通字符。块自身不附加空行；相邻高层记录的间距由渲染器统一插入。
    """

    text: str = field(repr=False)
    kind: str = "user"

    def __repr__(self) -> str:
        return f"<UserBlock chars={len(self.text)}>"

    def render(self, width: int, palette: Palette, expanded: bool) -> list[str]:
        width = max(1, width)
        marker_width = cell_len(USER_MARKER)
        limit = max(1, width - marker_width - 1)
        rows = wrap_line(sanitize_text(self.text, newlines=True), limit)
        indent = " " * marker_width
        lines: list[str] = []
        for index, row in enumerate(rows):
            marker = USER_MARKER if index == 0 else indent
            lines.append(_user_line(marker, row, width, palette))
        return lines

    def split(self, *args: object, **kwargs: object) -> None:
        """用户消息不参与流式切分。"""


@dataclass(frozen=True, slots=True)
class ReasoningBlock:
    """模型思考：灰色次要文字、左侧细灰线、比正式回复更深一级的缩进。

    只有 ``ReasoningDelta`` 会创建本块；``hidden`` 不产生任何行，因此也
    不会影响相邻块间距，``collapsed``（默认）最多两行并带克制省略提示。
    """

    text: str = field(repr=False)
    mode: ReasoningDisplayMode = ReasoningDisplayMode.COLLAPSED
    kind: str = "reasoning"

    def __repr__(self) -> str:
        return f"<ReasoningBlock mode={self.mode.value} chars={len(self.text)}>"

    def render(self, width: int, palette: Palette, expanded: bool) -> list[str]:
        if self.mode is ReasoningDisplayMode.HIDDEN:
            return []
        width = max(1, width)
        body_width = max(1, width - cell_len(REASONING_BAR))
        body = sanitize_text(self.text, newlines=True)
        rows = wrap_line(body, body_width)
        if self.mode is ReasoningDisplayMode.COLLAPSED:
            rows = self._collapsed_rows(rows, body_width)
        lines: list[str] = []
        for row in rows:
            text = REASONING_BAR + row
            if cell_len(text) > width:
                text = truncate_head(text, width)
            lines.append(palette.paint(text, "reasoning"))
        return lines

    @staticmethod
    def _collapsed_rows(rows: list[str], body_width: int) -> list[str]:
        if len(rows) <= REASONING_COLLAPSED_ROWS:
            return rows
        first = rows[0]
        suffix = REASONING_PREVIEW_SUFFIX
        budget = max(0, body_width - cell_len(suffix))
        second = truncate_head(rows[1], budget) + suffix
        return [first, second]

    def split(self, *args: object, **kwargs: object) -> None:
        """思考块不参与流式切分。"""


def _user_line(marker: str, row: str, width: int, palette: Palette) -> str:
    content = f"{marker} {row}"
    if cell_len(content) > width:
        content = truncate_head(content, width)
    pad = max(0, width - cell_len(content))
    padded = content + " " * pad
    if not palette.enabled:
        return padded
    styles = palette.styles
    background = styles.get("user_bg", "")
    marker_code = ";".join(
        code for code in (styles.get("user_marker", ""), background) if code
    )
    body_code = ";".join(code for code in (styles.get("text", ""), background) if code)
    if marker == USER_MARKER and content.startswith(marker):
        rest = content[len(marker) :] + " " * pad
        marker_text = f"\x1b[{marker_code}m{marker}\x1b[0m" if marker_code else marker
        body_text = f"\x1b[{body_code}m{rest}\x1b[0m" if body_code else rest
        return marker_text + body_text
    filled = f"\x1b[{body_code}m{padded}\x1b[0m" if body_code else padded
    return filled


@dataclass(frozen=True, slots=True)
class ComposerFrame:
    """输入框的可绘制结果与光标位置。"""

    lines: list[str]
    cursor: tuple[int, int]
    row_count: int


def composer_body_width(width: int) -> int:
    """composer 正文可用列数（已扣除左右边框与提示前缀）。"""
    return max(1, composer_inner_width(width) - cell_len(COMPOSER_PROMPT))


def composer_inner_width(width: int) -> int:
    """composer 边框内部列数。"""
    return max(4, width - 2)


def _plain_rule(title: str, inner: int, *, left: str) -> str:
    """未着色的边框行；标题嵌在横线中间，方便标注上下隐藏行数。"""
    label = f"{left}{title} " if title else ""
    return label + _BOX_H * max(0, inner - cell_len(label))


def _hidden_label(count: int, arrow: str) -> str:
    return f"{arrow} {count} more" if count > 0 else ""


def composer_frame(
    draft_lines: list[str],
    cursor_row: int,
    cursor_column: int,
    *,
    width: int,
    palette: Palette,
    enabled: bool,
    hint: str = "",
    prompt: str = COMPOSER_PROMPT,
    hidden_above: int = 0,
    hidden_below: int = 0,
) -> ComposerFrame:
    """绘制完整边框输入框，并给出 chrome 内的物理光标坐标。

    正文行必须已经按 :func:`composer_body_width` 软换行；本函数只负责
    加边框、补占位提示与标注上下隐藏行数。
    """
    inner = composer_inner_width(width)
    body: list[str] = []
    if not draft_lines or (len(draft_lines) == 1 and not draft_lines[0].strip()):
        placeholder = COMPOSER_PLACEHOLDER
        body = [prompt + placeholder]
        chosen = ""
        if hint:
            for candidate in (hint, "Enter to send"):
                if (
                    cell_len(prompt) + cell_len(placeholder) + cell_len(candidate) + 3
                    <= inner
                ):
                    chosen = candidate
                    break
        if chosen:
            body[0] = (
                prompt
                + placeholder
                + " " * max(1, inner - 3 - cell_len(placeholder) - cell_len(chosen))
                + chosen
            )
    else:
        for index, text in enumerate(draft_lines[:COMPOSER_MAX_LINES]):
            prefix = prompt if index == 0 else "  "
            # 正文保留前导/尾随空格与空白行，只清控制内容并展开制表符。
            body.append(prefix + expand_tabs(sanitize_text(text, newlines=False)))
    top = (
        _BOX_TL
        + _plain_rule(_hidden_label(hidden_above, "↑"), inner, left="─ ")
        + _BOX_TR
    )
    bottom = (
        _BOX_BL
        + _plain_rule(_hidden_label(hidden_below, "↓"), inner, left="─ ")
        + _BOX_BR
    )
    lines = [top]
    for row in body[:COMPOSER_MAX_LINES]:
        lines.append(_BOX_V + _pad(truncate_head(row, inner), inner) + _BOX_V)
    lines.append(bottom)
    kind = "composer" if enabled else "muted"
    styled = [
        palette.paint(lines[0], "border"),
        *[palette.paint(line, kind) for line in lines[1:-1]],
        palette.paint(lines[-1], "border"),
    ]
    column = min(1 + cell_len(prompt) + max(0, cursor_column), 1 + inner)
    return ComposerFrame(
        lines=styled,
        cursor=(1 + min(max(cursor_row, 0), len(body) - 1), column),
        row_count=len(styled),
    )


def facts_lines(
    workspace: str | None,
    branch: str | None,
    model: str | None,
    session: str | None,
    *,
    width: int,
    palette: Palette,
) -> list[str]:
    """底部两行事实：workspace (branch) / model 与 sessions。"""
    left_value = _safe_line(workspace, fallback="none")
    if branch:
        left_value += f" ({_safe_line(branch, fallback='')})"
    right_value = _safe_line(model, fallback="none")
    model_width = min(28, max(6, min(cell_len(right_value), width // 2)))
    left_width = max(5, width - model_width - 1)
    left_text = truncate_tail(left_value, left_width)
    right_text = truncate_tail(right_value, model_width)
    gap = max(1, width - cell_len(left_text) - cell_len(right_text))
    first = (
        palette.paint(left_text, "brand")
        + " " * gap
        + palette.paint(right_text, "brand")
    )
    prefix = "sessions: "
    session_text = truncate_tail(
        _safe_line(session, fallback="none"), max(1, width - cell_len(prefix))
    )
    second = palette.paint(prefix, "facts_muted") + palette.paint(session_text, "facts")
    return [first, second]


def read_git_branch(workspace_path: object, *, timeout: float = 2.0) -> str | None:
    """读取当前工作区的 Git 分支；失败时返回 ``None``。

    这是输入区恢复时刷新的受控公开工作区事实：只返回分支名，不返回
    remote、提交、路径或任何命令输出正文。
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    branch = sanitize_scalar(completed.stdout, fallback="").strip()
    if not branch or len(branch) > 64:
        return None
    return branch


__all__ = [
    "COMPOSER_MAX_LINES",
    "COMPOSER_PLACEHOLDER",
    "IDLE_HINT",
    "REASONING_BAR",
    "REASONING_COLLAPSED_ROWS",
    "REASONING_PREVIEW_SUFFIX",
    "USER_MARKER",
    "ComposerFrame",
    "ReasoningBlock",
    "UserBlock",
    "assistant_block",
    "composer_frame",
    "facts_lines",
    "logo_lines",
    "read_git_branch",
    "reasoning_block",
    "startup_blocks",
    "status_line",
    "user_block",
]
