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
from mica_agent.tui import (
    MicaTuiApp,
    ReadToolCard,
    ToolCardState,
    WriteToolCard,
)

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
        (Message(MessageRole.ASSISTANT, "", calls),)
        + tuple(
            Message(MessageRole.TOOL, "{}", tool_call_id=call.call_id) for call in calls
        ),
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
            assert await app.project_engine_event(
                ToolPrepared(
                    "run",
                    3,
                    other_snapshot("write-call", ToolPresentationStatus.PENDING),
                )
            )
            assert list(app.query(ReadToolCard)) == []
            assert len(app.query(WriteToolCard)) == 1

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


def write_snapshot(
    call_id: str,
    status: ToolPresentationStatus,
    *,
    content: str,
    path: str = "docs/tui_design.md",
    message: str | None = None,
) -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        call_id,
        "write",
        ToolPresentationKind.WRITE,
        status,
        {"path": path, "content": content},
        message,
    )


def test_write_lifecycle_uses_one_card_and_preserves_plain_content() -> None:
    content = "# title\n**literal**\n`code`\n缩进    保留"

    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("write-run"))
            pending = write_snapshot(
                "write-call", ToolPresentationStatus.PENDING, content=content
            )
            assert await app.project_engine_event(ToolPrepared("write-run", 1, pending))
            await pilot.pause()
            card = app.query_one(WriteToolCard)
            assert card.state is ToolCardState.PENDING
            assert card.render().plain == f"write  docs/tui_design.md\n\n{content}"

            assert await app.project_engine_event(
                ToolStarted("write-run", 2, "write-call", "write")
            )
            assert app.query_one(WriteToolCard) is card
            assert card.state is ToolCardState.RUNNING

            final = write_snapshot(
                "write-call", ToolPresentationStatus.SUCCESS, content=content
            )
            assert await app.project_engine_event(
                ToolFinished(
                    "write-run",
                    3,
                    "write-call",
                    "write",
                    ToolResultStatus.SUCCESS,
                    final,
                )
            )
            await pilot.pause()
            assert app.query_one(WriteToolCard) is card
            assert len(app.query(WriteToolCard)) == 1
            assert card.state is ToolCardState.SUCCESS
            assert card.snapshot is final
            assert card.render().plain == f"write  docs/tui_design.md\n\n{content}"
            assert "path:" not in card.render().plain
            assert "content:" not in card.render().plain

    asyncio.run(scenario())


def test_write_line_count_fold_and_focus_only_ctrl_o() -> None:
    content = "\r\n".join(f"line {index}" for index in range(1, 16)) + "\r\n"

    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 28)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared(
                    "run",
                    1,
                    write_snapshot(
                        "call", ToolPresentationStatus.PENDING, content=content
                    ),
                )
            )
            await pilot.pause()
            card = app.query_one(WriteToolCard)
            assert card.logical_line_count == 15
            assert card.expanded is False
            rendered = card.render().plain
            assert "line 1" in rendered and "line 10" in rendered
            assert "line 11" not in rendered
            assert "... (5 more lines, 15 total, ctrl+o to expand)" in rendered

            card.focus()
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert card.expanded is True
            assert card.logical_line_count == 15
            assert "line 15" in card.render().plain
            assert "... (15 total, ctrl+o to collapse)" in card.render().plain
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert card.expanded is False
            assert "line 15" not in card.render().plain

    asyncio.run(scenario())


def test_short_write_keeps_blank_lines_unicode_and_trailing_newline_without_fold() -> (
    None
):
    content = "first\n\n  缩进\n😀\nlast\n"
    card = WriteToolCard(
        write_snapshot("call", ToolPresentationStatus.SUCCESS, content=content)
    )
    assert card.logical_line_count == 5
    assert card.expanded is False
    rendered = card.render().plain
    assert rendered == "write  docs/tui_design.md\n\nfirst\n\n  缩进\n😀\nlast"
    assert "more lines" not in rendered


