"""P6-10E blocking overlay layout, key ownership, and Ask terminal records."""

from __future__ import annotations

import asyncio

from textual.events import AppBlur, AppFocus

from mica_agent.events import (
    PermissionRequested,
    RunCancelled,
    RunCompleted,
    RunStarted,
    TextDelta,
    ToolFinished,
    ToolPrepared,
    UserInputRequested,
)
from mica_agent.interactions import (
    AskOption,
    AskPrompt,
    PermissionAnswer,
    PermissionResponse,
    UserInputResponse,
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
    MicaComposer,
    MicaTuiApp,
    PermissionInteractionView,
)
from mica_agent.tui.interaction_layer import InteractionOverlay
from mica_agent.tui.tool_cards import BashToolCard
from mica_agent.tui.transcript import ConversationTranscript, UserMessageCard


def ask_snapshot(
    status: ToolPresentationStatus = ToolPresentationStatus.PENDING,
    answer: str = "",
) -> ToolPresentationSnapshot:
    payload = {"question": "Pick"}
    if status is not ToolPresentationStatus.PENDING:
        payload["answer"] = answer
    return ToolPresentationSnapshot(
        "call", "ask", ToolPresentationKind.ASK, status, payload
    )


def bash_snapshot() -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        "call",
        "bash",
        ToolPresentationKind.BASH,
        ToolPresentationStatus.PENDING,
        {"command": "printf first"},
    )


def option_prompt() -> AskPrompt:
    return AskPrompt(
        "Pick",
        title="Choice",
        options=(
            AskOption("first", "first description", recommended=True),
            AskOption("second", "second description"),
        ),
        allow_custom=True,
    )


async def open_ask(
    app: MicaTuiApp, prompt: AskPrompt | None = None
) -> AskInteractionView:
    assert await app.project_engine_event(RunStarted("run")) is True
    assert await app.project_engine_event(ToolPrepared("run", 1, ask_snapshot()))
    assert not await app.project_engine_event(
        UserInputRequested("run", 2, "interaction", "Pick", prompt or option_prompt())
    )
    return app.query_one(AskInteractionView)


def test_overlay_replaces_bottom_chrome_for_ask_and_permission() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(permission_responder=lambda _response: True)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.text = "keep draft"
            assert list(app.query(InteractionOverlay)) == []
            assert app.query_one("#work-status").display is True
            assert composer.display is True
            assert app.query_one("#runtime-facts").display is True

            await open_ask(app)
            await pilot.pause()
            overlay = app.query_one(InteractionOverlay)
            ask = app.query_one(AskInteractionView)
            assert overlay.display is True
            assert ask.parent is overlay
            assert ask not in app.query_one("#transcript").children
            assert app.query_one("#work-status").display is False
            assert composer.display is False
            assert app.query_one("#runtime-facts").display is False
            assert composer.text == "keep draft"
            assert repr(overlay) == "<InteractionOverlay>"
            assert "interaction" not in repr(overlay)

            await app.project_engine_event(
                ToolFinished(
                    "run",
                    3,
                    "call",
                    "ask",
                    ToolResultStatus.SUCCESS,
                    ask_snapshot(ToolPresentationStatus.SUCCESS, "first"),
                )
            )
            await app.project_engine_event(RunCompleted("run", 4))
            await pilot.pause()
            assert overlay.display is False
            assert composer.display is True
            assert composer is app.query_one(MicaComposer)
            assert composer.text == "keep draft"

            assert await app.project_engine_event(RunStarted("run-2"))
            assert await app.project_engine_event(
                ToolPrepared("run-2", 1, bash_snapshot())
            )
            assert not await app.project_engine_event(
                PermissionRequested("run-2", 2, "perm", "call", "bash", "allow?")
            )
            await pilot.pause()
            panel = app.query_one(PermissionInteractionView)
            assert panel.parent is overlay
            assert overlay.display is True
            assert panel not in app.query_one("#transcript").query("*")
            assert app.query_one(BashToolCard).parent is app.query_one("#transcript")
            assert composer.display is False
            assert composer.text == "keep draft"

    asyncio.run(scenario())


