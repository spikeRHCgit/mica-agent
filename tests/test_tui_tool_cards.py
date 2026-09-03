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
    EditToolCard,
    HistoricalToolRecordView,
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


def edit_snapshot(
    call_id: str,
    status: ToolPresentationStatus,
    *,
    matched: bool | None,
    lines: tuple[dict[str, object], ...],
    path: str = "docs/tui_design.md",
    message: str | None = None,
) -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        call_id,
        "edit",
        ToolPresentationKind.EDIT,
        status,
        {"path": path, "matched": matched, "lines": lines},
        message,
    )


def make_recorded_edit_session() -> StoredSession:
    call = ToolCall("legacy-edit", "edit", {})
    turn = TurnEntry(
        "legacy-edit-turn",
        "legacy-edit-run",
        Message(MessageRole.USER, "edit legacy file"),
        Message(MessageRole.ASSISTANT, "done"),
        NOW,
        (
            Message(MessageRole.ASSISTANT, "", (call,)),
            Message(MessageRole.TOOL, "{}", tool_call_id=call.call_id),
        ),
    )
    return StoredSession(
        SessionMeta(
            "legacy-edit-session",
            CURRENT_SESSION_FORMAT_VERSION,
            "workspace-tool-cards",
            "C:/fictional/mica",
            "legacy edit",
            NOW,
            NOW,
        ),
        (turn,),
    )


def test_edit_lifecycle_renders_diff_in_place_with_real_line_numbers() -> None:
    lines = (
        {"kind": "context", "text": "before", "old_line": 18, "new_line": 18},
        {"kind": "removed", "text": "old", "old_line": 19, "new_line": None},
        {"kind": "added", "text": "new", "old_line": None, "new_line": 19},
        {"kind": "context", "text": "after", "old_line": 20, "new_line": 20},
    )

    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("edit-run"))
            pending = edit_snapshot(
                "edit-call", ToolPresentationStatus.PENDING, matched=None, lines=lines
            )
            assert await app.project_engine_event(ToolPrepared("edit-run", 1, pending))
            await pilot.pause()
            card = app.query_one(EditToolCard)
            assert card.state is ToolCardState.PENDING
            assert "- old" in card.render().plain
            assert "+ new" in card.render().plain
            assert "19" not in card.render().plain
            await app.project_engine_event(
                ToolStarted("edit-run", 2, "edit-call", "edit")
            )
            final = edit_snapshot(
                "edit-call", ToolPresentationStatus.SUCCESS, matched=True, lines=lines
            )
            await app.project_engine_event(
                ToolFinished(
                    "edit-run", 3, "edit-call", "edit", ToolResultStatus.SUCCESS, final
                )
            )
            assert app.query_one(EditToolCard) is card
            assert card.state is ToolCardState.SUCCESS
            assert card.snapshot is final
            rendered = card.render()
            assert "- 19  old" in rendered.plain
            assert "+ 19  new" in rendered.plain
            assert "  20  after" in rendered.plain
            styles = {str(span.style) for span in rendered.spans}
            assert {"#e2e8f0", "#fca5a5", "#bef264"} <= styles

    asyncio.run(scenario())


def test_edit_unmatched_and_large_diff_fold_are_safe_and_isolated() -> None:
    unmatched_lines = (
        {"kind": "removed", "text": "old", "old_line": None, "new_line": None},
        {"kind": "added", "text": "new", "old_line": None, "new_line": None},
    )
    many_lines = tuple(
        {"kind": "added", "text": f"line {i}", "old_line": None, "new_line": i}
        for i in range(1, 23)
    )

    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("edit-a"))
            await app.project_engine_event(
                ToolPrepared(
                    "edit-a",
                    1,
                    edit_snapshot(
                        "same",
                        ToolPresentationStatus.PENDING,
                        matched=None,
                        lines=many_lines,
                    ),
                )
            )
            await app.project_engine_event(RunStarted("edit-b"))
            await app.project_engine_event(
                ToolPrepared(
                    "edit-b",
                    1,
                    edit_snapshot(
                        "same",
                        ToolPresentationStatus.PENDING,
                        matched=None,
                        lines=unmatched_lines,
                    ),
                )
            )
            large, unmatched = list(app.query(EditToolCard))
            assert large.change_line_count == 22
            assert "line 1" in large.render().plain
            assert "line 11" not in large.render().plain
            assert "change lines hidden" in large.render().plain
            assert "- old" in unmatched.render().plain
            assert "+ new" in unmatched.render().plain
            assert "file was not changed" not in unmatched.render().plain
            large.focus()
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert large.expanded is True
            assert "line 22" in large.render().plain
            assert unmatched.expanded is False

            final = edit_snapshot(
                "same",
                ToolPresentationStatus.FAILED,
                matched=False,
                lines=unmatched_lines,
                message="file was not changed",
            )
            await app.project_engine_event(
                ToolFinished(
                    "edit-b",
                    2,
                    "same",
                    "edit",
                    ToolResultStatus.EXECUTION_FAILED,
                    final,
                )
            )
            assert unmatched.state is ToolCardState.FAILED
            assert unmatched.render().plain.endswith("file was not changed")
            assert unmatched.render().plain.count("file was not changed") == 1
            assert large.expanded is True

    asyncio.run(scenario())


