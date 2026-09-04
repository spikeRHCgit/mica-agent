"""P6-05E generic tool-card and resolver public behavior tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from mica_agent.events import (
    ErrorCategory,
    RunCancelled,
    RunFailed,
    RunStarted,
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
    GenericToolCard,
    HistoricalToolRecordView,
    MicaTuiApp,
    ReadToolCard,
    ToolCard,
    ToolCardState,
    create_tool_card,
)

NOW = datetime(2026, 9, 4, tzinfo=UTC)


def generic_snapshot(
    call_id: str = "generic-call",
    tool_name: str = "custom_tool",
    status: ToolPresentationStatus = ToolPresentationStatus.PENDING,
) -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        call_id, tool_name, ToolPresentationKind.GENERIC, status, {}
    )


def make_generic_session(*snapshots: ToolPresentationSnapshot) -> StoredSession:
    if snapshots:
        calls = tuple(ToolCall(item.call_id, item.tool_name, {}) for item in snapshots)
    else:
        calls = (ToolCall("recorded-call", "custom_tool", {}),)
    turn = TurnEntry(
        "generic-turn",
        "generic-run",
        Message(MessageRole.USER, "use tools"),
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
            "generic-session",
            CURRENT_SESSION_FORMAT_VERSION,
            "generic-workspace",
            "C:/fictional/mica",
            "generic",
            NOW,
            NOW,
        ),
        (turn,),
    )


def test_generic_card_has_safe_two_line_states_and_fixed_repr() -> None:
    expected = {
        ToolCardState.PENDING: "custom_tool\nwaiting",
        ToolCardState.RUNNING: "custom_tool\nrunning…",
        ToolCardState.SUCCESS: "custom_tool\ncompleted",
        ToolCardState.FAILED: "custom_tool\ntool execution failed",
    }
    card = GenericToolCard(generic_snapshot())
    assert card.render().plain == expected[ToolCardState.PENDING]
    assert repr(card) == "<GenericToolCard>"
    card.start()
    assert card.render().plain == expected[ToolCardState.RUNNING]
    card.finish(
        ToolResultStatus.SUCCESS,
        generic_snapshot(status=ToolPresentationStatus.SUCCESS),
    )
    assert card.render().plain == expected[ToolCardState.SUCCESS]

    failed = GenericToolCard(generic_snapshot(status=ToolPresentationStatus.FAILED))
    assert failed.render().plain == expected[ToolCardState.FAILED]
    before_snapshot = failed.snapshot
    before_render = failed.render().plain
    failed.finish(ToolResultStatus.CANCELLED)
    assert failed.state is ToolCardState.FAILED
    assert failed.snapshot is before_snapshot
    assert failed.render().plain == before_render

    cancelled = GenericToolCard(generic_snapshot())
    cancelled.finish(ToolResultStatus.CANCELLED)
    assert cancelled.render().plain == "custom_tool\noperation cancelled"
    assert "tool details are unavailable" not in cancelled.render().plain


def test_generic_realtime_lifecycle_is_in_place_and_run_isolated() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(60, 18)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run-a"))
            assert await app.project_engine_event(
                ToolPrepared("run-a", 1, generic_snapshot("same"))
            )
            await app.project_engine_event(RunStarted("run-b"))
            assert await app.project_engine_event(
                ToolPrepared("run-b", 1, generic_snapshot("same"))
            )
            first, second = list(app.query(GenericToolCard))
            assert await app.project_engine_event(
                ToolStarted("run-a", 2, "same", "custom_tool")
            )
            assert first.state is ToolCardState.RUNNING
            assert second.state is ToolCardState.PENDING
            assert not await app.project_engine_event(
                ToolOutputDelta("run-a", 3, "same", "custom_tool", "hidden")
            )
            await app.project_engine_event(
                RunFailed("run-a", "safe failure", ErrorCategory.UNKNOWN, 4)
            )
            assert first.state is ToolCardState.FAILED
            assert second.state is ToolCardState.PENDING
            await app.project_engine_event(RunCancelled("run-b", 2))
            assert second.state is ToolCardState.CANCELLED

    asyncio.run(scenario())


def test_generic_history_and_old_history_use_distinct_paths() -> None:
    session = make_generic_session(
        generic_snapshot("success", status=ToolPresentationStatus.SUCCESS),
        generic_snapshot("failed", status=ToolPresentationStatus.FAILED),
    )

    async def scenario() -> None:
        app = MicaTuiApp(stored_session=session)
        async with app.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            cards = list(app.query(GenericToolCard))
            assert [card.render().plain for card in cards] == [
                "custom_tool\ncompleted",
                "custom_tool\ntool execution failed",
            ]
            assert app.query_one("#work-status").render().plain == "Mica · ready"

    asyncio.run(scenario())

    old = make_generic_session()

    async def old_scenario() -> None:
        app = MicaTuiApp(stored_session=old)
        async with app.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert list(app.query(GenericToolCard)) == []
            assert app.query_one(HistoricalToolRecordView).render().plain == (
                "tool custom_tool · recorded"
            )

    asyncio.run(old_scenario())


class CustomToolCard(GenericToolCard):
    """Test-only card proving the injected factory receives safe snapshots."""


def test_resolver_prioritizes_builtins_and_falls_back_safely() -> None:
    received: list[ToolPresentationSnapshot] = []

    def custom(snapshot: ToolPresentationSnapshot) -> ToolCard:
        received.append(snapshot)
        return CustomToolCard(snapshot)

    factories = {"custom_tool": custom, "read": custom}
    card = create_tool_card(generic_snapshot(), tool_card_factories=factories)
    assert isinstance(card, CustomToolCard)
    assert received == [card.snapshot]
    read = ToolPresentationSnapshot(
        "read-call",
        "read",
        ToolPresentationKind.READ,
        ToolPresentationStatus.SUCCESS,
        {"path": "safe.md"},
    )
    assert isinstance(
        create_tool_card(read, tool_card_factories=factories), ReadToolCard
    )

    factories["custom_tool"] = lambda _snapshot: object()  # type: ignore[assignment]
    fallback = create_tool_card(generic_snapshot(), tool_card_factories=factories)
    assert isinstance(fallback, GenericToolCard)

    with pytest.raises(TypeError, match="Mapping"):
        MicaTuiApp(tool_card_factories=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="valid tool names"):
        MicaTuiApp(tool_card_factories={"bad name": custom})
    with pytest.raises(TypeError, match="callable"):
        MicaTuiApp(tool_card_factories={"custom_tool": object()})  # type: ignore[arg-type]


def test_app_copies_factory_mapping_and_factory_exception_does_not_leak() -> None:
    sentinel = "secret factory failure"

    def failing(_snapshot: ToolPresentationSnapshot) -> ToolCard:
        raise RuntimeError(sentinel)

    factories = {"custom_tool": failing}
    app = MicaTuiApp(tool_card_factories=factories)
    factories.clear()

    async def scenario() -> None:
        async with app.run_test(size=(60, 14)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            assert await app.project_engine_event(
                ToolPrepared("run", 1, generic_snapshot())
            )
            card = app.query_one(GenericToolCard)
            rendered = card.render().plain + repr(card)
            assert sentinel not in rendered
            assert "Traceback" not in rendered

    asyncio.run(scenario())


def test_factory_snapshot_mismatch_falls_back_to_original_pending_snapshot() -> None:
    pending = generic_snapshot()

    def mismatched(snapshot: ToolPresentationSnapshot) -> ToolCard:
        return GenericToolCard(
            ToolPresentationSnapshot(
                snapshot.call_id,
                snapshot.tool_name,
                snapshot.kind,
                ToolPresentationStatus.FAILED,
                snapshot.payload,
                snapshot.message,
            )
        )

    card = create_tool_card(pending, tool_card_factories={"custom_tool": mismatched})
    assert isinstance(card, GenericToolCard)
    assert card.snapshot == pending
    assert card.render().plain == "custom_tool\nwaiting"


def test_reserved_names_reject_mismatched_generic_presentations_without_factory() -> (
    None
):
    calls: list[str] = []

    def factory(snapshot: ToolPresentationSnapshot) -> ToolCard:
        calls.append(snapshot.tool_name)
        return GenericToolCard(snapshot)

    for tool_name in ("read", "ask"):
        snapshot = ToolPresentationSnapshot(
            f"{tool_name}-call",
            tool_name,
            ToolPresentationKind.GENERIC,
            ToolPresentationStatus.PENDING,
            {},
        )
        assert (
            create_tool_card(snapshot, tool_card_factories={tool_name: factory}) is None
        )

    assert calls == []


def test_ask_never_becomes_generic_card() -> None:
    snapshot = ToolPresentationSnapshot(
        "ask-call",
        "ask",
        ToolPresentationKind.ASK,
        ToolPresentationStatus.PENDING,
        {"question": "safe question"},
    )
    assert create_tool_card(snapshot) is None
