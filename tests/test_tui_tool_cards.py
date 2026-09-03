"""P6-05A tool-card and read lifecycle public behavior tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from mica_agent.events import (
    ErrorCategory,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    ToolFinished,
    ToolPrepared,
    ToolStarted,
)
from mica_agent.jsonl_store import StoredSession
from mica_agent.models import Message, MessageRole
from mica_agent.session_contracts import (
    CURRENT_SESSION_FORMAT_VERSION,
    SessionMeta,
    TurnEntry,
)
from mica_agent.tool_contracts import ToolCall, ToolResultStatus
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)
from mica_agent.tui import MicaTuiApp, ReadToolCard, ToolCardState

NOW = datetime(2026, 9, 3, tzinfo=UTC)


def read_snapshot(
    call_id: str,
    status: ToolPresentationStatus,
    *,
    path: str = "docs/tui_design.md",
    message: str | None = None,
) -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        call_id,
        "read",
        ToolPresentationKind.READ,
        status,
        {"path": path},
        message,
    )


def other_snapshot(
    call_id: str,
    status: ToolPresentationStatus,
) -> ToolPresentationSnapshot:
    payload = {"path": "notes.md", "content": "safe body"}
    return ToolPresentationSnapshot(
        call_id,
        "write",
        ToolPresentationKind.WRITE,
        status,
        payload,
        None if status is ToolPresentationStatus.SUCCESS else "tool execution failed",
    )


def make_history_session(
    snapshots: tuple[ToolPresentationSnapshot, ...],
) -> StoredSession:
    calls = tuple(
        ToolCall(snapshot.call_id, snapshot.tool_name, {}) for snapshot in snapshots
    )
    turn = TurnEntry(
        "turn-tool-cards",
        "historical-run",
        Message(MessageRole.USER, "use tools"),
        Message(MessageRole.ASSISTANT, "done"),
        NOW,
        (Message(MessageRole.ASSISTANT, "", calls),),
        snapshots,
    )
    return StoredSession(
        SessionMeta(
            "session-tool-cards",
            CURRENT_SESSION_FORMAT_VERSION,
            "workspace-tool-cards",
            "C:/fictional/mica",
            "cards",
            NOW,
            NOW,
        ),
        (turn,),
    )


def test_read_lifecycle_updates_one_widget_in_place() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            pending = read_snapshot("call-1", ToolPresentationStatus.PENDING)
            assert await app.project_engine_event(ToolPrepared("run-1", 1, pending))
            await pilot.pause()
            card = app.query_one(ReadToolCard)
            assert card.state is ToolCardState.PENDING
            assert card.styles.background.hex == "#172554"

            assert await app.project_engine_event(
                ToolStarted("run-1", 2, "call-1", "read")
            )
            assert app.query_one(ReadToolCard) is card
            assert card.state is ToolCardState.RUNNING

            final = read_snapshot("call-1", ToolPresentationStatus.SUCCESS)
            assert await app.project_engine_event(
                ToolFinished(
                    "run-1",
                    3,
                    "call-1",
                    "read",
                    ToolResultStatus.SUCCESS,
                    final,
                )
            )
            await pilot.pause()
            assert app.query_one(ReadToolCard) is card
            assert len(app.query(ReadToolCard)) == 1
            assert card.state is ToolCardState.SUCCESS
            assert card.snapshot is final
            assert card.styles.background.hex.casefold() == "#052e16"
            assert card.render().plain == "read  docs/tui_design.md"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("result_status", "presentation_status", "reason", "expected_state"),
    [
        (
            ToolResultStatus.EXECUTION_FAILED,
            ToolPresentationStatus.FAILED,
            "tool execution failed",
            ToolCardState.FAILED,
        ),
        (
            ToolResultStatus.CANCELLED,
            ToolPresentationStatus.CANCELLED,
            "operation cancelled",
            ToolCardState.CANCELLED,
        ),
    ],
)
def test_read_failure_and_cancellation_are_red_with_safe_single_line_reason(
    result_status: ToolResultStatus,
    presentation_status: ToolPresentationStatus,
    reason: str,
    expected_state: ToolCardState,
) -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(32, 12)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared(
                    "run",
                    1,
                    read_snapshot("call", ToolPresentationStatus.PENDING),
                )
            )
            card = app.query_one(ReadToolCard)
            final = read_snapshot("call", presentation_status, message=reason)
            await app.project_engine_event(
                ToolFinished("run", 2, "call", "read", result_status, final)
            )
            await pilot.pause()
            assert card.state is expected_state
            assert card.styles.background.hex.casefold() == "#450a0a"
            assert card.region.height == 1
            assert card.render().plain.endswith(f" · {reason}")
            assert "Traceback" not in card.render().plain

    asyncio.run(scenario())


def test_missing_or_mismatched_lifecycle_events_do_not_create_cards() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(60, 16)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            assert not await app.project_engine_event(
                ToolStarted("run", 1, "missing", "read")
            )
            assert not await app.project_engine_event(
                ToolFinished("run", 2, "missing", "read", ToolResultStatus.SUCCESS)
            )
            assert not await app.project_engine_event(
                ToolPrepared(
                    "run",
                    3,
                    other_snapshot("write-call", ToolPresentationStatus.PENDING),
                )
            )
            assert list(app.query(ReadToolCard)) == []

    asyncio.run(scenario())


def test_same_call_id_is_isolated_between_runs() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(60, 18)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run-a"))
            await app.project_engine_event(
                ToolPrepared(
                    "run-a",
                    1,
                    read_snapshot("same", ToolPresentationStatus.PENDING, path="a.md"),
                )
            )
            await app.project_engine_event(RunStarted("run-b"))
            await app.project_engine_event(
                ToolPrepared(
                    "run-b",
                    1,
                    read_snapshot("same", ToolPresentationStatus.PENDING, path="b.md"),
                )
            )
            first, second = list(app.query(ReadToolCard))
            await app.project_engine_event(
                ToolFinished(
                    "run-a",
                    2,
                    "same",
                    "read",
                    ToolResultStatus.SUCCESS,
                    read_snapshot("same", ToolPresentationStatus.SUCCESS, path="a.md"),
                )
            )
            assert first.state is ToolCardState.SUCCESS
            assert second.state is ToolCardState.PENDING

    asyncio.run(scenario())


def test_run_terminal_closes_only_unfinished_cards_for_that_run() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(60, 18)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run-a"))
            await app.project_engine_event(
                ToolPrepared(
                    "run-a",
                    1,
                    read_snapshot(
                        "done", ToolPresentationStatus.PENDING, path="done.md"
                    ),
                )
            )
            await app.project_engine_event(
                ToolFinished(
                    "run-a",
                    2,
                    "done",
                    "read",
                    ToolResultStatus.SUCCESS,
                    read_snapshot(
                        "done", ToolPresentationStatus.SUCCESS, path="done.md"
                    ),
                )
            )
            await app.project_engine_event(
                ToolPrepared(
                    "run-a",
                    3,
                    read_snapshot(
                        "open", ToolPresentationStatus.PENDING, path="open.md"
                    ),
                )
            )
            await app.project_engine_event(RunStarted("run-b"))
            await app.project_engine_event(
                ToolPrepared(
                    "run-b",
                    1,
                    read_snapshot(
                        "other", ToolPresentationStatus.PENDING, path="other.md"
                    ),
                )
            )
            done, open_card, other = list(app.query(ReadToolCard))
            await app.project_engine_event(
                RunFailed("run-a", "safe run failure", ErrorCategory.UNKNOWN, 4)
            )
            assert done.state is ToolCardState.SUCCESS
            assert open_card.state is ToolCardState.FAILED
            assert open_card.render().plain.endswith("tool execution failed")
            assert other.state is ToolCardState.PENDING
            await app.project_engine_event(RunCancelled("run-b", 2))
            assert other.state is ToolCardState.CANCELLED
            assert other.render().plain.endswith("operation cancelled")

    asyncio.run(scenario())


def test_completed_run_does_not_recolor_successful_card() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(50, 14)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared(
                    "run", 1, read_snapshot("call", ToolPresentationStatus.PENDING)
                )
            )
            await app.project_engine_event(
                ToolFinished(
                    "run",
                    2,
                    "call",
                    "read",
                    ToolResultStatus.SUCCESS,
                    read_snapshot("call", ToolPresentationStatus.SUCCESS),
                )
            )
            card = app.query_one(ReadToolCard)
            await app.project_engine_event(RunCompleted("run", 3))
            assert card.state is ToolCardState.SUCCESS
            assert card.styles.background.hex.casefold() == "#052e16"

    asyncio.run(scenario())


def test_read_render_styles_name_and_relative_path_without_private_details() -> None:
    snapshot = read_snapshot("private-call-id", ToolPresentationStatus.SUCCESS)
    card = ReadToolCard(snapshot)
    rendered = card.render()
    assert rendered.plain == "read  docs/tui_design.md"
    assert rendered.spans[0].style == "bold"
    assert rendered.spans[1].style == "underline #7dd3fc"
    blob = rendered.plain + repr(card)
    for hidden in ("private-call-id", "line 12", "123 bytes", "file body"):
        assert hidden not in blob
    assert repr(card) == "<ReadToolCard>"
