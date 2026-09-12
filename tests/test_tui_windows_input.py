"""P6-10F native input reliability: owner self-heal, page routing, Ctrl+C.

These tests never deliver a synthetic ``AppFocus``: the first key after a
blur must repair the current input owner by itself, because real Windows
terminals may not report focus transitions at all.
"""

from __future__ import annotations

import asyncio
import gc
import warnings

from rich.cells import cell_len
from textual.events import AppBlur, AppFocus, MouseScrollUp, Paste

from mica_agent.events import (
    PermissionRequested,
    RunCompleted,
    RunStarted,
    TextDelta,
    ToolPrepared,
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
    MicaComposer,
    MicaTuiApp,
    PermissionInteractionView,
)
from mica_agent.tui.interaction_layer import InteractionOverlay
from mica_agent.tui.transcript import (
    ConversationTranscript,
    NewOutputIndicator,
    UserMessageCard,
)


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


def ask_prompt() -> AskPrompt:
    return AskPrompt("Pick", options=(AskOption("yes"), AskOption("no")))


def ask_snapshot() -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        "call-ask",
        "ask",
        ToolPresentationKind.ASK,
        ToolPresentationStatus.PENDING,
        {"question": "Pick"},
    )


def bash_snapshot() -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        "call-bash",
        "bash",
        ToolPresentationKind.BASH,
        ToolPresentationStatus.PENDING,
        {"command": "printf first"},
    )


def write_snapshot() -> ToolPresentationSnapshot:
    return ToolPresentationSnapshot(
        "call-write",
        "write",
        ToolPresentationKind.WRITE,
        ToolPresentationStatus.PENDING,
        {"path": "note.txt", "content": "body"},
    )


async def mount_ask(app: MicaTuiApp, pilot) -> AskInteractionView:
    assert await app.project_engine_event(RunStarted("run")) is True
    assert (
        await app.project_engine_event(ToolPrepared("run", 1, ask_snapshot())) is True
    )
    assert not await app.project_engine_event(
        UserInputRequested("run", 2, "interaction", "Pick", ask_prompt())
    )
    await pilot.pause()
    return app.query_one(AskInteractionView)


async def mount_permission(app: MicaTuiApp, pilot) -> PermissionInteractionView:
    assert await app.project_engine_event(RunStarted("run")) is True
    assert (
        await app.project_engine_event(ToolPrepared("run", 1, bash_snapshot())) is True
    )
    assert not await app.project_engine_event(
        PermissionRequested("run", 2, "interaction", "call-bash", "bash", "allow?")
    )
    await pilot.pause()
    return app.query_one(PermissionInteractionView)


async def click_content_cell(
    pilot,
    composer: MicaComposer,
    cell_x: int,
    cell_y: int,
) -> None:
    """Click one rendered content cell of the composer.

    ``(0, 0)`` addresses the first cell of the first visible content row, so
    the assertions below talk about what the user actually sees.
    """
    content = composer.content_region
    await pilot.click(
        composer,
        offset=(
            content.x - composer.region.x + cell_x,
            content.y - composer.region.y + cell_y,
        ),
    )
    await pilot.pause()


def caret_cell(composer: MicaComposer) -> tuple[int, int]:
    """Visible caret position relative to the composer content area."""
    screen = composer.cursor_screen_offset
    content = composer.content_region
    return screen.x - content.x, screen.y - content.y