def test_edit_cancelled_finish_and_run_terminal_keep_isolation_and_reason() -> None:
    lines = (
        {"kind": "removed", "text": "old line", "old_line": None, "new_line": None},
        {"kind": "added", "text": "new line", "old_line": None, "new_line": None},
        {"kind": "added", "text": "", "old_line": None, "new_line": None},
    )

    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(72, 22)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("cancel-run"))
            await app.project_engine_event(
                ToolPrepared(
                    "cancel-run",
                    1,
                    edit_snapshot(
                        "same-call",
                        ToolPresentationStatus.PENDING,
                        matched=None,
                        lines=lines,
                    ),
                )
            )
            await app.project_engine_event(RunStarted("other-run"))
            await app.project_engine_event(
                ToolPrepared(
                    "other-run",
                    1,
                    edit_snapshot(
                        "same-call",
                        ToolPresentationStatus.PENDING,
                        matched=None,
                        lines=lines,
                    ),
                )
            )
            cancelled, pending = list(app.query(EditToolCard))
            final = edit_snapshot(
                "same-call",
                ToolPresentationStatus.CANCELLED,
                matched=False,
                lines=lines,
                message="operation cancelled",
            )
            await app.project_engine_event(
                ToolFinished(
                    "cancel-run",
                    2,
                    "same-call",
                    "edit",
                    ToolResultStatus.CANCELLED,
                    final,
                )
            )
            assert cancelled.state is ToolCardState.CANCELLED
            assert cancelled.styles.background.hex.casefold() == "#450a0a"
            assert "- old line" in cancelled.render().plain
            assert "+ new line" in cancelled.render().plain
            assert cancelled.render().plain.endswith(
                "operation cancelled · file was not changed"
            )
            assert pending.state is ToolCardState.PENDING
            await app.project_engine_event(RunCancelled("other-run", 2))
            assert pending.state is ToolCardState.CANCELLED
            assert cancelled.state is ToolCardState.CANCELLED

    asyncio.run(scenario())


def test_edit_fold_counts_only_changes_and_preserves_the_first_and_last_ten() -> None:
    lines = tuple(
        [{"kind": "context", "text": "context", "old_line": 1, "new_line": 1}]
        + [
            {
                "kind": "added",
                "text": f"change {i}",
                "old_line": None,
                "new_line": i + 2,
            }
            for i in range(21)
        ]
        + [{"kind": "context", "text": "tail", "old_line": 23, "new_line": 23}]
    )
    card = EditToolCard(
        edit_snapshot(
            "fold-call",
            ToolPresentationStatus.SUCCESS,
            matched=True,
            lines=lines,
        )
    )
    assert card.change_line_count == 21
    collapsed = card.render().plain
    assert "change 0" in collapsed and "change 9" in collapsed
    assert "change 10" not in collapsed
    assert "change 20" in collapsed
    assert "tail" in collapsed
    assert "1 change lines hidden" in collapsed


def test_edit_failed_run_closes_only_unfinished_cards() -> None:
    lines = (
        {"kind": "removed", "text": "old", "old_line": 3, "new_line": None},
        {"kind": "added", "text": "new", "old_line": None, "new_line": 3},
    )

    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(72, 20)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("failed-run"))
            await app.project_engine_event(
                ToolPrepared(
                    "failed-run",
                    1,
                    edit_snapshot(
                        "finished",
                        ToolPresentationStatus.PENDING,
                        matched=None,
                        lines=lines,
                    ),
                )
            )
            await app.project_engine_event(
                ToolFinished(
                    "failed-run",
                    2,
                    "finished",
                    "edit",
                    ToolResultStatus.EXECUTION_FAILED,
                    edit_snapshot(
                        "finished",
                        ToolPresentationStatus.FAILED,
                        matched=True,
                        lines=lines,
                        message="tool execution failed",
                    ),
                )
            )
            await app.project_engine_event(
                ToolPrepared(
                    "failed-run",
                    3,
                    edit_snapshot(
                        "unfinished",
                        ToolPresentationStatus.PENDING,
                        matched=None,
                        lines=lines,
                    ),
                )
            )
            finished, unfinished = list(app.query(EditToolCard))
            await app.project_engine_event(
                RunFailed("failed-run", "safe failure", ErrorCategory.UNKNOWN, 4)
            )
            assert finished.state is ToolCardState.FAILED
            assert finished.render().plain.endswith("tool execution failed")
            assert unfinished.state is ToolCardState.FAILED
            assert unfinished.render().plain.endswith(
                "tool execution failed · file was not changed"
            )

    asyncio.run(scenario())


def test_edit_failed_history_restores_and_missing_history_is_recorded() -> None:
    lines = (
        {"kind": "removed", "text": "old", "old_line": 4, "new_line": None},
        {"kind": "added", "text": "new", "old_line": None, "new_line": 4},
    )
    success = edit_snapshot(
        "success-edit",
        ToolPresentationStatus.SUCCESS,
        matched=True,
        lines=lines,
    )
    failed = edit_snapshot(
        "failed-edit",
        ToolPresentationStatus.FAILED,
        matched=True,
        lines=lines,
        message="tool execution failed",
    )
    session = make_history_session((success, failed))

    async def scenario() -> None:
        app = MicaTuiApp(stored_session=session)
        async with app.run_test(size=(72, 18)) as pilot:
            await pilot.pause()
            success_card, failed_card = list(app.query(EditToolCard))
            assert success_card.state is ToolCardState.SUCCESS
            assert "- 4  old" in success_card.render().plain
            assert failed_card.state is ToolCardState.FAILED
            assert "- 4  old" in failed_card.render().plain
            assert failed_card.render().plain.endswith("tool execution failed")

        old_app = MicaTuiApp(stored_session=make_recorded_edit_session())
        async with old_app.run_test(size=(72, 18)) as pilot:
            await pilot.pause()
            assert list(old_app.query(EditToolCard)) == []
            records = list(old_app.query(HistoricalToolRecordView))
            assert [record.render().plain for record in records] == [
                "tool edit · recorded"
            ]

    asyncio.run(scenario())
