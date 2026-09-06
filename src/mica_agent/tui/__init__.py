"""Mica Textual TUI 的显式导入入口。"""

from typing import TYPE_CHECKING

from .app import MicaTuiApp, RunRequester
from .composer import MicaComposer
from .interactions import (
    AskInteractionView,
    HistoricalAskRecordView,
    RunCancelRequester,
    UserInputResponder,
)
from .models import StartupResourceSnapshot
from .output import AssistantResponseView, ReasoningDisplayMode, ReasoningView
from .permissions import PermissionInteractionView, PermissionResponder
from .status import WorkPhase, WorkStatus, WorkStatusView
from .tool_cards import (
    BashToolCard,
    EditToolCard,
    GenericToolCard,
    ReadToolCard,
    ToolCard,
    ToolCardFactory,
    ToolCardState,
    ToolCardStatus,
    WriteToolCard,
    create_tool_card,
)
from .transcript import (
    ConversationTranscript,
    HistoricalToolRecordView,
    NewOutputIndicator,
)

if TYPE_CHECKING:
    from .runtime import MicaTuiRuntime


def __getattr__(name: str):
    if name == "MicaTuiRuntime":
        from .runtime import MicaTuiRuntime

        return MicaTuiRuntime
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AskInteractionView",
    "AssistantResponseView",
    "BashToolCard",
    "ConversationTranscript",
    "EditToolCard",
    "GenericToolCard",
    "HistoricalAskRecordView",
    "HistoricalToolRecordView",
    "MicaComposer",
    "MicaTuiApp",
    "MicaTuiRuntime",
    "NewOutputIndicator",
    "PermissionInteractionView",
    "PermissionResponder",
    "ReadToolCard",
    "ReasoningDisplayMode",
    "ReasoningView",
    "RunCancelRequester",
    "RunRequester",
    "StartupResourceSnapshot",
    "ToolCard",
    "ToolCardFactory",
    "ToolCardState",
    "ToolCardStatus",
    "UserInputResponder",
    "WorkPhase",
    "WorkStatus",
    "WorkStatusView",
    "WriteToolCard",
    "create_tool_card",
]
