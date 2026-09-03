"""P6-04 历史恢复与 transcript 滚动的公开行为测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from mica_agent.events import ReasoningDelta, RunCompleted, RunStarted, TextDelta
from mica_agent.jsonl_store import StoredSession
from mica_agent.models import Message, MessageRole
from mica_agent.session_contracts import (
    CURRENT_SESSION_FORMAT_VERSION,
    SessionMeta,
    TurnEntry,
)
from mica_agent.tool_contracts import ToolCall
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)
from mica_agent.tui import (
    AssistantResponseView,
    ConversationTranscript,
    HistoricalToolRecordView,
    MicaTuiApp,
    NewOutputIndicator,
    ReasoningDisplayMode,
    ReasoningView,
)
from mica_agent.tui.transcript import UserMessageCard

NOW = datetime(2026, 8, 21, 3, 4, 5, tzinfo=UTC)


def make_session(*turns: TurnEntry) -> StoredSession:
    return StoredSession(
        SessionMeta(
            "session-history",
            CURRENT_SESSION_FORMAT_VERSION,
            "workspace-history",
            "C:/fictional/mica",
            "history",
            NOW,
            NOW,
        ),
        tuple(turns),
    )


def make_turn(number: int, user: str, assistant: str) -> TurnEntry:
    return TurnEntry(
        f"turn-{number}",
        f"historical-run-{number}",
        Message(MessageRole.USER, user),
        Message(MessageRole.ASSISTANT, assistant),
        NOW,
    )


def make_long_session(turn_count: int = 12) -> StoredSession:
    return make_session(
        *(
            make_turn(
                number,
                f"user {number} " + ("u" * 80),
                f"assistant {number}\n\n" + ("answer line\n" * 4),
            )
            for number in range(1, turn_count + 1)
        )
    )


def make_tool_turn(*, presentations: bool) -> TurnEntry:
    calls = (
        ToolCall("call-read", "read", {"path": "private.md"}),
        ToolCall("call-bash", "bash", {"command": "secret command"}),
    )
    snapshots: tuple[ToolPresentationSnapshot, ...] = ()
    if presentations:
        snapshots = (
            ToolPresentationSnapshot(
                "call-read",
                "read",
                ToolPresentationKind.READ,
                ToolPresentationStatus.SUCCESS,
                {"path": "private.md"},
            ),
            ToolPresentationSnapshot(
                "call-bash",
                "bash",
                ToolPresentationKind.BASH,
                ToolPresentationStatus.FAILED,
                {
                    "command": "secret command",
                    "output": "secret result",
                    "exit_code": 1,
                },
                "tool execution failed",
            ),
        )
    return TurnEntry(
        "tool-turn",
        "tool-history-run",
        Message(MessageRole.USER, "use tools"),
        Message(MessageRole.ASSISTANT, "finished"),
        NOW,
        (
            Message(MessageRole.ASSISTANT, "", calls),
            Message(MessageRole.TOOL, '{"secret":"one"}', tool_call_id="call-read"),
            Message(MessageRole.TOOL, '{"secret":"two"}', tool_call_id="call-bash"),
        ),
        snapshots,
    )


def test_stored_session_is_restored_in_turn_order_without_replaying_a_run() -> None:
    session = make_session(
        make_turn(1, "first user", "# first assistant"),
        make_turn(2, "second user", "second assistant"),
    )

    async def scenario() -> None:
        app = MicaTuiApp(stored_session=session)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            visible = [
                child
                for child in transcript.children
                if isinstance(child, (UserMessageCard, AssistantResponseView))
            ]
            assert [type(child) for child in visible] == [
                UserMessageCard,
                AssistantResponseView,
                UserMessageCard,
                AssistantResponseView,
            ]
            assert [str(child.render()) for child in visible[::2]] == [
                "first user",
                "second user",
            ]
            assert [child.markdown.source for child in visible[1::2]] == [
                "# first assistant",
                "second assistant",
            ]
            assert all(not child.is_streaming for child in visible[1::2])
            assert list(app.query(ReasoningView)) == []
            assert app.query_one("#work-status").render().plain == "Mica · ready"

    asyncio.run(scenario())


def test_non_stored_session_is_rejected_at_construction() -> None:
    with pytest.raises(TypeError, match="stored_session"):
        MicaTuiApp(stored_session=object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("has_presentations", "expected"),
    [
        (True, ["tool read · success", "tool bash · failed"]),
        (False, ["tool read · recorded", "tool bash · recorded"]),
    ],
)
def test_historical_tool_records_keep_call_order_without_exposing_details(
    has_presentations: bool, expected: list[str]
) -> None:
    turn = make_tool_turn(presentations=has_presentations)

    async def scenario() -> None:
        app = MicaTuiApp(stored_session=make_session(turn))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            records = list(app.query(HistoricalToolRecordView))
            visible = [
                child
                for child in transcript.children
                if isinstance(
                    child,
                    (UserMessageCard, HistoricalToolRecordView, AssistantResponseView),
                )
            ]
            assert [type(child) for child in visible] == [
                UserMessageCard,
                HistoricalToolRecordView,
                HistoricalToolRecordView,
                AssistantResponseView,
            ]
            assert [str(record.render()) for record in records] == expected
            safe = "\n".join(str(record.render()) + repr(record) for record in records)
            for hidden in (
                "call-read",
                "call-bash",
                "private.md",
                "secret command",
                "secret result",
                '{"secret"',
            ):
                assert hidden not in safe
            if has_presentations:
                assert [record.presentation for record in records] == list(
                    turn.tool_presentations
                )
            else:
                assert all(record.presentation is None for record in records)

    asyncio.run(scenario())


def test_empty_session_keeps_startup_only_and_exposes_following_transcript() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(stored_session=None)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            assert transcript.is_following is True
            assert transcript.has_pending_new_output is False
            assert list(app.query(UserMessageCard)) == []
            assert list(app.query(AssistantResponseView)) == []
            assert list(app.query(NewOutputIndicator)) == []

    asyncio.run(scenario())


def test_long_history_starts_at_bottom_and_new_user_submission_follows() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(stored_session=make_long_session())
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            assert transcript.max_scroll_y > 0
            assert transcript.scroll_y == transcript.max_scroll_y
            await pilot.press("n", "e", "w", "enter")
            await pilot.pause()
            assert str(app.query(UserMessageCard).last().render()) == "new"
            assert transcript.scroll_y == transcript.max_scroll_y
            assert transcript.is_following is True
            assert transcript.has_pending_new_output is False

    asyncio.run(scenario())


def test_real_page_up_detaches_then_new_visible_output_shows_one_indicator() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(stored_session=make_long_session())
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            transcript.focus()
            await pilot.press("pageup")
            await pilot.pause()
            detached_y = transcript.scroll_y
            assert detached_y < transcript.max_scroll_y
            assert transcript.is_following is False
            assert transcript.has_pending_new_output is False
            assert list(app.query(NewOutputIndicator)) == []

            app.query_one("#composer").focus()
            await pilot.press("x", "enter")
            await pilot.pause()
            assert transcript.scroll_y == detached_y
            assert transcript.has_pending_new_output is True
            indicators = list(app.query(NewOutputIndicator))
            assert len(indicators) == 1
            assert indicators[0].display is True
            assert str(indicators[0].render()) == "↓ new output"

            await pilot.press("y", "enter")
            await pilot.pause()
            assert transcript.scroll_y == detached_y
            assert len(app.query(NewOutputIndicator)) == 1
            transcript.return_to_latest()
            await pilot.pause()
            assert transcript.scroll_y == transcript.max_scroll_y
            assert transcript.is_following is True
            assert transcript.has_pending_new_output is False
            assert list(app.query(NewOutputIndicator)) == []

    asyncio.run(scenario())


def test_bottom_following_tracks_reasoning_growth_and_streaming_text_growth() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(stored_session=make_long_session())
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            assert transcript.is_following is True
            assert await app.project_engine_event(RunStarted("live-follow")) is True
            assert (
                await app.project_engine_event(
                    ReasoningDelta("live-follow", "reasoning line\n" * 10, 1)
                )
                is True
            )
            await pilot.pause()
            assert transcript.scroll_y == transcript.max_scroll_y
            assert transcript.has_pending_new_output is False
            assert (
                await app.project_engine_event(
                    TextDelta("live-follow", "answer line\n" * 10, 2)
                )
                is True
            )
            await pilot.pause()
            assert transcript.scroll_y == transcript.max_scroll_y
            assert transcript.has_pending_new_output is False

    asyncio.run(scenario())


def test_rejected_events_do_not_change_scroll_or_new_output_state() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(stored_session=make_long_session())
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            transcript.focus()
            await pilot.press("pageup")
            await pilot.pause()
            detached_y = transcript.scroll_y
            assert await app.project_engine_event(RunStarted("guarded-run")) is True
            assert (
                await app.project_engine_event(TextDelta("guarded-run", "kept", 1))
                is True
            )
            await pilot.pause()
            pending_before = transcript.has_pending_new_output
            assert pending_before is True
            assert transcript.scroll_y == detached_y
            assert (
                await app.project_engine_event(TextDelta("guarded-run", "duplicate", 1))
                is False
            )
            assert (
                await app.project_engine_event(TextDelta("guarded-run", "reverse", 0))
                is False
            )
            assert (
                await app.project_engine_event(TextDelta("unknown-run", "unknown", 1))
                is False
            )
            assert (
                await app.project_engine_event(RunCompleted("guarded-run", 2)) is True
            )
            assert (
                await app.project_engine_event(TextDelta("guarded-run", "late", 3))
                is False
            )
            await pilot.pause()
            assert transcript.scroll_y == detached_y
            assert transcript.has_pending_new_output is True
            assert len(app.query(NewOutputIndicator)) == 1

    asyncio.run(scenario())


def test_hidden_reasoning_does_not_create_new_output_while_detached() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(
            stored_session=make_long_session(),
            reasoning_display_mode=ReasoningDisplayMode.HIDDEN,
        )
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            transcript.focus()
            await pilot.press("pageup")
            await pilot.pause()
            assert transcript.is_following is False
            assert await app.project_engine_event(RunStarted("hidden-run")) is True
            assert (
                await app.project_engine_event(
                    ReasoningDelta("hidden-run", "hidden body", 1)
                )
                is True
            )
            await pilot.pause()
            assert list(app.query(ReasoningView)) == []
            assert transcript.has_pending_new_output is False
            assert list(app.query(NewOutputIndicator)) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (40, 16), (24, 8)])
def test_indicator_never_covers_status_or_composer_and_survives_resize(
    size: tuple[int, int],
) -> None:
    async def scenario() -> None:
        app = MicaTuiApp(stored_session=make_long_session())
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            transcript.focus()
            await pilot.press("pageup")
            app.query_one("#composer").focus()
            await pilot.press("z", "enter")
            await pilot.pause()
            indicator = app.query_one(NewOutputIndicator)
            assert indicator.parent is transcript

            status = app.query_one("#work-status")
            composer = app.query_one("#composer")
            assert transcript.region.bottom <= status.region.y
            assert status.region.bottom <= composer.region.y
            assert indicator.region.bottom <= transcript.region.bottom

            status_height = status.region.height
            await pilot.resize_terminal(size[0] + 12, size[1] + 6)
            await pilot.pause()
            resized_transcript = app.query_one(ConversationTranscript)
            resized = list(app.query(NewOutputIndicator))
            assert len(resized) == (
                1 if resized_transcript.has_pending_new_output else 0
            )
            for survivor in resized:
                assert survivor.parent is resized_transcript
                assert survivor.region.bottom <= resized_transcript.region.bottom
            assert app.query_one("#work-status").region.height == status_height
            assert (
                resized_transcript.region.bottom
                <= app.query_one("#work-status").region.y
            )

    asyncio.run(scenario())


def test_transcript_and_indicator_repr_stay_free_of_history_content() -> None:
    session = make_session(
        make_turn(1, "secret user body", "secret assistant body"),
        *(
            make_turn(number, f"filler {number}", "filler\n" * 4)
            for number in range(2, 12)
        ),
    )

    async def scenario() -> None:
        app = MicaTuiApp(stored_session=session)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            transcript.focus()
            await pilot.press("pageup")
            app.query_one("#composer").focus()
            await pilot.press("s", "enter")
            await pilot.pause()
            indicator = app.query_one(NewOutputIndicator)
            assert repr(transcript) == "<ConversationTranscript>"
            assert repr(indicator) == "<NewOutputIndicator>"
            blob = repr(transcript) + repr(indicator)
            for hidden in (
                "secret user body",
                "secret assistant body",
                "session-history",
                "C:/fictional/mica",
            ):
                assert hidden not in blob

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("no_color", "ansi_color"),
    [(True, None), (False, True)],
)
def test_history_and_indicator_survive_terminal_color_degradation(
    monkeypatch: pytest.MonkeyPatch,
    no_color: bool,
    ansi_color: bool | None,
) -> None:
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    async def scenario() -> None:
        app = MicaTuiApp(stored_session=make_long_session(), ansi_color=ansi_color)
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            if no_color:
                assert app.no_color is True
            else:
                assert app.ansi_color is True
            transcript = app.query_one(ConversationTranscript)
            assert transcript.scroll_y == transcript.max_scroll_y
            assert len(app.query(UserMessageCard)) == 12
            transcript.focus()
            await pilot.press("pageup")
            app.query_one("#composer").focus()
            await pilot.press("q", "enter")
            await pilot.pause()
            indicator = app.query_one(NewOutputIndicator)
            assert indicator.display is True
            assert str(indicator.render()) == "\u2193 new output"

    asyncio.run(scenario())


def test_history_and_live_events_stay_in_order_and_live_reply_is_separate() -> None:
    session = make_session(make_turn(1, "old user", "old answer"))

    async def scenario() -> None:
        app = MicaTuiApp(stored_session=session)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-live")) is True
            assert (
                await app.project_engine_event(ReasoningDelta("run-live", "think", 1))
                is True
            )
            assert (
                await app.project_engine_event(TextDelta("run-live", "new answer", 2))
                is True
            )
            assert await app.project_engine_event(RunCompleted("run-live", 3)) is True
            await pilot.pause()
            responses = list(app.query(AssistantResponseView))
            assert [response.markdown.source for response in responses] == [
                "old answer",
                "new answer",
            ]
            assert len(app.query(ReasoningView)) == 1
            content = [
                child
                for child in app.query_one(ConversationTranscript).children
                if isinstance(
                    child, (UserMessageCard, ReasoningView, AssistantResponseView)
                )
            ]
            assert [type(child) for child in content] == [
                UserMessageCard,
                AssistantResponseView,
                ReasoningView,
                AssistantResponseView,
            ]

    asyncio.run(scenario())
