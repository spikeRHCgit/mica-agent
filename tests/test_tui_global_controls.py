"""P6-10D App-level Esc cancellation and two-press Ctrl+C exit."""

from __future__ import annotations

import asyncio

from mica_agent.events import (
    PermissionRequested,
    ReasoningDelta,
    RunCancelled,
    RunCompleted,
    RunStarted,
    TextDelta,
    ToolOutputDelta,
    ToolPrepared,
    ToolStarted,
    UserInputRequested,
)
from mica_agent.interactions import AskOption, AskPrompt, PermissionResponse
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)
from mica_agent.tui import (
    AskInteractionView,
    AssistantResponseView,
    BashToolCard,
    MicaComposer,
    MicaTuiApp,
    PermissionInteractionView,
    ReasoningView,
)
from mica_agent.tui.transcript import ConversationTranscript, UserMessageCard


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RecordingApp(MicaTuiApp):
    def __init__(self, *args, **kwargs) -> None:
        self.exits: list[object] = []
        super().__init__(*args, **kwargs)

    def exit(self, result=None, return_code: int = 0, message=None) -> None:
        self.exits.append(return_code)
        super().exit(result, return_code, message)


def bash_snapshot() -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        "call-bash",
        "bash",
        ToolPresentationKind.BASH,
        ToolPresentationStatus.PENDING,
        {"command": "printf safe"},
    )


def ask_snapshot() -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        "call-ask",
        "ask",
        ToolPresentationKind.ASK,
        ToolPresentationStatus.PENDING,
        {"question": "Pick"},
    )


async def start_streaming_run(app: MicaTuiApp) -> None:
    assert await app.project_engine_event(RunStarted("run")) is True
    assert await app.project_engine_event(ReasoningDelta("run", "think", 1)) is True
    assert await app.project_engine_event(TextDelta("run", "partial", 2)) is True


