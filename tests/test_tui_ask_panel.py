from __future__ import annotations

import asyncio

import pytest

from mica_agent.events import (
    RunCancelled,
    RunStarted,
    ToolFinished,
    ToolPrepared,
    UserInputRequested,
)
from mica_agent.interactions import AskOption, AskPrompt, UserInputResponse
from mica_agent.tool_contracts import ToolResultStatus
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)
from mica_agent.tui import AskInteractionView, MicaTuiApp
from mica_agent.tui.interaction_layer import InteractionOverlay


def ask_snapshot(
    status: ToolPresentationStatus, answer: str = ""
) -> ToolPresentationSnapshot:
    payload = {
        "question": "Pick",
        **({"answer": answer} if status is not ToolPresentationStatus.PENDING else {}),
    }
    return ToolPresentationSnapshot(
        "call", "ask", ToolPresentationKind.ASK, status, payload
    )


def test_ask_view_repr_and_structured_response() -> None:
    responses: list[UserInputResponse] = []
    view = AskInteractionView(
        run_id="run",
        interaction_id="interaction",
        prompt=AskPrompt(
            "Pick",
            title="Choice",
            options=(
                AskOption("first", "first description"),
                AskOption("second", recommended=True),
            ),
            allow_custom=True,
        ),
        user_input_responder=lambda response: responses.append(response) or True,
    )
    assert repr(view) == "<AskInteractionView>"
    assert "first description" in view.render().plain
    assert "recommended" in view.render().plain
    view._selected_index = 1
    view._submit_selection()
    assert responses == [UserInputResponse("interaction", "second", 1, None)]


