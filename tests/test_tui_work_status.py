"""P6-03C 唯一工作状态条的公开行为测试。"""

from __future__ import annotations

import asyncio

import pytest

from mica_agent.events import (
    EngineEvent,
    ErrorCategory,
    PermissionRequested,
    ReasoningDelta,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
    ToolFinished,
    ToolOutputDelta,
    ToolPrepared,
    ToolStarted,
    UserInputRequested,
)
from mica_agent.tool_contracts import ToolResultStatus
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)
from mica_agent.tui import MicaTuiApp, WorkPhase, WorkStatus, WorkStatusView
from mica_agent.tui.theme import BRAND, BRAND_DEEP, BRAND_LIGHT, ERROR, SUCCESS


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_initial_ready_and_typing_keeps_the_single_fixed_status_view() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            views = list(app.query(WorkStatusView))
            assert len(views) == 1
            view = views[0]
            assert view is app.query_one("#work-status", WorkStatusView)
            assert view.status is WorkStatus.READY
            assert view.phase is WorkPhase.NONE
            assert view.render().plain == "Mica · ready"
            assert view.parent is app.screen
            assert view not in app.query_one("#transcript").query("*")

            await pilot.press("h", "e", "l", "l", "o")
            await pilot.pause()
            assert app.query_one("#work-status", WorkStatusView) is view
            assert view.status is WorkStatus.READY
            assert view.render().plain == "Mica · ready"

    asyncio.run(scenario())


def test_run_started_switches_to_working_waiting_at_zero_elapsed() -> None:
    async def scenario() -> None:
        clock = FakeClock(100.0)
        app = MicaTuiApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)

            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert view.status is WorkStatus.WORKING
            assert view.phase is WorkPhase.WAITING
            assert view.render().plain == "Mica · working. (waiting 0.0s)"

    asyncio.run(scenario())


def test_working_dot_animation_advances_deterministically() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(clock=FakeClock())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("run-1")) is True

            assert view.render().plain.startswith("Mica · working.")
            view.advance_animation()
            assert view.render().plain.startswith("Mica · working..")
            view.advance_animation()
            assert view.render().plain.startswith("Mica · working...")
            view.advance_animation()
            assert view.render().plain.startswith("Mica · working.")

    asyncio.run(scenario())


def test_working_color_deepens_across_three_and_ten_second_boundaries() -> None:
    async def scenario() -> None:
        clock = FakeClock(100.0)
        app = MicaTuiApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("run-1")) is True

            colors: dict[float, str] = {}
            for elapsed in (0.0, 2.9, 3.0, 3.5, 9.9, 10.0, 10.5, 30.0, 60.0):
                clock.value = 100.0 + elapsed
                colors[elapsed] = str(view.render().style)

            assert colors[0.0] == BRAND_LIGHT
            assert colors[2.9] != colors[0.0]
            assert colors[3.0] == BRAND
            assert colors[3.5] != colors[3.0]
            assert colors[9.9] != colors[3.5]
            assert colors[10.0] == "#2c68e7"
            assert colors[10.5] != colors[10.0]
            assert colors[30.0] == BRAND_DEEP

    asyncio.run(scenario())


def test_reasoning_progress_switches_to_thinking_without_resetting_same_phase() -> None:
    async def scenario() -> None:
        clock = FakeClock(10.0)
        app = MicaTuiApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("run-1")) is True

            clock.advance(2.0)
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", "first", 1))
                is True
            )
            assert view.phase is WorkPhase.THINKING
            assert view.render().plain.endswith("(thinking 0.0s)")
            assert str(view.render().style) == BRAND_LIGHT

            clock.advance(2.0)
            assert view.render().plain.endswith("(thinking 2.0s)")
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", "second", 2))
                is True
            )
            assert view.render().plain.endswith("(thinking 2.0s)")
            assert str(view.render().style) == BRAND_LIGHT

    asyncio.run(scenario())