def test_escape_cancels_once_from_any_focused_widget() -> None:
    async def scenario(focus_target: str) -> None:
        cancelled: list[str] = []
        app = MicaTuiApp(
            run_cancel_requester=lambda run_id: cancelled.append(run_id) or True
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await start_streaming_run(app)
            if focus_target == "bash":
                assert await app.project_engine_event(
                    ToolPrepared("run", 3, bash_snapshot())
                )
                assert await app.project_engine_event(
                    ToolStarted("run", 4, "call-bash", "bash")
                )
                assert await app.project_engine_event(
                    ToolOutputDelta("run", 5, "call-bash", "bash", "first-output")
                )
            elif focus_target == "ask":
                assert await app.project_engine_event(
                    ToolPrepared("run", 3, ask_snapshot())
                )
                assert not await app.project_engine_event(
                    UserInputRequested(
                        "run",
                        4,
                        "interaction",
                        "Pick",
                        AskPrompt("Pick", options=(AskOption("yes"), AskOption("no"))),
                    )
                )
            elif focus_target == "permission":
                assert await app.project_engine_event(
                    ToolPrepared("run", 3, bash_snapshot())
                )
                assert not await app.project_engine_event(
                    PermissionRequested(
                        "run", 4, "interaction", "call-bash", "bash", "allow?"
                    )
                )
            await pilot.pause()
            targets = {
                "composer": app.query_one(MicaComposer),
                "transcript": app.query_one(ConversationTranscript),
                "reasoning": app.query_one(ReasoningView),
                "bash": app.query_one(BashToolCard) if focus_target == "bash" else None,
                "ask": app.query_one(AskInteractionView)
                if focus_target == "ask"
                else None,
                "permission": app.query_one(PermissionInteractionView)
                if focus_target == "permission"
                else None,
            }
            widget = targets[focus_target]
            assert widget is not None
            widget.focus()
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert cancelled == ["run"]
            assert app.is_running
            await pilot.press("escape")
            await pilot.pause()
            assert cancelled == ["run"]

    for target in (
        "composer",
        "transcript",
        "reasoning",
        "bash",
        "ask",
        "permission",
    ):
        asyncio.run(scenario(target))


def test_idle_escape_is_noop_and_does_not_arm_exit() -> None:
    async def scenario() -> None:
        cancelled: list[str] = []
        clock = FakeClock()
        app = RecordingApp(
            clock=clock,
            run_cancel_requester=lambda run_id: cancelled.append(run_id) or True,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.text = "keep draft"
            children = list(app.query_one("#transcript").children)
            await pilot.press("escape")
            await pilot.pause()
            assert cancelled == []
            assert composer.text == "keep draft"
            assert list(app.query_one("#transcript").children) == children
            assert app.is_running
            assert app.exits == []
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.is_running
            assert app.exits == []
            assert composer.text == "keep draft"

    asyncio.run(scenario())


def test_ctrl_c_idle_requires_two_presses_and_resets_after_timeout() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        app = RecordingApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.text = "keep draft"
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.is_running
            assert app.exits == []
            assert composer.text == "keep draft"
            assert list(app.query(UserMessageCard)) == []
            clock.advance(2.1)
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.is_running
            assert app.exits == []
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.exits == [0]

    asyncio.run(scenario())


def test_ctrl_c_during_run_cancels_once_then_exits_inside_window() -> None:
    async def scenario() -> None:
        cancelled: list[str] = []
        clock = FakeClock()
        app = RecordingApp(
            clock=clock,
            run_cancel_requester=lambda run_id: cancelled.append(run_id) or True,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await start_streaming_run(app)
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert cancelled == ["run"]
            assert app.is_running
            assert app.exits == []
            await pilot.press("escape")
            await pilot.pause()
            assert cancelled == ["run"]
            clock.advance(1.0)
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert cancelled == ["run"]
            assert app.exits == [0]

    asyncio.run(scenario())


def test_failed_cancel_still_arms_exit_and_second_ctrl_c_exits() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        app = RecordingApp(clock=clock, run_cancel_requester=lambda _run_id: False)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await start_streaming_run(app)
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.is_running
            assert app.exits == []
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.exits == [0]

    asyncio.run(scenario())


def test_terminated_or_closed_keys_are_noop() -> None:
    async def scenario() -> None:
        cancelled: list[str] = []
        app = MicaTuiApp(
            run_cancel_requester=lambda run_id: cancelled.append(run_id) or True
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await start_streaming_run(app)
            assert await app.project_engine_event(RunCompleted("run", 3)) is True
            await pilot.press("escape")
            await pilot.pause()
            assert cancelled == []
            app.stop_runtime_input()
            app.action_interrupt_run()
            app.action_confirm_exit()
            assert cancelled == []
            assert app.is_running

    asyncio.run(scenario())


def test_ask_and_permission_escape_do_not_synthesize_responses() -> None:
    async def scenario() -> None:
        cancelled: list[str] = []
        answers: list[object] = []
        permissions: list[PermissionResponse] = []
        app = MicaTuiApp(
            user_input_responder=lambda response: answers.append(response) or True,
            permission_responder=lambda response: permissions.append(response) or True,
            run_cancel_requester=lambda run_id: cancelled.append(run_id) or True,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run-ask"))
            await app.project_engine_event(ToolPrepared("run-ask", 1, ask_snapshot()))
            assert not await app.project_engine_event(
                UserInputRequested(
                    "run-ask",
                    2,
                    "interaction",
                    "Pick",
                    AskPrompt("Pick", options=(AskOption("yes"), AskOption("no"))),
                )
            )
            view = app.query_one(AskInteractionView)
            app.query_one(ConversationTranscript).focus()
            await pilot.press("escape")
            await pilot.pause()
            assert cancelled == ["run-ask"]
            assert answers == []
            assert view.is_active is False
            view._request_cancel()
            assert cancelled == ["run-ask"]

            await app.project_engine_event(RunStarted("run-perm"))
            await app.project_engine_event(ToolPrepared("run-perm", 1, bash_snapshot()))
            assert not await app.project_engine_event(
                PermissionRequested(
                    "run-perm", 2, "perm", "call-bash", "bash", "allow?"
                )
            )
            panel = app.query_one(PermissionInteractionView)
            app.query_one(ConversationTranscript).focus()
            await pilot.press("escape")
            await pilot.pause()
            assert cancelled == ["run-ask", "run-perm"]
            assert permissions == []
            assert panel.is_active is False
            assert len(app.query(PermissionInteractionView)) == 0

    asyncio.run(scenario())


def test_escape_keeps_projected_text_and_does_not_exit() -> None:
    async def scenario() -> None:
        cancelled: list[str] = []
        app = MicaTuiApp(
            run_cancel_requester=lambda run_id: cancelled.append(run_id) or True
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await start_streaming_run(app)
            view = app.query_one(AssistantResponseView)
            app.query_one(ConversationTranscript).focus()
            await pilot.press("escape")
            await pilot.pause()
            assert cancelled == ["run"]
            assert view.text == "partial"
            assert app.is_running
            assert await app.project_engine_event(RunCancelled("run", 3)) is True
            await pilot.pause()
            assert view.is_streaming is False
            assert len(view.query(".streaming-cursor")) == 0
            assert await app.project_engine_event(TextDelta("run", "late", 4)) is False
            assert view.text == "partial"

    asyncio.run(scenario())
