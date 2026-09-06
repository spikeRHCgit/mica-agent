from __future__ import annotations

import asyncio

import pytest

from mica_agent.events import (
    PermissionRequested,
    RunCancelled,
    RunCompleted,
    RunStarted,
    ToolFinished,
    ToolPrepared,
    ToolStarted,
)
from mica_agent.interactions import PermissionAnswer, PermissionResponse
from mica_agent.tool_contracts import ToolResultStatus
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
)
from mica_agent.tui import (
    MicaTuiApp,
    PermissionInteractionView,
)
from mica_agent.tui.tool_cards import BashToolCard, ToolCardState


def bash_snapshot(
    status: ToolPresentationStatus,
    command: str = "rm -rf data",
    *,
    message: str | None = None,
) -> ToolPresentationSnapshot:
    payload = {"command": command}
    if status is not ToolPresentationStatus.PENDING:
        payload.update({"output": "", "exit_code": None})
    return ToolPresentationSnapshot(
        "call",
        "bash",
        ToolPresentationKind.BASH,
        status,
        payload,
        message,
    )


def test_permission_view_is_plain_text_and_uses_public_response() -> None:
    responses: list[PermissionResponse] = []
    view = PermissionInteractionView(
        run_id="run",
        interaction_id="interaction",
        presentation=bash_snapshot(
            ToolPresentationStatus.PENDING,
            "printf '[secret-markup]'",
        ),
        permission_responder=lambda response: responses.append(response) or True,
    )

    rendered = view.render().plain
    assert "Bash command" in rendered
    assert "$ printf '[secret-markup]'" in rendered
    assert "Do you want to proceed?" in rendered
    assert "› 1. Yes" in rendered
    assert "  2. No" in rendered
    assert "↑/↓ to navigate · Enter to confirm · Esc to cancel" in rendered
    assert repr(view) == "<PermissionInteractionView>"
    assert "interaction" not in repr(view)
    assert "secret-markup" not in repr(view)

    view._submit_selection()
    assert responses == [PermissionResponse("interaction", PermissionAnswer.ALLOW_ONCE)]
    assert view.is_locked is True


@pytest.mark.parametrize("answer_index", [0, 1])
def test_permission_app_selects_ascii_options_and_cleans_panel(
    answer_index: int,
) -> None:
    async def scenario() -> None:
        responses: list[PermissionResponse] = []
        app = MicaTuiApp(
            permission_responder=lambda response: responses.append(response) or True
        )
        async with app.run_test(size=(70, 20)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer")
            composer.text = "draft"
            assert await app.project_engine_event(RunStarted("run")) is True
            pending = bash_snapshot(ToolPresentationStatus.PENDING)
            assert await app.project_engine_event(ToolPrepared("run", 1, pending))
            assert not await app.project_engine_event(
                PermissionRequested(
                    "run", 2, "interaction", "call", "bash", "untrusted event text"
                )
            )
            await pilot.pause()
            panel = app.query_one(PermissionInteractionView)
            card = app.query_one(BashToolCard)
            assert card.state is ToolCardState.PENDING
            assert panel.parent is app.screen
            assert panel not in app.query_one("#transcript").query("*")
            assert app.screen.children.index(panel) < app.screen.children.index(
                app.query_one("#work-status")
            )
            assert composer.disabled
            assert composer.text == "draft"
            assert "untrusted event text" not in panel.render().plain

            await pilot.press("１")
            assert panel.selected_index == 0
            await pilot.press("2" if answer_index else "1")
            assert panel.selected_index == answer_index
            assert responses == []
            await pilot.press("enter")
            await pilot.pause()
            expected = (
                PermissionAnswer.ALLOW_ONCE
                if answer_index == 0
                else PermissionAnswer.DENY
            )
            assert responses == [PermissionResponse("interaction", expected)]
            assert len(app.query(PermissionInteractionView)) == 0
            assert not composer.disabled
            assert composer.text == "draft"
            assert card.state is ToolCardState.PENDING

            await pilot.press("enter")
            assert responses == [PermissionResponse("interaction", expected)]
            if answer_index == 0:
                assert await app.project_engine_event(
                    ToolStarted("run", 3, "call", "bash")
                )
                assert card.state is ToolCardState.RUNNING
            final_status = (
                ToolPresentationStatus.SUCCESS
                if answer_index == 0
                else ToolPresentationStatus.FAILED
            )
            final = bash_snapshot(final_status)
            assert await app.project_engine_event(
                ToolFinished(
                    "run",
                    4,
                    "call",
                    "bash",
                    ToolResultStatus.SUCCESS
                    if answer_index == 0
                    else ToolResultStatus.PERMISSION_DENIED,
                    final,
                )
            )
            assert card.state in {ToolCardState.SUCCESS, ToolCardState.FAILED}

    asyncio.run(scenario())


def test_permission_esc_only_requests_run_cancel() -> None:
    async def scenario() -> None:
        responses: list[PermissionResponse] = []
        cancelled: list[str] = []
        app = MicaTuiApp(
            permission_responder=lambda response: responses.append(response) or True,
            run_cancel_requester=lambda run_id: cancelled.append(run_id) or True,
        )
        async with app.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared("run", 1, bash_snapshot(ToolPresentationStatus.PENDING))
            )
            await app.project_engine_event(
                PermissionRequested("run", 2, "interaction", "call", "bash", "allow?")
            )
            panel = app.query_one(PermissionInteractionView)
            await pilot.press("escape")
            await pilot.pause()
            assert cancelled == ["run"]
            assert responses == []
            assert panel.is_locked is True
            assert len(app.query(PermissionInteractionView)) == 0
            assert not app.query_one("#composer").disabled
            assert await app.project_engine_event(RunCancelled("run", 3))
            assert not app.query_one("#composer").disabled
            await pilot.pause()
            assert len(app.query(PermissionInteractionView)) == 0
            panel._request_cancel()
            assert cancelled == ["run"]

    asyncio.run(scenario())