def test_text_progress_switches_to_responding_and_keeps_working_animation() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        app = MicaTuiApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("run-1")) is True
            clock.advance(1.0)
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", "thought", 1))
                is True
            )

            clock.advance(2.0)
            assert (
                await app.project_engine_event(TextDelta("run-1", "answer", 2)) is True
            )
            assert view.status is WorkStatus.WORKING
            assert view.phase is WorkPhase.RESPONDING
            assert view.render().plain.endswith("(responding 0.0s)")

            clock.advance(2.0)
            assert await app.project_engine_event(TextDelta("run-1", "!", 3)) is True
            assert view.render().plain.endswith("(responding 2.0s)")
            before = view.render().plain
            view.advance_animation()
            assert view.status is WorkStatus.WORKING
            assert view.render().plain != before

    asyncio.run(scenario())


_PENDING_READ = ToolPresentationSnapshot(
    "call-1",
    "read",
    ToolPresentationKind.READ,
    ToolPresentationStatus.PENDING,
    {"path": "notes.md"},
)


@pytest.mark.parametrize(
    "event",
    [
        ToolPrepared("run-1", 2, _PENDING_READ),
        ToolStarted("run-1", 2, "call-1", "read"),
        ToolOutputDelta("run-1", 2, "call-1", "bash", "safe output"),
        ToolFinished("run-1", 2, "call-1", "read", ToolResultStatus.SUCCESS),
        PermissionRequested(
            "run-1", 2, "interaction-1", "call-1", "read", "Allow read?"
        ),
        UserInputRequested("run-1", 2, "interaction-1", "Choose one"),
    ],
)
def test_tool_and_interaction_progress_switches_to_waiting_without_placeholder(
    event: EngineEvent,
) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        app = MicaTuiApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert (
                await app.project_engine_event(TextDelta("run-1", "answer", 1)) is True
            )
            clock.advance(2.0)

            projected = await app.project_engine_event(event)
            assert projected is isinstance(event, ToolPrepared)
            assert view.status is WorkStatus.WORKING
            assert view.phase is WorkPhase.WAITING
            assert view.render().plain.endswith("(waiting 0.0s)")
            assert str(view.render().style) == BRAND_LIGHT

    asyncio.run(scenario())


def test_completed_run_without_reasoning_shows_waited_before_first_text() -> None:
    async def scenario() -> None:
        clock = FakeClock(50.0)
        app = MicaTuiApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("run-1")) is True
            clock.advance(2.8)
            assert (
                await app.project_engine_event(TextDelta("run-1", "answer", 1)) is True
            )
            clock.advance(3.5)
            assert await app.project_engine_event(RunCompleted("run-1", 2)) is True
            assert view.render().plain == "Mica · done (waited 2.8s · total 6.3s)"
            assert "thought" not in view.render().plain

    asyncio.run(scenario())


def test_completed_run_with_reasoning_shows_thought_and_total_and_stops_animation() -> (
    None
):
    async def scenario() -> None:
        clock = FakeClock(20.0)
        app = MicaTuiApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("run-1")) is True
            clock.advance(1.0)
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", "thought", 1))
                is True
            )
            clock.advance(2.8)
            assert (
                await app.project_engine_event(TextDelta("run-1", "answer", 2)) is True
            )
            clock.advance(7.5)

            assert await app.project_engine_event(RunCompleted("run-1", 3)) is True
            assert view.status is WorkStatus.DONE
            assert view.phase is WorkPhase.NONE
            assert view.render().plain == "Mica · done (thought 2.8s · total 11.3s)"
            assert str(view.render().style) == SUCCESS
            frozen = view.render().plain
            view.advance_animation()
            clock.advance(10.0)
            assert view.render().plain == frozen

    asyncio.run(scenario())


def test_completed_without_text_uses_total_as_waited_time() -> None:
    async def scenario() -> None:
        clock = FakeClock(1.0)
        app = MicaTuiApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("run-1")) is True
            clock.advance(4.25)
            assert await app.project_engine_event(RunCompleted("run-1", 1)) is True
            assert view.render().plain == "Mica · done (waited 4.2s · total 4.2s)"

    asyncio.run(scenario())

    async def scenario() -> None:
        clock = FakeClock(20.0)
        app = MicaTuiApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("run-1")) is True
            clock.advance(1.0)
            assert await app.project_engine_event(RunCancelled("run-1", 1)) is True
            assert view.status is WorkStatus.CANCEL
            assert view.render().plain == "Mica · cancel"
            assert str(view.render().style) == ERROR
            frozen = view.render().plain
            view.advance_animation()
            assert view.render().plain == frozen

            assert await app.project_engine_event(RunStarted("run-2")) is True
            assert (
                await app.project_engine_event(ReasoningDelta("run-2", "progress", 1))
                is True
            )
            assert await app.project_engine_event(RunCancelled("run-2", 2)) is True
            assert view.status is WorkStatus.INTERRUPT
            assert view.render().plain == "Mica · interrupt"
            assert str(view.render().style) == ERROR

    asyncio.run(scenario())


