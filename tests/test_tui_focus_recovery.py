"""P6-10E AppBlur / AppFocus ownership restoration."""

from __future__ import annotations

import asyncio

from textual.events import AppBlur, AppFocus

from mica_agent.events import (
    PermissionRequested,
    RunCompleted,
    RunStarted,
    TextDelta,
    ToolPrepared,
    UserInputRequested,
)
from mica_agent.interactions import AskOption, AskPrompt
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)
from mica_agent.tui import (
    AskInteractionView,
    MicaComposer,
    MicaTuiApp,
    PermissionInteractionView,
)
from mica_agent.tui.transcript import ConversationTranscript


def bash_snapshot() -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        "call",
        "bash",
        ToolPresentationKind.BASH,
        ToolPresentationStatus.PENDING,
        {"command": "printf first"},
    )


async def blur_and_focus(app: MicaTuiApp, pilot) -> None:
    app.post_message(AppBlur())
    await pilot.pause()
    app.screen.set_focus(None)
    await pilot.pause()
    app.post_message(AppFocus())
    await pilot.pause()


def test_app_focus_restores_ask_options_without_resetting_selection() -> None:
    async def scenario() -> None:
        responses: list[object] = []
        app = MicaTuiApp(
            user_input_responder=lambda response: responses.append(response) or True
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run"))
            assert not await app.project_engine_event(
                UserInputRequested(
                    "run",
                    1,
                    "interaction",
                    "Pick",
                    AskPrompt(
                        "Pick",
                        options=(AskOption("yes"), AskOption("no")),
                    ),
                )
            )
            await pilot.pause()
            view = app.query_one(AskInteractionView)
            await pilot.press("down")
            assert view.selected_option_index == 1
            await blur_and_focus(app, pilot)
            assert view.selected_option_index == 1
            assert view.is_active is True
            assert responses == []
            assert view.has_focus

    asyncio.run(scenario())


def test_app_focus_restores_ask_input_and_keeps_typed_text() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(user_input_responder=lambda _response: True)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run"))
            assert not await app.project_engine_event(
                UserInputRequested(
                    "run",
                    1,
                    "interaction",
                    "Pick",
                    AskPrompt(
                        "Pick",
                        options=(AskOption("yes"), AskOption("no")),
                    ),
                )
            )
            await pilot.pause()
            view = app.query_one(AskInteractionView)
            await pilot.press("n", "a", "b")
            notes = view.query_one(".ask-input")
            assert notes.value == "ab"
            await blur_and_focus(app, pilot)
            restored = view.query_one(".ask-input")
            assert restored.value == "ab"
            assert restored.has_focus
            assert view.selected_option_index == 0

    asyncio.run(scenario())


def test_app_focus_restores_permission_without_resetting_index() -> None:
    async def scenario() -> None:
        responses: list[object] = []
        app = MicaTuiApp(
            permission_responder=lambda response: responses.append(response) or True
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run"))
            assert await app.project_engine_event(
                ToolPrepared("run", 1, bash_snapshot())
            )
            assert not await app.project_engine_event(
                PermissionRequested("run", 2, "perm", "call", "bash", "allow?")
            )
            await pilot.pause()
            panel = app.query_one(PermissionInteractionView)
            await pilot.press("down")
            assert panel.selected_index == 1
            await blur_and_focus(app, pilot)
            assert panel.selected_index == 1
            assert panel.is_active is True
            assert responses == []
            assert panel.has_focus

    asyncio.run(scenario())


def test_app_focus_restores_transcript_during_active_run() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.text = "keep draft"
            assert await app.project_engine_event(RunStarted("run"))
            assert await app.project_engine_event(TextDelta("run", "partial\n" * 20, 1))
            transcript = app.query_one(ConversationTranscript)
            transcript.focus()
            await pilot.press("pageup")
            await pilot.pause()
            detached = transcript.scroll_y
            assert transcript.is_following is False
            await blur_and_focus(app, pilot)
            assert transcript.has_focus
            assert transcript.scroll_y == detached
            assert composer.text == "keep draft"
            assert (
                list(app.query("#new-output")) == []
                or transcript.has_pending_new_output
            )

    asyncio.run(scenario())


def test_app_focus_restores_idle_composer_and_draft() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.text = "keep draft"
            assert await app.project_engine_event(RunStarted("run"))
            assert await app.project_engine_event(RunCompleted("run", 1))
            await pilot.pause()
            app.query_one(ConversationTranscript).focus()
            await blur_and_focus(app, pilot)
            assert composer.has_focus
            assert composer.text == "keep draft"
            assert composer is app.query_one(MicaComposer)

    asyncio.run(scenario())
