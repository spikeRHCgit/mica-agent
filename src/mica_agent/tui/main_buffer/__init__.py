"""主缓冲区终端前端的显式导入入口。

该子包不依赖 Textual，也不扩展 ``mica_agent`` 顶层公共 API；它只在
``--main-buffer-preview`` 临时入口下由 CLI 装配。
"""

from .components import (
    ComposerFrame,
    assistant_block,
    composer_frame,
    facts_lines,
    logo_lines,
    read_git_branch,
    reasoning_block,
    startup_blocks,
    status_line,
    tool_block,
    user_block,
)
from .editor import ComposerEditor, Key, normalize_key, windowed_lines
from .interactions import (
    AskInteraction,
    InteractionOutcome,
    PermissionInteraction,
)
from .renderer import (
    Block,
    MainBufferRenderer,
    Palette,
    cell_len,
    char_cells,
    truncate_head,
    truncate_tail,
    wrap_line,
)
from .runtime import MainBufferRuntime
from .sanitize import sanitize_scalar, sanitize_text
from .screen import MainBufferScreen, Mode, Status
from .terminal import Capabilities, Terminal, find_forbidden_sequences

__all__ = [
    "AskInteraction",
    "Block",
    "Capabilities",
    "ComposerEditor",
    "ComposerFrame",
    "InteractionOutcome",
    "Key",
    "MainBufferRenderer",
    "MainBufferRuntime",
    "MainBufferScreen",
    "Mode",
    "Palette",
    "PermissionInteraction",
    "Status",
    "Terminal",
    "assistant_block",
    "cell_len",
    "char_cells",
    "composer_frame",
    "facts_lines",
    "find_forbidden_sequences",
    "logo_lines",
    "normalize_key",
    "read_git_branch",
    "reasoning_block",
    "sanitize_scalar",
    "sanitize_text",
    "startup_blocks",
    "status_line",
    "tool_block",
    "truncate_head",
    "truncate_tail",
    "user_block",
    "windowed_lines",
    "wrap_line",
]
