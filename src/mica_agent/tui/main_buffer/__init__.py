"""Explicit import boundary for the main-buffer terminal frontend.

This Textual-independent package is the default terminal frontend for ``mica cli`` and
does not extend the top-level ``mica_agent`` public API.
"""

from .components import (
    ComposerFrame,
    ReasoningBlock,
    UserBlock,
    assistant_block,
    composer_frame,
    facts_lines,
    logo_lines,
    read_git_branch,
    reasoning_block,
    startup_blocks,
    status_line,
    user_block,
)
from .editor import ComposerEditor, Key, normalize_key, windowed_lines
from .interactions import (
    AskInteraction,
    InteractionOutcome,
    PermissionInteraction,
)
from .markdown import (
    MarkdownBlock,
    StreamingMarkdownBlock,
    render_markdown,
    render_markdown_preview,
)
from .renderer import (
    Block,
    BlockLike,
    MainBufferRenderer,
    Palette,
    RenderedBlock,
    cell_len,
    char_cells,
    strip_ansi,
    truncate_head,
    truncate_tail,
    wrap_line,
)
from .runtime import MainBufferRuntime
from .sanitize import sanitize_scalar, sanitize_text
from .screen import MainBufferScreen, Mode, Status
from .terminal import Capabilities, Terminal, find_forbidden_sequences
from .tool_views import (
    AskToolView,
    BashToolView,
    EditToolView,
    GenericToolView,
    ReadToolView,
    ToolView,
    ToolViewFactory,
    WriteToolView,
)

__all__ = [
    "AskInteraction",
    "AskToolView",
    "BashToolView",
    "Block",
    "BlockLike",
    "Capabilities",
    "ComposerEditor",
    "ComposerFrame",
    "EditToolView",
    "GenericToolView",
    "InteractionOutcome",
    "Key",
    "MainBufferRenderer",
    "MainBufferRuntime",
    "MainBufferScreen",
    "MarkdownBlock",
    "Mode",
    "Palette",
    "PermissionInteraction",
    "ReadToolView",
    "ReasoningBlock",
    "RenderedBlock",
    "Status",
    "StreamingMarkdownBlock",
    "Terminal",
    "ToolView",
    "ToolViewFactory",
    "UserBlock",
    "WriteToolView",
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
    "render_markdown",
    "render_markdown_preview",
    "sanitize_scalar",
    "sanitize_text",
    "startup_blocks",
    "status_line",
    "strip_ansi",
    "truncate_head",
    "truncate_tail",
    "user_block",
    "windowed_lines",
    "wrap_line",
]