def test_ask_keys_work_without_local_focus_and_do_not_leak_to_composer() -> None:
    async def scenario() -> None:
        responses: list[UserInputResponse] = []
        app = MicaTuiApp(
            user_input_responder=lambda response: responses.append(response) or True
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.text = "draft"
            view = await open_ask(app)
            await pilot.pause()
            app.query_one(ConversationTranscript).focus()
            await pilot.pause()
            await pilot.press("down")
            assert view.selected_option_index == 1
            await pilot.press("1")
            assert view.selected_option_index == 0
            await pilot.press("enter")
            assert responses == [UserInputResponse("interaction", "first", 0, None)]
            assert composer.text == "draft"
            assert list(app.query(UserMessageCard)) == []

    asyncio.run(scenario())


def test_permission_keys_work_without_local_focus() -> None:
    async def scenario() -> None:
        responses: list[PermissionResponse] = []
        app = MicaTuiApp(
            permission_responder=lambda response: responses.append(response) or True
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.text = "draft"
            assert await app.project_engine_event(RunStarted("run"))
            assert await app.project_engine_event(
                ToolPrepared("run", 1, bash_snapshot())
            )
            assert not await app.project_engine_event(
                PermissionRequested("run", 2, "perm", "call", "bash", "allow?")
            )
            await pilot.pause()
            panel = app.query_one(PermissionInteractionView)
            app.query_one(ConversationTranscript).focus()
            await pilot.press("down")
            assert panel.selected_index == 1
            await pilot.press("up")
            assert panel.selected_index == 0
            await pilot.press("2")
            await pilot.press("enter")
            await pilot.pause()
            assert responses == [PermissionResponse("perm", PermissionAnswer.DENY)]
            assert composer.text == "draft"
            assert list(app.query(PermissionInteractionView)) == []

    asyncio.run(scenario())


def test_ask_input_mode_keeps_digits_and_n_as_text() -> None:
    async def scenario() -> None:
        responses: list[UserInputResponse] = []
        app = MicaTuiApp(
            user_input_responder=lambda response: responses.append(response) or True
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = await open_ask(app)
            await pilot.pause()
            await pilot.press("n")
            notes = view.query_one(".ask-input")
            assert notes.has_focus
            await pilot.press("1", "n")
            await pilot.press("enter")
            assert view.notes == "1n"
            assert responses == []
            await pilot.press("3")
            await pilot.press("enter")
            custom = view.query_one(".ask-input")
            custom.focus()
            await pilot.press("n", "1")
            await pilot.press("enter")
            assert responses[0].answer == "n1"
            assert responses[0].selected_option_index is None
            assert list(app.query(UserMessageCard)) == []

    asyncio.run(scenario())


def test_ask_terminal_record_keeps_timeline_and_closes_overlay() -> None:
    async def scenario() -> None:
        responses: list[UserInputResponse] = []
        app = MicaTuiApp(
            user_input_responder=lambda response: responses.append(response) or True
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run"))
            assert await app.project_engine_event(TextDelta("run", "before", 1))
            assert await app.project_engine_event(
                ToolPrepared("run", 2, ask_snapshot())
            )
            assert not await app.project_engine_event(
                UserInputRequested("run", 3, "interaction", "Pick", option_prompt())
            )
            await pilot.pause()
            overlay = app.query_one(InteractionOverlay)
            active = app.query_one(AskInteractionView)
            assert active.parent is overlay
            await pilot.press("enter")
            assert responses
            assert overlay.display is True
            assert "waiting for tool completion" in active.render().plain
            assert await app.project_engine_event(
                ToolFinished(
                    "run",
                    4,
                    "call",
                    "ask",
                    ToolResultStatus.SUCCESS,
                    ask_snapshot(ToolPresentationStatus.SUCCESS, "first"),
                )
            )
            assert await app.project_engine_event(TextDelta("run", "after", 5))
            await app.project_engine_event(RunCompleted("run", 6))
            await pilot.pause()
            record = app.query_one(AskInteractionView)
            children = [
                child
                for child in app.query_one("#transcript").children
                if isinstance(child, (AssistantResponseView, AskInteractionView))
            ]
            assert overlay.display is False
            assert record is not active
            assert record.parent is app.query_one("#transcript")
            assert [type(child) for child in children] == [
                AssistantResponseView,
                AskInteractionView,
                AssistantResponseView,
            ]
            assert children[0].text == "before"
            assert children[2].text == "after"
            assert list(app.query(UserMessageCard)) == []
            assert len(app.query(AskInteractionView)) == 1

    asyncio.run(scenario())


def test_ask_escape_settles_one_cancelled_record() -> None:
    async def scenario() -> None:
        answers: list[object] = []
        cancelled: list[str] = []
        app = MicaTuiApp(
            user_input_responder=lambda response: answers.append(response) or True,
            run_cancel_requester=lambda run_id: cancelled.append(run_id) or True,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = await open_ask(app)
            await pilot.pause()
            overlay = app.query_one(InteractionOverlay)
            app.query_one(ConversationTranscript).focus()
            await pilot.press("escape")
            await pilot.pause()
            assert cancelled == ["run"]
            assert answers == []
            assert view.is_active is False
            assert await app.project_engine_event(RunCancelled("run", 3))
            await pilot.pause()
            records = list(app.query(AskInteractionView))
            assert len(records) == 1
            assert records[0].parent is app.query_one("#transcript")
            assert records[0].terminal_status is ToolResultStatus.CANCELLED
            assert overlay.display is False
            assert app.query_one(MicaComposer).display is True
            assert not app.query_one(MicaComposer).disabled
            records[0]._request_cancel()
            records[0]._submit_selection()
            assert cancelled == ["run"]
            assert answers == []

    asyncio.run(scenario())


def test_overlay_keeps_transcript_row_on_extreme_terminal() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(
            user_input_responder=lambda _response: True,
            permission_responder=lambda _response: True,
        )
        async with app.run_test(size=(24, 8)) as pilot:
            await pilot.pause()
            await open_ask(app)
            await pilot.pause()
            overlay = app.query_one(InteractionOverlay)
            transcript = app.query_one("#transcript")
            assert overlay.display is True
            assert transcript.region.height >= 1
            assert overlay.region.y >= transcript.region.y
            assert overlay.region.bottom <= app.screen.size.height
            assert overlay.region.x >= 0
            assert overlay.region.right <= app.screen.size.width
            await pilot.press("down")
            assert app.query_one(AskInteractionView).selected_option_index == 1

    asyncio.run(scenario())


def test_permission_overlay_scrolls_without_moving_transcript() -> None:
    async def scenario() -> None:
        command = "printf 'long-safe-command-'" + ("x" * 160)
        app = MicaTuiApp(permission_responder=lambda _response: True)
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run"))
            assert await app.project_engine_event(
                ToolPrepared(
                    "run",
                    1,
                    ToolPresentationSnapshot(
                        "call",
                        "bash",
                        ToolPresentationKind.BASH,
                        ToolPresentationStatus.PENDING,
                        {"command": command},
                    ),
                )
            )
            assert not await app.project_engine_event(
                PermissionRequested("run", 2, "perm", "call", "bash", "allow?")
            )
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            panel = app.query_one(PermissionInteractionView)
            overlay = app.query_one(InteractionOverlay)
            transcript.focus(scroll_visible=False)
            await pilot.pause()
            start = transcript.scroll_y
            overlay_start = overlay.scroll_y
            await pilot.press("pagedown")
            await pilot.pause()
            assert transcript.scroll_y == start
            assert panel.scroll_y == 0
            assert overlay.scroll_y > overlay_start
            await pilot.press("pageup")
            await pilot.pause()
            assert transcript.scroll_y == start
            assert overlay.scroll_y == overlay_start

    asyncio.run(scenario())


def test_idle_digit_and_enter_still_belong_to_composer() -> None:
    async def scenario() -> None:
        submitted: list[str] = []

        class Recording(MicaTuiApp):
            def on_mica_composer_submitted(
                self, message: MicaComposer.Submitted
            ) -> None:
                submitted.append(message.text)

        app = Recording()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            await pilot.press("1", "n")
            assert app.query_one(MicaComposer).text == "1n"
            await pilot.press("enter")
            assert submitted == ["1n"]

    asyncio.run(scenario())


def test_app_blur_does_not_submit_or_reset_ask_state() -> None:
    async def scenario() -> None:
        responses: list[UserInputResponse] = []
        app = MicaTuiApp(
            user_input_responder=lambda response: responses.append(response) or True
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = await open_ask(app)
            await pilot.pause()
            await pilot.press("down")
            app.post_message(AppBlur())
            await pilot.pause()
            app.screen.set_focus(None)
            app.post_message(AppFocus())
            await pilot.pause()
            assert view.selected_option_index == 1
            assert responses == []
            assert view.is_active is True
            assert app.query_one(AskInteractionView).has_focus or view.query(
                ".ask-input"
            )

    asyncio.run(scenario())