def test_failed_run_is_interrupt_and_late_or_foreign_events_do_not_change_status() -> (
    None
):
    async def scenario() -> None:
        clock = FakeClock(5.0)
        app = MicaTuiApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert await app.project_engine_event(RunStarted("run-2")) is True
            await app.project_engine_event(ReasoningDelta("run-1", "foreign", 1))
            assert view.status is WorkStatus.WORKING
            assert view.phase is WorkPhase.WAITING
            assert (
                await app.project_engine_event(
                    RunFailed("run-2", "safe", ErrorCategory.UNKNOWN, 1)
                )
                is True
            )
            assert view.status is WorkStatus.INTERRUPT
            frozen = view.render().plain
            await app.project_engine_event(TextDelta("run-2", "late", 2))
            await app.project_engine_event(ReasoningDelta("run-1", "late", 2))
            assert view.render().plain == frozen

    asyncio.run(scenario())


def test_status_view_identity_and_bottom_position_survive_resize_and_new_run() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(clock=FakeClock())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert await app.project_engine_event(RunCompleted("run-1", 1)) is True
            await pilot.press("x")
            for size in ((40, 16), (24, 8), (80, 24)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                assert app.query_one(WorkStatusView) is view
                assert len(app.query(WorkStatusView)) == 1
                assert view.region.bottom <= app.query_one("#composer").region.y
                assert (
                    app.query_one("#composer").region.bottom == app.screen.size.height
                )
            assert view.status is WorkStatus.DONE
            assert await app.project_engine_event(RunStarted("run-2")) is True
            assert app.query_one(WorkStatusView) is view
            assert view.status is WorkStatus.WORKING

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "progress_event",
    [
        ReasoningDelta("run-1", "thought", 1),
        TextDelta("run-1", "text", 1),
        ToolPrepared("run-1", 1, _PENDING_READ),
        ToolStarted("run-1", 1, "call-1", "read"),
        ToolOutputDelta("run-1", 1, "call-1", "bash", "output"),
        ToolFinished("run-1", 1, "call-1", "read", ToolResultStatus.SUCCESS),
        PermissionRequested("run-1", 1, "interaction-1", "call-1", "read", "Allow?"),
        UserInputRequested("run-1", 1, "interaction-1", "Question?"),
    ],
)
def test_every_observable_progress_kind_makes_cancel_interrupt(
    progress_event: EngineEvent,
) -> None:
    async def scenario() -> None:
        app = MicaTuiApp(clock=FakeClock())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("run-1")) is True
            await app.project_engine_event(progress_event)
            assert view.had_progress is True
            assert await app.project_engine_event(RunCancelled("run-1", 2)) is True
            assert view.status is WorkStatus.INTERRUPT
            assert view.render().plain == "Mica · interrupt"

    asyncio.run(scenario())


def test_clock_regression_is_clamped_and_status_repr_is_safe() -> None:
    async def scenario() -> None:
        clock = FakeClock(10.0)
        app = MicaTuiApp(clock=clock)
        async with app.run_test(size=(24, 8)) as pilot:
            await pilot.pause()
            view = app.query_one(WorkStatusView)
            assert await app.project_engine_event(RunStarted("secret-run")) is True
            clock.advance(4.0)
            before = view.render().plain
            clock.value = 1.0
            assert view.render().plain == before
            assert "secret-run" not in repr(view)
            assert "Traceback" not in repr(view)
            assert "Mica · working" in view.render().plain
            assert app.query_one("#composer").region.bottom == app.screen.size.height

    asyncio.run(scenario())
