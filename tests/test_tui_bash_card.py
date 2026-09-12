"""P6-05D Bash tool-card public behavior tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from mica_agent.events import (
    ErrorCategory,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    ToolFinished,
    ToolOutputDelta,
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
    BashToolCard,
    ConversationTranscript,
    HistoricalToolRecordView,
    MicaTuiApp,
    ToolCardState,
)

NOW = datetime(2026, 9, 3, tzinfo=UTC)


def bash_snapshot(
    call_id: str,
    status: ToolPresentationStatus,
    *,
    command: str = "printf safe",
    output: str = "",
    exit_code: int | None = None,
    message: str | None = None,
) -> ToolPresentationSnapshot:
    payload = {"command": command}
    if status is not ToolPresentationStatus.PENDING:
        payload.update(output=output, exit_code=exit_code)
    return ToolPresentationSnapshot(
        call_id,
        "bash",
        ToolPresentationKind.BASH,
        status,
        payload,
        message,
    )


def make_history_session(
    snapshots: tuple[ToolPresentationSnapshot, ...],
) -> StoredSession:
    calls = (
        tuple(
            ToolCall(snapshot.call_id, "bash", {"command": "hidden"})
            for snapshot in snapshots
        )
        if snapshots
        else (ToolCall("legacy", "bash", {"command": "hidden"}),)
    )
    turn = TurnEntry(
        "bash-turn",
        "historical-run",
        Message(MessageRole.USER, "use bash"),
        Message(MessageRole.ASSISTANT, "done"),
        NOW,
        (
            Message(MessageRole.ASSISTANT, "", calls),
            *(
                Message(MessageRole.TOOL, "{}", tool_call_id=call.call_id)
                for call in calls
            ),
        ),
        snapshots,
    )
    return StoredSession(
        SessionMeta(
            "bash-session",
            CURRENT_SESSION_FORMAT_VERSION,
            "workspace-bash",
            "C:/fictional/mica",
            "bash",
            NOW,
            NOW,
        ),
        (turn,),
    )


def test_bash_lifecycle_streams_in_place_and_final_output_is_authoritative() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run"))
            pending = bash_snapshot(
                "call", ToolPresentationStatus.PENDING, command="printf safe"
            )
            assert await app.project_engine_event(ToolPrepared("run", 1, pending))
            await pilot.pause()
            card = app.query_one(BashToolCard)
            assert card.state is ToolCardState.PENDING
            assert card.render().plain == "bash\n$ printf safe"
            assert await app.project_engine_event(ToolStarted("run", 2, "call", "bash"))
            assert app.query_one(BashToolCard) is card
            assert card.state is ToolCardState.RUNNING
            assert await app.project_engine_event(
                ToolOutputDelta("run", 3, "call", "bash", "first\n")
            )
            assert await app.project_engine_event(
                ToolOutputDelta("run", 4, "call", "bash", "second\n")
            )
            final = bash_snapshot(
                "call",
                ToolPresentationStatus.SUCCESS,
                output="first\nsecond\n",
                exit_code=0,
            )
            assert await app.project_engine_event(
                ToolFinished("run", 5, "call", "bash", ToolResultStatus.SUCCESS, final)
            )
            await pilot.pause()
            assert app.query_one(BashToolCard) is card
            assert card.state is ToolCardState.SUCCESS
            assert card.output == "first\nsecond\n"
            assert card.render().plain.count("first") == 1
            assert card.render().plain.count("second") == 1
            assert card.snapshot is final
            assert await app.project_engine_event(RunCompleted("run", 6))

    asyncio.run(scenario())


def test_bash_output_routes_only_to_running_matching_cards() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(72, 20)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared(
                    "run", 1, bash_snapshot("bash-call", ToolPresentationStatus.PENDING)
                )
            )
            card = app.query_one(BashToolCard)
            assert not await app.project_engine_event(
                ToolOutputDelta("run", 2, "bash-call", "read", "wrong tool")
            )
            assert not await app.project_engine_event(
                ToolOutputDelta("other-run", 1, "bash-call", "bash", "wrong run")
            )
            assert card.output == ""
            await app.project_engine_event(ToolStarted("run", 3, "bash-call", "bash"))
            assert await app.project_engine_event(
                ToolOutputDelta("run", 4, "bash-call", "bash", "safe\n")
            )
            assert not await app.project_engine_event(
                ToolOutputDelta("run", 5, "missing", "bash", "unknown")
            )
            await app.project_engine_event(
                ToolFinished(
                    "run",
                    6,
                    "bash-call",
                    "bash",
                    ToolResultStatus.SUCCESS,
                    bash_snapshot(
                        "bash-call", ToolPresentationStatus.SUCCESS, output="safe\n"
                    ),
                )
            )
            assert not await app.project_engine_event(
                ToolOutputDelta("run", 7, "bash-call", "bash", "late")
            )
            assert card.output == "safe\n"

    asyncio.run(scenario())


def test_bash_command_fold_and_output_browse_are_local_to_one_card() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(44, 24)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            command = "one\ntwo\nthree\nfour\nfive"
            await app.project_engine_event(
                ToolPrepared(
                    "run",
                    1,
                    bash_snapshot(
                        "call", ToolPresentationStatus.PENDING, command=command
                    ),
                )
            )
            await app.project_engine_event(ToolStarted("run", 2, "call", "bash"))
            card = app.query_one(BashToolCard)
            for index in range(10):
                await app.project_engine_event(
                    ToolOutputDelta("run", index + 3, "call", "bash", f"line {index}\n")
                )
            await pilot.pause()
            assert "... (" in card.render().plain
            assert "line 9" in card.render().plain
            card.focus()
            await pilot.press("up")
            assert card.output_following is False
            assert card.output_start == 1
            await app.project_engine_event(
                ToolOutputDelta("run", 13, "call", "bash", "line 10\n")
            )
            assert card.unread_lines == 1
            assert "↓ 1 new lines" in card.render().plain
            await pilot.press("end")
            assert card.output_following is True
            assert card.unread_lines == 0
            assert "line 10" in card.render().plain
            await pilot.press("ctrl+o")
            assert card.expanded is True
            assert "ctrl+o to collapse" in card.render().plain
            await pilot.press("ctrl+o")
            assert card.expanded is False

    asyncio.run(scenario())


def test_bash_terminal_states_keep_safe_text_and_exit_information() -> None:
    success = BashToolCard(
        bash_snapshot("success", ToolPresentationStatus.SUCCESS, output="", exit_code=0)
    )
    failed = BashToolCard(
        bash_snapshot(
            "card-2",
            ToolPresentationStatus.FAILED,
            command="printf [REDACTED]",
            output="plain Unicode 中文\n",
            exit_code=7,
            message="tool execution failed",
        )
    )
    cancelled = BashToolCard(
        bash_snapshot(
            "id-c",
            ToolPresentationStatus.CANCELLED,
            output="partial\n",
            exit_code=None,
            message="operation cancelled",
        )
    )
    assert "(no output)" in success.render().plain
    assert "exit 7" in failed.render().plain
    assert "plain Unicode 中文" in failed.render().plain
    assert "[REDACTED]" in failed.render().plain
    assert "operation cancelled" in cancelled.render().plain
    for card in (success, failed, cancelled):
        rendered = card.render().plain + repr(card)
        for secret in (
            card.call_id,
            "run-id",
            "C:\\workspace",
            "C:/workspace",
            "cmd.exe",
        ):
            assert secret not in rendered
    assert repr(failed) == "<BashToolCard>"


def test_bash_run_terminal_closes_only_unfinished_cards() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(72, 20)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared(
                    "run", 1, bash_snapshot("done", ToolPresentationStatus.PENDING)
                )
            )
            await app.project_engine_event(
                ToolPrepared(
                    "run", 2, bash_snapshot("open", ToolPresentationStatus.PENDING)
                )
            )
            await app.project_engine_event(ToolStarted("run", 3, "done", "bash"))
            await app.project_engine_event(ToolStarted("run", 4, "open", "bash"))
            await app.project_engine_event(
                ToolOutputDelta("run", 5, "open", "bash", "partial\n")
            )
            await app.project_engine_event(
                ToolFinished(
                    "run",
                    6,
                    "done",
                    "bash",
                    ToolResultStatus.SUCCESS,
                    bash_snapshot(
                        "done", ToolPresentationStatus.SUCCESS, output="done\n"
                    ),
                )
            )
            done, open_card = list(app.query(BashToolCard))
            await app.project_engine_event(RunCancelled("run", 7))
            assert done.state is ToolCardState.SUCCESS
            assert open_card.state is ToolCardState.CANCELLED
            assert open_card.output == "partial\n"
            assert "operation cancelled" in open_card.render().plain

    asyncio.run(scenario())


def test_bash_history_restores_v3_cards_and_old_history_record() -> None:
    success = bash_snapshot(
        "success",
        ToolPresentationStatus.SUCCESS,
        command="printf saved",
        output="saved\n",
        exit_code=0,
    )
    failed = bash_snapshot(
        "call-123",
        ToolPresentationStatus.FAILED,
        command="false",
        output="failure\n",
        exit_code=1,
        message="tool execution failed",
    )

    async def scenario() -> None:
        app = MicaTuiApp(stored_session=make_history_session((success, failed)))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            cards = list(app.query(BashToolCard))
            assert [card.state for card in cards] == [
                ToolCardState.SUCCESS,
                ToolCardState.FAILED,
            ]
            assert cards[0].output == "saved\n"
            assert "exit 1" in cards[1].render().plain
            assert all(card.output_following for card in cards)

        old = MicaTuiApp(stored_session=make_history_session(()))
        async with old.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert list(old.query(BashToolCard)) == []
            records = list(old.query(HistoricalToolRecordView))
            assert [record.render().plain for record in records] == [
                "tool bash · recorded"
            ]

    asyncio.run(scenario())


def test_bash_output_preserves_blank_lines_crlf_and_eof_text() -> None:
    card = BashToolCard(
        bash_snapshot(
            "output-card",
            ToolPresentationStatus.SUCCESS,
            output="first\r\n\r\nlast",
            exit_code=0,
        )
    )
    assert card.output == "first\r\n\r\nlast"
    assert card.logical_output_line_count == 3
    rendered = card.render().plain
    assert "first\n\nlast" in rendered
    assert "(no output)" not in rendered


def test_bash_run_failed_closes_unfinished_card_without_exit_code() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(72, 20)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared(
                    "run", 1, bash_snapshot("open", ToolPresentationStatus.PENDING)
                )
            )
            await app.project_engine_event(ToolStarted("run", 2, "open", "bash"))
            await app.project_engine_event(
                ToolOutputDelta("run", 3, "open", "bash", "partial\n")
            )
            await app.project_engine_event(
                RunFailed("run", "safe failure", ErrorCategory.UNKNOWN, 4)
            )
            card = app.query_one(BashToolCard)
            assert card.state is ToolCardState.FAILED
            assert card.output == "partial\n"
            assert "exit " not in card.render().plain
            assert "tool execution failed" in card.render().plain

    asyncio.run(scenario())


def test_bash_internal_moves_consume_and_boundary_keys_bubble() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(72, 14)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared(
                    "run", 1, bash_snapshot("call", ToolPresentationStatus.PENDING)
                )
            )
            await app.project_engine_event(ToolStarted("run", 2, "call", "bash"))
            for index in range(10):
                await app.project_engine_event(
                    ToolOutputDelta("run", index + 3, "call", "bash", f"line {index}\n")
                )
            card = app.query_one(BashToolCard)
            transcript = app.query_one(ConversationTranscript)
            # 与产品一致：聚焦不抢 transcript 的滚动位置。
            card.focus(scroll_visible=False)
            await pilot.pause()
            card.scroll_visible(animate=False)
            await transcript.stop_animation("scroll_y")
            await pilot.pause()

            for key, expected_start in (
                ("up", 1),
                ("down", 2),
                ("pageup", 0),
                ("pagedown", 2),
            ):
                before_scroll = transcript.scroll_y
                await pilot.press(key)
                await pilot.pause()
                assert card.output_start == expected_start
                assert transcript.scroll_y == before_scroll

            for key in ("down", "pagedown", "end"):
                transcript.scroll_page_up(animate=False)
                await pilot.pause()
                before_scroll = transcript.scroll_y
                before_state = (
                    card.output_start,
                    card.output_following,
                    card.unread_lines,
                )
                await pilot.press(key)
                await pilot.pause()
                assert (
                    card.output_start,
                    card.output_following,
                    card.unread_lines,
                ) == before_state
                assert transcript.scroll_y > before_scroll, (
                    key,
                    before_scroll,
                    transcript.scroll_y,
                    transcript.max_scroll_y,
                )

    asyncio.run(scenario())


def test_bash_end_bubbles_when_output_is_short_or_already_latest() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(72, 18)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared(
                    "run", 1, bash_snapshot("short", ToolPresentationStatus.PENDING)
                )
            )
            await app.project_engine_event(ToolStarted("run", 2, "short", "bash"))
            await app.project_engine_event(
                ToolOutputDelta("run", 3, "short", "bash", "one\ntwo\n")
            )
            short = app.query_one(BashToolCard)
            short.focus()
            await pilot.press("down", "pagedown", "end")
            assert short.output_start == 0
            assert short.output_following is True
            assert short.unread_lines == 0

            await app.project_engine_event(
                ToolPrepared(
                    "run", 4, bash_snapshot("long", ToolPresentationStatus.PENDING)
                )
            )
            await app.project_engine_event(ToolStarted("run", 5, "long", "bash"))
            for index in range(10):
                await app.project_engine_event(
                    ToolOutputDelta("run", index + 6, "long", "bash", f"line {index}\n")
                )
            long = list(app.query(BashToolCard))[-1]
            long.focus()
            latest = (long.output_start, long.output_following, long.unread_lines)
            await pilot.press("end")
            assert (
                long.output_start,
                long.output_following,
                long.unread_lines,
            ) == latest

    asyncio.run(scenario())