def test_ask_first_key_after_blur_selects_without_appfocus() -> None:
    async def scenario() -> None:
        answers: list[object] = []
        app = MicaTuiApp(
            user_input_responder=lambda response: answers.append(response) or True,
            run_cancel_requester=lambda run_id: False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = await mount_ask(app, pilot)
            app.post_message(AppBlur())
            await pilot.pause()
            assert app.focused is None
            assert app.app_focus is False

            await pilot.press("down")
            await pilot.pause()
            assert view.selected_option_index == 1
            assert app.app_focus is True

            await pilot.press("enter")
            await pilot.pause()
            assert len(answers) == 1
            response = answers[0]
            assert response.interaction_id == "interaction"
            assert response.answer == "no"
            assert response.selected_option_index == 1
            assert app.query_one(MicaComposer).text == ""

    asyncio.run(scenario())


def test_ask_reconciles_foreign_focus_to_the_active_owner() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(
            user_input_responder=lambda response: True,
            run_cancel_requester=lambda run_id: False,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = await mount_ask(app, pilot)
            transcript = app.query_one(ConversationTranscript)
            transcript.focus(scroll_visible=False)
            await pilot.pause()
            assert app.focused is transcript

            await pilot.press("down")
            await pilot.pause()
            assert view.selected_option_index == 1
            focused = app.focused
            assert focused is not None
            node = focused
            while node is not None and node is not view:
                node = node.parent
            assert node is view

    asyncio.run(scenario())


def test_permission_first_down_after_blur_reaches_no() -> None:
    async def scenario() -> None:
        responses: list[PermissionResponse] = []
        cancelled: list[str] = []
        app = MicaTuiApp(
            permission_responder=lambda response: responses.append(response) or True,
            run_cancel_requester=lambda run_id: cancelled.append(run_id) or True,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            view = await mount_permission(app, pilot)
            app.post_message(AppBlur())
            await pilot.pause()
            assert app.focused is None

            await pilot.press("down")
            await pilot.pause()
            assert view.selected_index == 1

            await pilot.press("enter")
            await pilot.pause()
            assert len(responses) == 1
            assert responses[0].interaction_id == "interaction"
            assert cancelled == []
            assert app.query_one(MicaComposer).text == ""

    asyncio.run(scenario())


def test_transcript_first_pageup_after_lost_focus_really_scrolls() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            for index in range(24):
                await transcript.mount(
                    UserMessageCard(MicaComposer.Submitted(f"line-{index}"))
                )
            await pilot.pause()
            transcript.focus(scroll_visible=False)
            transcript.scroll_end(animate=False, immediate=True)
            await pilot.pause()
            start = transcript.scroll_y
            assert start > 0

            app.screen.set_focus(None)
            await pilot.pause()
            await pilot.press("pageup")
            await pilot.pause()
            assert transcript.scroll_y < start
            assert transcript.is_following is False

            await pilot.press("pagedown", "end")
            await pilot.pause()
            assert transcript.scroll_y == transcript.max_scroll_y
            assert transcript.is_following is True
            assert transcript.has_pending_new_output is False

    asyncio.run(scenario())


def test_idle_page_keys_browse_transcript_with_composer_focused() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            for index in range(24):
                await transcript.mount(
                    UserMessageCard(MicaComposer.Submitted(f"line-{index}"))
                )
            await pilot.pause()
            transcript.scroll_end(animate=False, immediate=True)
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            assert app.focused is composer
            start = transcript.scroll_y
            assert start == transcript.max_scroll_y
            assert start > 0

            await pilot.press("pageup")
            await pilot.pause()
            assert transcript.scroll_y < start
            assert composer.text == ""

            await pilot.press("pagedown", "pagedown", "pagedown", "pagedown")
            await pilot.pause()
            assert transcript.scroll_y == transcript.max_scroll_y
            assert transcript.is_following is True

    asyncio.run(scenario())


def test_resize_reconciles_a_lost_input_owner() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            assert app.focused is composer
            app.screen.set_focus(None)
            await pilot.pause()
            assert app.focused is None

            await pilot.resize_terminal(90, 24)
            await pilot.pause()
            assert app.focused is composer

    asyncio.run(scenario())


def test_mouse_wheel_after_lost_focus_really_scrolls() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            for index in range(24):
                await transcript.mount(
                    UserMessageCard(MicaComposer.Submitted(f"line-{index}"))
                )
            await pilot.pause()
            transcript.focus(scroll_visible=False)
            transcript.scroll_end(animate=False, immediate=True)
            await pilot.pause()
            start = transcript.scroll_y
            assert start > 0
            app.screen.set_focus(None)
            await pilot.pause()

            await pilot.hover(transcript)
            region = transcript.region
            app.post_message(
                MouseScrollUp(
                    transcript,
                    x=region.x + 2,
                    y=region.y + 2,
                    delta_x=0,
                    delta_y=-3,
                    button=0,
                    shift=False,
                    meta=False,
                    ctrl=False,
                    screen_x=region.x + 2,
                    screen_y=region.y + 2,
                )
            )
            await pilot.pause()
            assert transcript.scroll_y < start
            assert app.focused is app.query_one(MicaComposer)

    asyncio.run(scenario())


def test_reconcile_keeps_transcript_owner_during_active_run() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run")) is True
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.focus(scroll_visible=False)
            await pilot.pause()
            app.screen.set_focus(None)
            await pilot.pause()

            await pilot.press("pageup")
            await pilot.pause()
            focused = app.focused
            assert focused is app.query_one(ConversationTranscript)

    asyncio.run(scenario())


def test_run_terminal_restores_composer_after_lost_focus() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.text = "keep draft"
            assert await app.project_engine_event(RunStarted("run")) is True
            assert (
                await app.project_engine_event(TextDelta("run", "partial", 1)) is True
            )
            app.screen.set_focus(None)
            await pilot.pause()
            assert app.focused is None

            await pilot.press("pageup")
            await pilot.pause()
            assert app.focused is app.query_one(ConversationTranscript)

            assert await app.project_engine_event(RunCompleted("run", 2)) is True
            await pilot.pause()
            assert app.focused is composer
            assert composer.text == "keep draft"

    asyncio.run(scenario())


def test_reconcile_is_safe_once_screen_is_unmounted() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
        # Outside the app session the screen no longer exists; a late input
        # event must remain a safe no-op.
        app._reconcile_input_owner()
        app.stop_runtime_input()
        app._reconcile_input_owner()

    asyncio.run(scenario())


def test_middle_insert_and_delete_target_the_visible_cursor() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.focus()
            for character in "abcde":
                await pilot.press(character)
            await pilot.pause()
            await pilot.press("left", "left")
            await pilot.press("X")
            await pilot.pause()
            assert composer.text == "abcXde"
            assert composer.cursor_location == (0, 4)

            await pilot.press("backspace")
            await pilot.pause()
            assert composer.text == "abcde"
            assert composer.cursor_location == (0, 3)
            await pilot.press("delete")
            await pilot.pause()
            assert composer.text == "abce"
            assert composer.cursor_location == (0, 3)

    asyncio.run(scenario())


def test_first_arrow_after_lost_focus_moves_composer_cursor() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.focus()
            composer.text = "abcde"
            composer.move_cursor((0, 5))
            await pilot.pause()
            app.screen.set_focus(None)
            await pilot.pause()
            assert composer.cursor_location == (0, 5)

            await pilot.press("left")
            await pilot.pause()
            assert composer.cursor_location == (0, 4)
            assert app.focused is composer

    asyncio.run(scenario())


def drawn_cursor_cell(composer: MicaComposer) -> tuple[int, int] | None:
    """Return the (row, cell) of the caret cell inside the rendered strips."""
    theme = composer._theme
    cursor_style = theme.cursor_style if theme else None
    if cursor_style is None:
        return None
    marker = str(cursor_style)
    for row in range(composer.size.height):
        strip = composer.render_line(row)
        offset = 0
        for segment in strip._segments:
            if segment.style is not None and str(segment.style) == marker:
                return row, offset
            offset += cell_len(segment.text)
    return None


def test_cursor_screen_offset_matches_the_drawn_cell() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.focus()
            composer.text = "abcde\nsecond"
            content_x = composer.content_region.x

            composer.move_cursor((0, 3))
            await pilot.pause()
            drawn = drawn_cursor_cell(composer)
            assert drawn is not None
            assert composer.cursor_screen_offset.x == content_x + drawn[1]
            assert drawn[1] == composer.gutter_width + 3

            composer.move_cursor((1, 3))
            await pilot.pause()
            drawn = drawn_cursor_cell(composer)
            assert drawn is not None
            assert drawn[0] == 1
            assert composer.cursor_screen_offset.x == content_x + drawn[1]
            assert drawn[1] == 3

    asyncio.run(scenario())


def test_composer_click_does_not_leak_unawaited_coroutine() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.focus()
            composer.text = "abcdef"
            await pilot.pause()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                await pilot.click(composer, offset=(4, 1))
                await pilot.pause()
                gc.collect()
            leaked = [
                item
                for item in caught
                if issubclass(item.category, RuntimeWarning)
                and "never awaited" in str(item.message)
            ]
            assert leaked == []

    asyncio.run(scenario())


def test_cursor_after_click_still_lands_on_the_visible_cell() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.focus()
            composer.text = "abcdef"
            composer.move_cursor((0, 0))
            await pilot.pause()
            await pilot.click(composer, offset=(7, 1))
            await pilot.pause()
            assert composer.cursor_location == (0, 3)

    asyncio.run(scenario())


def test_click_on_later_rows_maps_to_the_clicked_cell() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.focus()
            composer.text = "alpha\nbravo\ncharlie"
            composer.move_cursor((0, 0))
            await pilot.pause()
            assert composer.scroll_offset.y == 0

            # The first visible row draws the two-cell prompt.
            await click_content_cell(pilot, composer, 2, 0)
            assert composer.cursor_location == (0, 0)
            assert caret_cell(composer) == (2, 0)
            await click_content_cell(pilot, composer, 4, 0)
            assert composer.cursor_location == (0, 2)
            assert caret_cell(composer) == (4, 0)

            # Rows without a prompt must not reserve its two cells.
            await click_content_cell(pilot, composer, 0, 1)
            assert composer.cursor_location == (1, 0)
            assert caret_cell(composer) == (0, 1)
            await click_content_cell(pilot, composer, 3, 1)
            assert composer.cursor_location == (1, 3)
            assert caret_cell(composer) == (3, 1)
            await click_content_cell(pilot, composer, 2, 2)
            assert composer.cursor_location == (2, 2)
            assert caret_cell(composer) == (2, 2)

            # Typing lands exactly where the user clicked ...
            await pilot.press("X")
            await pilot.pause()
            assert composer.text == "alpha\nbravo\nchXarlie"

            # ... and Backspace/Delete act on that same position.
            await click_content_cell(pilot, composer, 3, 1)
            assert caret_cell(composer) == (3, 1)
            await pilot.press("backspace")
            await pilot.pause()
            assert composer.text == "alpha\nbrvo\nchXarlie"
            assert composer.cursor_location == (1, 2)
            await pilot.press("delete")
            await pilot.pause()
            assert composer.text == "alpha\nbro\nchXarlie"

    asyncio.run(scenario())


def test_click_on_soft_wrapped_second_visual_row() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.focus()
            text = "0123456789" * 12
            composer.text = text
            composer.move_cursor((0, 0))
            await pilot.pause()

            assert "\n" not in composer.text
            assert composer.scroll_offset.y == 0
            assert composer.virtual_size.height == 2
            wrap = composer.wrap_width
            assert composer.cursor_location == (0, 0)

            # The second visual row has no prompt, so its first cell is the
            # first character of the wrapped section.
            await click_content_cell(pilot, composer, 0, 1)
            assert composer.cursor_location == (0, wrap)
            assert caret_cell(composer) == (0, 1)

            await pilot.press("X")
            await pilot.pause()
            assert composer.text == f"{text[:wrap]}X{text[wrap:]}"

            # The prompt row still maps after its two cells.
            await click_content_cell(pilot, composer, 2, 0)
            assert composer.cursor_location == (0, 0)
            assert caret_cell(composer) == (2, 0)

    asyncio.run(scenario())


def test_click_maps_to_visible_rows_after_composer_scrolls() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.focus()
            composer.text = "\n".join(f"line-{index}" for index in range(12))
            composer.move_cursor((11, 7))
            await pilot.pause()

            hidden = int(composer.scroll_offset.y)
            assert hidden > 0

            await click_content_cell(pilot, composer, 2, 0)
            assert composer.cursor_location == (hidden, 0)
            assert caret_cell(composer) == (2, 0)

            await click_content_cell(pilot, composer, 4, 1)
            assert composer.cursor_location == (hidden + 1, 4)
            assert caret_cell(composer) == (4, 1)

            await pilot.press("X")
            await pilot.pause()
            lines = composer.text.splitlines()
            assert lines[hidden] == f"line-{hidden}"
            assert lines[hidden + 1] == f"lineX-{hidden + 1}"

    asyncio.run(scenario())


def test_click_maps_correctly_with_unicode_rows() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.focus()

            composer.text = "héllo wörld\nnée ünïcode"
            composer.move_cursor((0, 0))
            await pilot.pause()
            await click_content_cell(pilot, composer, 1, 1)
            assert composer.cursor_location == (1, 1)
            assert caret_cell(composer) == (1, 1)
            await pilot.press("X")
            await pilot.pause()
            assert composer.text == "héllo wörld\nnXée ünïcode"

            # Wide characters keep one column per character; the caret cell is
            # the first cell of that character, never a cell inside it.
            composer.text = "abc\n中文x"
            composer.move_cursor((0, 0))
            await pilot.pause()
            for cell_x, column, caret_x in (
                (0, 0, 0),
                (1, 0, 0),
                (2, 1, 2),
                (3, 1, 2),
                (4, 2, 4),
            ):
                await click_content_cell(pilot, composer, cell_x, 1)
                assert composer.cursor_location == (1, column)
                assert caret_cell(composer) == (caret_x, 1)

    asyncio.run(scenario())


def test_click_inside_paste_fragment_never_lands_inside_it() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            composer = app.query_one(MicaComposer)
            composer.focus()
            pasted = "\n".join(f"line {index}" for index in range(9))
            app.post_message(Paste(pasted))
            await pilot.pause()

            summary = composer.text
            assert summary == "[paste #1 +9 lines]"
            assert len(composer.display_fragments) == 1

            await click_content_cell(pilot, composer, 7, 0)
            index = composer.document.get_index_from_location(composer.cursor_location)
            assert index in (0, len(summary))
            assert caret_cell(composer) == (2, 0)

            # Editing around the click must keep the fragment atomic.
            await pilot.press("X")
            await pilot.pause()
            assert composer.text == f"X{summary}"
            assert len(composer.display_fragments) == 1
            assert composer.display_fragments[0].original == pasted

    asyncio.run(scenario())


def test_native_interrupt_and_key_share_one_state_machine() -> None:
    async def scenario() -> None:
        cancelled: list[str] = []
        clock = FakeClock()
        app = RecordingApp(
            clock=clock,
            run_cancel_requester=lambda run_id: cancelled.append(run_id) or True,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run")) is True
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert cancelled == ["run"]
            assert app.exits == []

            # The same physical press also reaching the console-signal seam
            # must not count as a second press (near-synchronous duplicate).
            clock.advance(0.05)
            app.handle_native_interrupt()
            await pilot.pause()
            assert cancelled == ["run"]
            assert app.exits == []

            clock.advance(0.5)
            app.handle_native_interrupt()
            await pilot.pause()
            assert cancelled == ["run"]
            assert app.exits == [0]

    asyncio.run(scenario())


def test_ctrl_c_near_synchronous_duplicates_never_exit() -> None:
    """一次物理按下的近同步重复（Key 与信号/重复分发）不计为第二次。"""

    async def scenario() -> None:
        clock = FakeClock()
        app = RecordingApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.exits == []

            clock.advance(0.05)
            await pilot.press("ctrl+c")
            clock.advance(0.05)
            app.handle_native_interrupt()
            await pilot.pause()
            assert app.is_running
            assert app.exits == []

            # A deliberate second press inside the confirmation window exits.
            clock.advance(0.5)
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.exits == [0]

    asyncio.run(scenario())


def test_ctrl_c_delayed_event_is_indistinguishable_from_a_new_press() -> None:
    """平台限制：去重窗口之后的同一物理按键重复与新的按下无法区分。

    Textual 8.2.8 的 Windows 控制台输入在转换时丢弃 key-up 记录、忽略
    ``wRepeatCount``，``Key`` 事件也不携带按下/重复/释放标记；因此持续按住
    Ctrl+C 产生的延迟自动重复在产品可观察层就是一次新的按下，可能被计为
    第二次按键。这里如实锁定该边界，真实终端行为由使用者手工复验。
    """

    async def scenario() -> None:
        clock = FakeClock()
        app = RecordingApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.exits == []

            clock.advance(0.20)
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.exits == [0]

    asyncio.run(scenario())


def test_ctrl_c_reset_after_confirmation_window_arms_again() -> None:
    async def scenario() -> None:
        clock = FakeClock()
        app = RecordingApp(clock=clock)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.exits == []

            # Past the confirmation window the next press is a first press again.
            clock.advance(2.1)
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.is_running
            assert app.exits == []

            clock.advance(0.5)
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.exits == [0]

    asyncio.run(scenario())


def test_native_interrupt_after_close_is_noop() -> None:
    app = MicaTuiApp()
    app.stop_runtime_input()
    app.handle_native_interrupt()
    app.action_confirm_exit()


# --------------------------------------------------------------------------- #
# 自动跟随：用户接管视口后，流式输出不得把视口拉回底部
# --------------------------------------------------------------------------- #
async def fill_transcript(
    app: MicaTuiApp, pilot, count: int = 24
) -> ConversationTranscript:
    """Mount `count` user cards and park the transcript at the bottom."""
    transcript = app.query_one(ConversationTranscript)
    for index in range(count):
        await transcript.mount(UserMessageCard(MicaComposer.Submitted(f"line-{index}")))
    await pilot.pause()
    transcript.scroll_end(animate=False, immediate=True)
    await pilot.pause()
    assert transcript.scroll_y == transcript.max_scroll_y
    assert transcript.is_following is True
    return transcript


def test_page_up_survives_content_added_before_the_scroll_watcher() -> None:
    """真实失败顺序：PageUp 之后、刷新之前立即追加可见输出。"""

    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            transcript = await fill_transcript(app, pilot)
            transcript.focus()
            await pilot.pause()

            await pilot.press("pageup")
            transcript.content_added()
            await pilot.pause()

            assert transcript.is_following is False
            assert transcript.scroll_y < transcript.max_scroll_y
            assert transcript.has_pending_new_output is True
            assert len(app.query(NewOutputIndicator)) == 1

    asyncio.run(scenario())


def test_page_up_intent_is_recorded_before_the_scroll_position_changes() -> None:
    """同一事件循环内的滚动 + 输出，不得依赖 scroll_y watcher 的时序。"""

    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            transcript = await fill_transcript(app, pilot)
            bottom = transcript.scroll_y

            transcript.scroll_page_up(animate=False)
            assert transcript.is_following is False
            assert transcript.scroll_y == bottom
            transcript.content_added()
            await pilot.pause()

            assert transcript.is_following is False
            assert transcript.scroll_y < transcript.max_scroll_y
            assert transcript.has_pending_new_output is True
            assert len(app.query(NewOutputIndicator)) == 1

    asyncio.run(scenario())


def test_stale_follow_callback_cannot_pull_back_the_viewport() -> None:
    """先排队的自动跟随回调在用户接管后必须自动失效。"""

    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            transcript = await fill_transcript(app, pilot)

            transcript.content_added()
            transcript.scroll_page_up(animate=False)
            await pilot.pause()

            assert transcript.is_following is False
            assert transcript.scroll_y < transcript.max_scroll_y
            assert transcript.has_pending_new_output is True
            assert len(app.query(NewOutputIndicator)) == 1

    asyncio.run(scenario())


def test_blur_focus_rounds_and_streaming_keep_the_detached_viewport() -> None:
    """多次失焦/聚焦与持续输出都不得把用户拉回底部。"""

    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            transcript = await fill_transcript(app, pilot)
            transcript.focus()
            await pilot.pause()
            await pilot.press("pageup")
            await pilot.pause()
            detached = transcript.scroll_y
            assert detached < transcript.max_scroll_y

            for _ in range(3):
                app.post_message(AppBlur())
                await pilot.pause()
                app.screen.set_focus(None)
                await pilot.pause()
                app.post_message(AppFocus())
                await pilot.pause()
                transcript.content_added()
                await pilot.pause()

            assert transcript.scroll_y == detached
            assert transcript.is_following is False
            assert transcript.has_pending_new_output is True
            assert len(app.query(NewOutputIndicator)) == 1

    asyncio.run(scenario())


def test_end_restores_auto_follow_after_detach() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            transcript = await fill_transcript(app, pilot)
            transcript.focus()
            await pilot.pause()

            await pilot.press("pageup")
            await pilot.pause()
            assert transcript.is_following is False

            await pilot.press("end")
            await pilot.pause()

            assert transcript.scroll_y == transcript.max_scroll_y
            assert transcript.is_following is True
            assert transcript.has_pending_new_output is False
            assert list(app.query(NewOutputIndicator)) == []

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 交互所有权：owner 来自 overlay 的活动子组件
# --------------------------------------------------------------------------- #
def test_permission_first_down_works_without_the_business_index() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(permission_responder=lambda _response: True)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            panel = await mount_permission(app, pilot)

            # 业务关联索引与焦点同时失效时，键盘所有权仍来自已挂载的活动面板。
            original_key = app._active_permission_key
            app._active_permission_key = None
            app.screen.set_focus(None)
            await pilot.pause()

            await pilot.press("down")
            await pilot.pause()
            assert panel.selected_index == 1
            assert panel.is_locked is False

            focused = app.focused
            node = focused
            while node is not None and node is not panel:
                node = node.parent
            assert node is panel

            # 响应是否被接受仍由业务索引把关，路由与业务接受是两个职责。
            app._active_permission_key = original_key
            await pilot.press("enter")
            await pilot.pause()
            assert panel.is_locked is True

    asyncio.run(scenario())


def test_permission_page_keys_scroll_the_shared_overlay() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(permission_responder=lambda _response: True)
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run")) is True
            assert (
                await app.project_engine_event(
                    ToolPrepared(
                        "run",
                        1,
                        ToolPresentationSnapshot(
                            "call-bash",
                            "bash",
                            ToolPresentationKind.BASH,
                            ToolPresentationStatus.PENDING,
                            {"command": "printf 'long-' " + "x" * 160},
                        ),
                    )
                )
                is True
            )
            assert not await app.project_engine_event(
                PermissionRequested(
                    "run", 2, "interaction", "call-bash", "bash", "allow?"
                )
            )
            await pilot.pause()
            panel = app.query_one(PermissionInteractionView)
            overlay = app.query_one(InteractionOverlay)
            transcript = app.query_one(ConversationTranscript)

            # 单一滚动容器：面板自己不再滚动，页键一律滚动 overlay。
            assert panel.max_scroll_y == 0
            assert overlay.max_scroll_y > 0
            transcript_before = transcript.scroll_y
            await pilot.press("pagedown")
            await pilot.pause()
            assert overlay.scroll_y > 0
            assert panel.scroll_y == 0
            assert transcript.scroll_y == transcript_before
            await pilot.press("pageup")
            await pilot.pause()
            assert overlay.scroll_y == 0

    asyncio.run(scenario())
