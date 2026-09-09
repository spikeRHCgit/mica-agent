"""P6-10C same-run model-step timeline projection tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from mica_agent.events import (
    ErrorCategory,
    ReasoningDelta,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
    ToolFinished,
    ToolPrepared,
    ToolStarted,
    UserInputRequested,
)
from mica_agent.interactions import AskOption, AskPrompt
from mica_agent.jsonl_store import StoredSession
from mica_agent.models import Message, MessageRole
from mica_agent.session_contracts import (
    CURRENT_SESSION_FORMAT_VERSION,
    SessionMeta,
    TurnEntry,
)
from mica_agent.tool_contracts import ToolResultStatus
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)
from mica_agent.tui import (
    AskInteractionView,
    AssistantResponseView,
    ConversationTranscript,
    MicaTuiApp,
    NewOutputIndicator,
    ReadToolCard,
    ReasoningView,
    WriteToolCard,
)
from mica_agent.tui.transcript import UserMessageCard

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def read_snapshot(
    call_id: str,
    status: ToolPresentationStatus = ToolPresentationStatus.PENDING,
) -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        call_id,
        "read",
        ToolPresentationKind.READ,
        status,
        {"path": "docs/tui_design.md"},
    )


def write_snapshot(
    call_id: str,
    status: ToolPresentationStatus = ToolPresentationStatus.PENDING,
) -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        call_id,
        "write",
        ToolPresentationKind.WRITE,
        status,
        {"path": "notes.md", "content": "safe body"},
    )


def ask_snapshot(
    call_id: str,
    status: ToolPresentationStatus = ToolPresentationStatus.PENDING,
    answer: str = "",
) -> ToolPresentationSnapshot:
    payload = {
        "question": "Pick",
        **({"answer": answer} if status is not ToolPresentationStatus.PENDING else {}),
    }
    return ToolPresentationSnapshot(
        call_id, "ask", ToolPresentationKind.ASK, status, payload
    )


def invalid_prepared_snapshot(call_id: str) -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        call_id,
        "read",
        ToolPresentationKind.GENERIC,
        ToolPresentationStatus.PENDING,
        {},
    )


def make_session(*turns: TurnEntry) -> StoredSession:
    return StoredSession(
        SessionMeta(
            "session-timeline",
            CURRENT_SESSION_FORMAT_VERSION,
            "workspace-timeline",
            "C:/fictional/mica",
            "timeline",
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


def live_timeline_children(app: MicaTuiApp) -> list[object]:
    return [
        child
        for child in app.query_one("#transcript").children
        if isinstance(
            child,
            (
                UserMessageCard,
                ReasoningView,
                AssistantResponseView,
                ReadToolCard,
                WriteToolCard,
                AskInteractionView,
            ),
        )
    ]


def test_two_model_steps_and_two_tools_keep_engine_dom_order() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", "think-1", 1))
                is True
            )
            assert (
                await app.project_engine_event(TextDelta("run-1", "step-1", 2)) is True
            )
            first_reasoning = app.query(ReasoningView).first()
            first_response = app.query(AssistantResponseView).first()
            assert first_response.is_streaming is True

            assert await app.project_engine_event(
                ToolPrepared("run-1", 3, read_snapshot("call-read"))
            )
            await pilot.pause()
            assert first_response.is_streaming is False
            assert len(first_response.query(".streaming-cursor")) == 0
            assert len(app.query(AssistantResponseView)) == 1

            assert await app.project_engine_event(
                ToolPrepared("run-1", 4, write_snapshot("call-write"))
            )
            assert len(app.query(AssistantResponseView)) == 1
            assert await app.project_engine_event(
                ToolStarted("run-1", 5, "call-read", "read")
            )
            assert await app.project_engine_event(
                ToolFinished(
                    "run-1",
                    6,
                    "call-read",
                    "read",
                    ToolResultStatus.SUCCESS,
                    read_snapshot("call-read", ToolPresentationStatus.SUCCESS),
                )
            )
            assert await app.project_engine_event(
                ToolStarted("run-1", 7, "call-write", "write")
            )
            assert await app.project_engine_event(
                ToolFinished(
                    "run-1",
                    8,
                    "call-write",
                    "write",
                    ToolResultStatus.SUCCESS,
                    write_snapshot("call-write", ToolPresentationStatus.SUCCESS),
                )
            )
            assert len(app.query(AssistantResponseView)) == 1

            assert (
                await app.project_engine_event(ReasoningDelta("run-1", "think-2", 9))
                is True
            )
            assert (
                await app.project_engine_event(TextDelta("run-1", "step-2", 10)) is True
            )
            await pilot.pause()
            responses = list(app.query(AssistantResponseView))
            reasonings = list(app.query(ReasoningView))
            assert responses[0] is first_response
            assert reasonings[0] is first_reasoning
            assert responses[0].text == "step-1"
            assert responses[1].text == "step-2"
            assert reasonings[0].text == "think-1"
            assert reasonings[1].text == "think-2"
            assert responses[1].is_streaming is True
            assert await app.project_engine_event(RunCompleted("run-1", 11)) is True
            await pilot.pause()
            assert responses[1].is_streaming is False
            assert [type(child) for child in live_timeline_children(app)] == [
                ReasoningView,
                AssistantResponseView,
                ReadToolCard,
                WriteToolCard,
                ReasoningView,
                AssistantResponseView,
            ]
            assert repr(responses[0]) == "<AssistantResponseView>"
            assert "step-1" not in repr(responses[0])

    asyncio.run(scenario())


def test_rejected_tool_prepared_does_not_close_the_current_step() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert await app.project_engine_event(TextDelta("run-1", "kept", 1)) is True
            view = app.query(AssistantResponseView).first()
            assert (
                await app.project_engine_event(
                    ToolPrepared("run-1", 2, invalid_prepared_snapshot("bad"))
                )
                is False
            )
            assert view.is_streaming is True
            assert len(app.query(ReadToolCard)) == 0
            assert (
                await app.project_engine_event(TextDelta("run-1", " more", 3)) is True
            )
            assert app.query(AssistantResponseView).first() is view
            assert view.text == "kept more"
            assert (
                await app.project_engine_event(
                    ToolPrepared("run-1", 3, read_snapshot("late-dup"))
                )
                is False
            )
            assert (
                await app.project_engine_event(
                    ToolPrepared("unknown", 1, read_snapshot("other"))
                )
                is False
            )
            assert await app.project_engine_event(
                ToolPrepared("run-1", 4, read_snapshot("call-read"))
            )
            await pilot.pause()
            assert view.is_streaming is False
            assert view.text == "kept more"
            assert (
                await app.project_engine_event(
                    ToolPrepared("run-1", 5, read_snapshot("call-read"))
                )
                is False
            )
            assert len(app.query(AssistantResponseView)) == 1
            assert len(app.query(ReadToolCard)) == 1
            assert (
                await app.project_engine_event(TextDelta("run-1", "after", 6)) is True
            )
            assert list(app.query(AssistantResponseView))[1].text == "after"

    asyncio.run(scenario())


def test_ask_prepared_splits_the_model_step_without_a_tool_card() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-ask")) is True
            assert (
                await app.project_engine_event(TextDelta("run-ask", "ask first", 1))
                is True
            )
            first = app.query(AssistantResponseView).first()
            assert await app.project_engine_event(
                ToolPrepared("run-ask", 2, ask_snapshot("call-ask"))
            )
            await pilot.pause()
            assert first.is_streaming is False
            assert list(app.query(ReadToolCard)) == []
            assert list(app.query(AskInteractionView)) == []
            assert (
                await app.project_engine_event(
                    ToolPrepared("run-ask", 3, ask_snapshot("call-ask-2"))
                )
                is False
            )
            assert not await app.project_engine_event(
                UserInputRequested(
                    "run-ask",
                    4,
                    "interaction",
                    "Pick",
                    AskPrompt("Pick", options=(AskOption("yes"), AskOption("no"))),
                )
            )
            await pilot.pause()
            ask = app.query_one(AskInteractionView)
            assert ask.parent is not app.query_one("#transcript")
            assert await app.project_engine_event(
                ToolFinished(
                    "run-ask",
                    5,
                    "call-ask",
                    "ask",
                    ToolResultStatus.SUCCESS,
                    ask_snapshot("call-ask", ToolPresentationStatus.SUCCESS, "yes"),
                )
            )
            assert (
                await app.project_engine_event(TextDelta("run-ask", "thanks", 6))
                is True
            )
            await pilot.pause()
            responses = list(app.query(AssistantResponseView))
            record = app.query_one(AskInteractionView)
            assert responses[0] is first
            assert responses[1].text == "thanks"
            assert record is not ask
            assert record.parent is app.query_one("#transcript")
            assert [type(child) for child in live_timeline_children(app)] == [
                AssistantResponseView,
                AskInteractionView,
                AssistantResponseView,
            ]

    asyncio.run(scenario())


def test_run_failure_and_cancel_only_finish_the_active_step() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-fail")) is True
            assert (
                await app.project_engine_event(TextDelta("run-fail", "before", 1))
                is True
            )
            first = app.query(AssistantResponseView).first()
            assert await app.project_engine_event(
                ToolPrepared("run-fail", 2, read_snapshot("call-read"))
            )
            assert (
                await app.project_engine_event(TextDelta("run-fail", "after", 3))
                is True
            )
            second = list(app.query(AssistantResponseView))[1]
            assert (
                await app.project_engine_event(
                    RunFailed("run-fail", "safe failure", ErrorCategory.UNKNOWN, 4)
                )
                is True
            )
            await pilot.pause()
            assert first.text == "before"
            assert first.is_streaming is False
            assert second.text == "after"
            assert second.is_streaming is False
            assert (
                await app.project_engine_event(TextDelta("run-fail", "late", 5))
                is False
            )
            assert first.text == "before"
            assert second.text == "after"

            assert await app.project_engine_event(RunStarted("run-cancel")) is True
            assert (
                await app.project_engine_event(TextDelta("run-cancel", "partial", 1))
                is True
            )
            cancelled = app.query(AssistantResponseView).last()
            assert await app.project_engine_event(RunCancelled("run-cancel", 2)) is True
            assert cancelled.is_streaming is False
            assert (
                await app.project_engine_event(TextDelta("run-cancel", "late", 3))
                is False
            )
            assert cancelled.text == "partial"
            assert len(app.query(AssistantResponseView)) == 3

    asyncio.run(scenario())


def test_history_and_other_runs_stay_isolated_from_live_steps() -> None:
    session = make_session(make_turn(1, "old user", "old answer"))

    async def scenario() -> None:
        app = MicaTuiApp(stored_session=session)
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            historical = app.query(AssistantResponseView).first()
            assert await app.project_engine_event(RunStarted("run-a")) is True
            assert await app.project_engine_event(TextDelta("run-a", "a1", 1)) is True
            first_live = list(app.query(AssistantResponseView))[1]
            assert await app.project_engine_event(
                ToolPrepared("run-a", 2, read_snapshot("same"))
            )
            assert await app.project_engine_event(TextDelta("run-a", "a2", 3)) is True
            assert await app.project_engine_event(RunCompleted("run-a", 4)) is True
            await pilot.press("n", "e", "w", "enter")
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-b")) is True
            assert await app.project_engine_event(TextDelta("run-b", "b1", 1)) is True
            await pilot.pause()
            responses = list(app.query(AssistantResponseView))
            assert responses[0] is historical
            assert responses[1] is first_live
            assert [response.text for response in responses] == [
                "old answer",
                "a1",
                "a2",
                "b1",
            ]
            assert [type(child) for child in live_timeline_children(app)] == [
                UserMessageCard,
                AssistantResponseView,
                AssistantResponseView,
                ReadToolCard,
                AssistantResponseView,
                UserMessageCard,
                AssistantResponseView,
            ]

    asyncio.run(scenario())


def test_detached_timeline_growth_shows_one_new_output_indicator() -> None:
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
            assert await app.project_engine_event(RunStarted("live-timeline")) is True
            assert (
                await app.project_engine_event(
                    TextDelta("live-timeline", "first\n" * 8, 1)
                )
                is True
            )
            await pilot.pause()
            assert transcript.scroll_y == detached_y
            assert transcript.has_pending_new_output is True
            assert await app.project_engine_event(
                ToolPrepared("live-timeline", 2, read_snapshot("call-read"))
            )
            assert (
                await app.project_engine_event(
                    TextDelta("live-timeline", "second\n" * 8, 3)
                )
                is True
            )
            await pilot.pause()
            indicators = list(app.query(NewOutputIndicator))
            assert len(indicators) == 1
            assert str(indicators[0].render()) == "↓ new output"
            assert transcript.scroll_y == detached_y
            assert (
                await app.project_engine_event(
                    ToolPrepared("live-timeline", 3, write_snapshot("ignored"))
                )
                is False
            )
            await pilot.pause()
            assert transcript.scroll_y == detached_y
            assert len(app.query(NewOutputIndicator)) == 1

    asyncio.run(scenario())