def test_permission_requests_require_exact_prepared_identity_and_dedupe() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(permission_responder=lambda _response: True)
        async with app.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared("run", 1, bash_snapshot(ToolPresentationStatus.PENDING))
            )
            assert not await app.project_engine_event(
                PermissionRequested("run", 2, "wrong", "other", "bash", "x")
            )
            assert not await app.project_engine_event(
                PermissionRequested("run", 3, "wrong", "call", "read", "x")
            )
            assert len(app.query(PermissionInteractionView)) == 0
            assert not await app.project_engine_event(
                PermissionRequested("run", 4, "interaction", "call", "bash", "x")
            )
            assert len(app.query(PermissionInteractionView)) == 1
            assert not await app.project_engine_event(
                PermissionRequested("run", 5, "interaction", "call", "bash", "x")
            )
            assert len(app.query(PermissionInteractionView)) == 1
            await app.project_engine_event(RunCancelled("run", 6))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "callback", [None, lambda _response: False, lambda _response: 1]
)
def test_permission_rejected_callbacks_keep_panel_with_fixed_message(callback) -> None:
    view = PermissionInteractionView(
        run_id="run",
        interaction_id="interaction",
        presentation=bash_snapshot(ToolPresentationStatus.PENDING),
        permission_responder=callback,
    )
    view._submit_selection()
    assert view.is_locked is False
    assert view.error_message == "Permission response was not accepted; try again."
    assert "secret" not in view.render().plain


@pytest.mark.parametrize("kind", ["permission", "cancel"])
def test_async_callback_shapes_are_rejected(kind: str) -> None:
    async def callback(*_args: object) -> bool:
        return True

    with pytest.raises(TypeError, match="synchronous callable"):
        if kind == "permission":
            PermissionInteractionView(
                run_id="run",
                interaction_id="interaction",
                presentation=bash_snapshot(ToolPresentationStatus.PENDING),
                permission_responder=callback,
            )
        else:
            PermissionInteractionView(
                run_id="run",
                interaction_id="interaction",
                presentation=bash_snapshot(ToolPresentationStatus.PENDING),
                run_cancel_requester=callback,
            )