def test_ask_app_updates_the_same_view_and_disables_composer() -> None:
    async def scenario() -> None:
        responses: list[UserInputResponse] = []
        app = MicaTuiApp(
            user_input_responder=lambda response: responses.append(response) or True
        )
        async with app.run_test(size=(70, 20)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run"))
            assert await app.project_engine_event(
                ToolPrepared("run", 1, ask_snapshot(ToolPresentationStatus.PENDING))
            )
            # Interaction events retain the historical False projection result.
            assert not await app.project_engine_event(
                UserInputRequested(
                    "run",
                    3,
                    "interaction",
                    "Pick",
                    AskPrompt("Pick", options=(AskOption("yes"), AskOption("no"))),
                )
            )
            await pilot.pause()
            view = app.query_one(AskInteractionView)
            overlay = app.query_one(InteractionOverlay)
            content = view.query_one(".ask-content")
            rendered = view.render().plain
            assert view.parent is overlay
            assert view not in app.query_one("#transcript").children
            assert overlay.display is True
            assert app.query_one("#work-status").display is False
            assert app.query_one("#composer").display is False
            assert app.query_one("#runtime-facts").display is False
            assert view.region.height >= 8
            assert content.region.height >= 6
            assert "Pick" in rendered
            assert "1. yes" in rendered
            assert "2. no" in rendered
            assert "Enter select" in rendered
            assert app.query_one("#composer").disabled
            await pilot.press("2")
            assert view.selected_option_index == 1
            assert responses == []
            await pilot.press("enter")
            assert responses[0].selected_option_index == 1
            assert await app.project_engine_event(
                ToolFinished(
                    "run",
                    4,
                    "call",
                    "ask",
                    ToolResultStatus.SUCCESS,
                    ask_snapshot(ToolPresentationStatus.SUCCESS, "no"),
                )
            )
            await pilot.pause()
            record = app.query_one(AskInteractionView)
            assert record is not view
            assert record.parent is app.query_one("#transcript")
            assert record.terminal_status is ToolResultStatus.SUCCESS
            assert overlay.display is False
            assert app.query_one("#composer").display is True
            assert not app.query_one("#composer").disabled

    asyncio.run(scenario())


def test_pilot_navigation_bounds_unicode_and_notes_inputs() -> None:
    async def scenario() -> None:
        responses: list[UserInputResponse] = []
        app = MicaTuiApp(
            user_input_responder=lambda response: responses.append(response) or True
        )
        async with app.run_test(size=(50, 14)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                UserInputRequested(
                    "run",
                    1,
                    "interaction",
                    "Pick",
                    AskPrompt(
                        "Pick",
                        options=tuple(AskOption(str(i)) for i in range(1, 5)),
                        allow_custom=True,
                    ),
                )
            )
            view = app.query_one(AskInteractionView)
            view.focus()
            await pilot.press("5")
            assert view.selected_option_index == 4
            await pilot.press("6")
            assert view.selected_option_index == 4
            await pilot.press("５")
            assert view.selected_option_index == 4
            await pilot.press("n")
            await pilot.press("n")
            assert view.notes is None
            await pilot.press("enter")
            assert view.notes == "n"
            notes_input = view.query_one(".ask-input")
            await pilot.press("n")
            notes_input.value = "n note"
            notes_input.focus()
            await pilot.press("enter")
            assert view.notes == "n note"
            assert responses == []
            await pilot.press("5")
            await pilot.press("enter")
            await pilot.press("enter")
            assert responses[0].selected_option_index is None
            assert responses[0].answer == ""

    asyncio.run(scenario())


def test_free_and_custom_inputs_keep_n_as_text_and_accept_empty() -> None:
    async def scenario() -> None:
        responses: list[UserInputResponse] = []
        app = MicaTuiApp(
            user_input_responder=lambda response: responses.append(response) or True
        )
        async with app.run_test(size=(50, 14)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                UserInputRequested(
                    "run", 1, "free-interaction", "Question", AskPrompt("Question")
                )
            )
            free_input = app.query_one(AskInteractionView).query_one(".ask-input")
            free_input.value = "n"
            await pilot.press("enter")
            assert responses[0].answer == "n"
            assert responses[0].selected_option_index is None
            assert responses[0].notes is None

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["responder", "canceller"])
def test_async_callback_shapes_are_rejected_without_repr_leak(kind: str) -> None:
    async def callback(*_args: object) -> bool:
        return True

    with pytest.raises(TypeError, match="synchronous callable"):
        if kind == "responder":
            MicaTuiApp(user_input_responder=callback)
        else:
            MicaTuiApp(run_cancel_requester=callback)

    async def scenario() -> None:
        calls: list[str] = []
        app = MicaTuiApp(
            run_cancel_requester=lambda run_id: calls.append(run_id) or True
        )
        async with app.run_test(size=(60, 18)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                UserInputRequested("run", 1, "interaction", "Question")
            )
            view = app.query_one(AskInteractionView)
            view._request_cancel()
            view._request_cancel()
            assert calls == ["run"]
            assert await app.project_engine_event(RunCancelled("run", 2))
            await pilot.pause()
            record = app.query_one(AskInteractionView)
            assert record.terminal_status is ToolResultStatus.CANCELLED
            assert "operation cancelled" in record.render().plain

    asyncio.run(scenario())


def test_structured_ask_presentation_accepts_extra_arguments_and_mapping_answer() -> (
    None
):
    from mica_agent.tool_contracts import ToolRequest, ToolResult
    from mica_agent.tool_presentation import (
        complete_tool_presentation,
        prepare_tool_presentation,
    )

    request = ToolRequest(
        "call",
        "ask",
        {"question": "Pick", "title": "Title", "options": [], "allow_custom": True},
    )
    pending = prepare_tool_presentation(request)
    assert pending.payload == {"question": "Pick"}
    result = ToolResult(
        "call",
        "ask",
        ToolResultStatus.SUCCESS,
        "done",
        {"answer": "yes", "selected_option_index": None, "notes": "n"},
    )
    final = complete_tool_presentation(request, result)
    assert final.payload == {"question": "Pick", "answer": "yes"}


if __name__ == "__main__":
    test_ask_view_repr_and_structured_response()
