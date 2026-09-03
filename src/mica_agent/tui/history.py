"""StoredSession 到 TUI 历史视图的只读投影。"""

from __future__ import annotations

from collections.abc import Iterator

from mica_agent.jsonl_store import StoredSession
from mica_agent.session_contracts import TurnEntry
from mica_agent.tool_presentation import ToolPresentationKind

from .output import AssistantResponseView
from .tool_cards import BashToolCard, EditToolCard, ReadToolCard, WriteToolCard
from .transcript import HistoricalToolRecordView, UserMessageCard

RECORDED_STATUS = "recorded"

HistoryView = (
    UserMessageCard
    | HistoricalToolRecordView
    | ReadToolCard
    | WriteToolCard
    | EditToolCard
    | BashToolCard
    | AssistantResponseView
)


def iter_history_views(stored_session: StoredSession | None) -> Iterator[HistoryView]:
    """按持久化顺序产出历史视图，不重放 Run、不重新执行工具。"""
    if stored_session is None:
        return
    for turn in stored_session.turns:
        yield from _iter_turn_views(turn)


def _iter_turn_views(turn: TurnEntry) -> Iterator[HistoryView]:
    yield UserMessageCard.from_text(turn.user_message.content)
    presentations = {
        presentation.call_id: presentation for presentation in turn.tool_presentations
    }
    for message in turn.intermediate_messages:
        for call in message.tool_calls:
            presentation = presentations.get(call.call_id)
            if (
                presentation is not None
                and presentation.kind is ToolPresentationKind.READ
            ):
                yield ReadToolCard(presentation)
                continue
            if (
                presentation is not None
                and presentation.kind is ToolPresentationKind.WRITE
            ):
                yield WriteToolCard(presentation)
                continue
            if (
                presentation is not None
                and presentation.kind is ToolPresentationKind.EDIT
            ):
                yield EditToolCard(presentation)
                continue
            if (
                presentation is not None
                and presentation.kind is ToolPresentationKind.BASH
            ):
                yield BashToolCard(presentation)
                continue
            status = (
                presentation.status.value
                if presentation is not None
                else RECORDED_STATUS
            )
            yield HistoricalToolRecordView(call.name, status, presentation=presentation)
    yield AssistantResponseView.from_history(turn.assistant_message.content)