def test_permission_from_run_one_survives_run_two_terminal_cleanup() -> None:
    async def scenario() -> None:
        responses: list[PermissionResponse] = []
        app = MicaTuiApp(
            permission_responder=lambda response: responses.append(response) or True
        )
        async with app.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("one"))
            await app.project_engine_event(
                ToolPrepared("one", 2, bash_snapshot(ToolPresentationStatus.PENDING))
            )
            assert not await app.project_engine_event(
                PermissionRequested(
                    "one", 3, "permission-one", "call", "bash", "allow?"
                )
            )
            await pilot.pause()
            panel = app.query_one(PermissionInteractionView)
            panel._selected_index = 1
            composer = app.query_one("#composer")
            assert composer.disabled

            assert await app.project_engine_event(RunStarted("two"))
            assert await app.project_engine_event(RunCompleted("two", 2))
            await pilot.pause()
            assert len(app.query(PermissionInteractionView)) == 1
            assert app.query_one(PermissionInteractionView) is panel
            assert panel.selected_index == 1
            assert composer.disabled
            assert responses == []

            assert await app.project_engine_event(RunCancelled("one", 4))
            await pilot.pause()
            assert len(app.query(PermissionInteractionView)) == 0
            assert not composer.disabled

    asyncio.run(scenario())


def test_unexpected_awaitable_is_closed_and_keeps_permission_panel() -> None:
    async def scenario() -> None:
        created: list[object] = []

        async def response_value() -> bool:
            return True

        def responder(_response: PermissionResponse) -> bool:
            value = response_value()
            created.append(value)
            return value  # type: ignore[return-value]

        view = PermissionInteractionView(
            run_id="run",
            interaction_id="interaction",
            presentation=bash_snapshot(ToolPresentationStatus.PENDING),
            permission_responder=responder,
        )
        view._submit_selection()
        await asyncio.sleep(0)
        assert len(created) == 1
        assert created[0].cr_frame is None  # type: ignore[attr-defined]
        assert view.is_active
        assert view.error_message == "Permission response was not accepted; try again."
        assert "Permission response was not accepted" in view.render().plain

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("no_color", "ansi_color"),
    [(True, None), (False, False)],
)
def test_permission_panel_scrolls_in_short_terminal_and_stays_readable(
    monkeypatch: pytest.MonkeyPatch,
    no_color: bool,
    ansi_color: bool | None,
) -> None:
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    async def scenario() -> None:
        command = "printf 'long-safe-command-'" + ("x" * 160)
        responses: list[PermissionResponse] = []
        app = MicaTuiApp(
            ansi_color=ansi_color,
            permission_responder=lambda response: responses.append(response) or True,
        )
        async with app.run_test(size=(24, 8)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared(
                    "run",
                    2,
                    bash_snapshot(ToolPresentationStatus.PENDING, command),
                )
            )
            assert not await app.project_engine_event(
                PermissionRequested("run", 3, "interaction", "call", "bash", "allow?")
            )
            await pilot.pause()
            panel = app.query_one(PermissionInteractionView)
            composer = app.query_one("#composer")
            assert panel.max_scroll_y > 0
            assert composer.region.bottom == app.screen.size.height
            rendered = panel.render().plain
            assert "long-safe-command-" in rendered
            assert "Do you want to proceed?" in rendered
            assert "1. Yes" in rendered and "2. No" in rendered

            panel.focus()
            start = panel.scroll_y
            await pilot.press("pagedown")
            assert panel.scroll_y > start
            await pilot.press("pageup")
            assert panel.scroll_y <= start
            await pilot.press("2", "enter")
            await pilot.pause()
            assert responses == [
                PermissionResponse("interaction", PermissionAnswer.DENY)
            ]
            assert len(app.query(PermissionInteractionView)) == 0

    asyncio.run(scenario())


def test_exited_permission_view_cannot_call_external_callbacks() -> None:
    async def scenario() -> None:
        responses: list[PermissionResponse] = []
        cancelled: list[str] = []
        app = MicaTuiApp(
            permission_responder=lambda response: responses.append(response) or True,
            run_cancel_requester=lambda run_id: cancelled.append(run_id) or True,
        )
        async with app.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            await app.project_engine_event(RunStarted("run"))
            await app.project_engine_event(
                ToolPrepared("run", 2, bash_snapshot(ToolPresentationStatus.PENDING))
            )
            await app.project_engine_event(
                PermissionRequested("run", 3, "interaction", "call", "bash", "allow?")
            )
            await pilot.pause()
            old_view = app.query_one(PermissionInteractionView)
            app.exit()
            await pilot.pause()
            assert len(app.query(PermissionInteractionView)) == 0
            old_view._submit_selection()
            old_view._request_cancel()
            assert responses == []
            assert cancelled == []

    asyncio.run(scenario())


if __name__ == "__main__":
    test_permission_view_is_plain_text_and_uses_public_response()