def test_narrow_terminal_soft_wrap_does_not_change_write_logical_lines() -> None:
    content = "a" * 100

    async def scenario() -> None:
        app = MicaTuiApp(ansi_color=False)
        async with app.run_test(size=(24, 12)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared(
                    "run",
                    1,
                    write_snapshot(
                        "call", ToolPresentationStatus.PENDING, content=content
                    ),
                )
            )
            await pilot.pause()
            card = app.query_one(WriteToolCard)
            assert card.logical_line_count == 1
            assert card.render().plain.endswith(content)
            assert card.region.height > 1
            await pilot.resize_terminal(80, 20)
            await pilot.pause()
            assert card.logical_line_count == 1
            assert card.render().plain.endswith(content)

    asyncio.run(scenario())


def test_write_failure_reason_stays_visible_when_content_is_folded_and_snapshot_unchanged() -> (
    None
):
    content = "\n".join(f"body {index}" for index in range(1, 16))
    pending = write_snapshot("call", ToolPresentationStatus.PENDING, content=content)
    final = write_snapshot(
        "call",
        ToolPresentationStatus.FAILED,
        content=content,
        path="safe/relative.md",
        message="permission denied",
    )

    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(70, 24)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(ToolPrepared("run", 1, pending))
            card = app.query_one(WriteToolCard)
            card.focus()
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert card.expanded is True
            await app.project_engine_event(
                ToolFinished(
                    "run", 2, "call", "write", ToolResultStatus.EXECUTION_FAILED, final
                )
            )
            await pilot.pause()
            assert card.state is ToolCardState.FAILED
            assert card.expanded is True
            assert card.render().plain.endswith("permission denied")
            assert card.snapshot is final
            assert "body 15" in card.render().plain
            assert card.styles.background.hex.casefold() == "#450a0a"

    asyncio.run(scenario())


def test_write_run_cancel_closes_only_unfinished_write_card() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(70, 22)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run-a"))
            await app.project_engine_event(
                ToolPrepared(
                    "run-a",
                    1,
                    write_snapshot(
                        "done", ToolPresentationStatus.PENDING, content="done"
                    ),
                )
            )
            await app.project_engine_event(
                ToolFinished(
                    "run-a",
                    2,
                    "done",
                    "write",
                    ToolResultStatus.SUCCESS,
                    write_snapshot(
                        "done", ToolPresentationStatus.SUCCESS, content="done"
                    ),
                )
            )
            await app.project_engine_event(
                ToolPrepared(
                    "run-a",
                    3,
                    write_snapshot(
                        "open", ToolPresentationStatus.PENDING, content="open"
                    ),
                )
            )
            await app.project_engine_event(RunStarted("run-b"))
            await app.project_engine_event(
                ToolPrepared(
                    "run-b",
                    1,
                    write_snapshot(
                        "other", ToolPresentationStatus.PENDING, content="other"
                    ),
                )
            )
            done, open_card, other = list(app.query(WriteToolCard))
            await app.project_engine_event(RunCancelled("run-a", 4))
            assert done.state is ToolCardState.SUCCESS
            assert open_card.state is ToolCardState.CANCELLED
            assert other.state is ToolCardState.PENDING
            await app.project_engine_event(RunCancelled("run-b", 2))
            assert other.state is ToolCardState.CANCELLED

    asyncio.run(scenario())


def test_write_v3_history_restores_final_card_and_old_history_stays_recorded() -> None:
    snapshots = (
        write_snapshot(
            "write-ok", ToolPresentationStatus.SUCCESS, content="# safe\n正文"
        ),
    )
    session = make_history_session(snapshots)

    async def scenario() -> None:
        app = MicaTuiApp(stored_session=session)
        async with app.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            card = app.query_one(WriteToolCard)
            assert card.state is ToolCardState.SUCCESS
            assert card.render().plain == "write  docs/tui_design.md\n\n# safe\n正文"
            assert card.logical_line_count == 2
            assert app.query_one("#work-status").render().plain == "Mica · ready"

    asyncio.run(scenario())
