"""P6-03A+B 模型正式回复与思考内容的公共 TUI seam 测试。"""

from __future__ import annotations

import asyncio

import pytest
from textual.events import Resize
from textual.geometry import Region
from textual.widgets import Markdown

from mica_agent.events import (
    ErrorCategory,
    ReasoningDelta,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
    ToolStarted,
)
from mica_agent.tui import (
    AssistantResponseView,
    MicaTuiApp,
    ReasoningDisplayMode,
    ReasoningView,
)
from mica_agent.tui.transcript import UserMessageCard


def test_run_started_creates_no_empty_output_components() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()

            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert list(app.query(AssistantResponseView)) == []
            assert list(app.query(ReasoningView)) == []
            assert [
                child.id
                for child in app.query_one("#transcript").children
                if child.id != "new-output"
            ] == [
                "mica-logo",
                "startup-intro",
                "startup-resources",
                "startup-ready",
            ]

    asyncio.run(scenario())


def test_first_text_delta_mounts_one_streaming_markdown_response() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True

            text = "hello"
            assert await app.project_engine_event(TextDelta("run-1", text, 1)) is True
            await pilot.pause()

            responses = list(app.query(AssistantResponseView))
            assert len(responses) == 1
            view = responses[0]
            assert view.text == text
            assert view.is_streaming is True
            markdown = view.query_one(Markdown)
            assert markdown.source == text
            assert len(view.query(".streaming-cursor")) == 1
            assert view.parent is app.query_one("#transcript")

    asyncio.run(scenario())


def test_text_deltas_append_in_order_without_replacing_response_view() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert (
                await app.project_engine_event(TextDelta("run-1", "标题\n\n", 1))
                is True
            )
            first = app.query(AssistantResponseView).first()

            suffix = "  *保留空格*\n```python\nprint('😀')\n```"
            assert await app.project_engine_event(TextDelta("run-1", suffix, 2)) is True
            await pilot.pause()

            second = app.query(AssistantResponseView).first()
            assert second is first
            assert second.text == "标题\n\n" + suffix
            assert second.markdown.source == second.text
            assert second.is_streaming is True
            assert len(app.query(AssistantResponseView)) == 1

    asyncio.run(scenario())


def test_duplicate_reverse_and_unknown_events_are_rejected() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert await app.project_engine_event(TextDelta("run-1", "kept", 1)) is True
            view = app.query(AssistantResponseView).first()

            assert (
                await app.project_engine_event(TextDelta("run-1", "duplicate", 1))
                is False
            )
            assert (
                await app.project_engine_event(TextDelta("run-1", "reverse", 0))
                is False
            )
            assert (
                await app.project_engine_event(TextDelta("unknown", "ignored", 1))
                is False
            )
            await pilot.pause()

            assert view.text == "kept"
            assert app.query(AssistantResponseView).first() is view
            assert len(app.query(AssistantResponseView)) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "terminal_event",
    [
        RunCompleted("run-1", 2),
        RunFailed("run-1", "safe failure", ErrorCategory.UNKNOWN, 2),
        RunCancelled("run-1", 2),
    ],
)
def test_all_run_terminal_events_stop_cursor_and_reject_late_output(
    terminal_event: RunCompleted | RunFailed | RunCancelled,
) -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert (
                await app.project_engine_event(TextDelta("run-1", "partial", 1)) is True
            )
            view = app.query(AssistantResponseView).first()

            assert await app.project_engine_event(terminal_event) is True
            await pilot.pause()
            assert view.is_streaming is False
            assert len(view.query(".streaming-cursor")) == 0
            assert view.text == "partial"
            assert view.markdown.source == view.text
            assert (
                await app.project_engine_event(TextDelta("run-1", "late", 3)) is False
            )
            assert await app.project_engine_event(terminal_event) is False
            assert view.text == "partial"

    asyncio.run(scenario())


def test_first_reasoning_delta_mounts_an_independent_collapsed_view() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True

            sentinel = "先思考，再回答"
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", sentinel, 1))
                is True
            )
            await pilot.pause()

            views = list(app.query(ReasoningView))
            assert len(views) == 1
            view = views[0]
            assert view.text == sentinel
            assert view.mode is ReasoningDisplayMode.COLLAPSED
            assert view.expanded is False
            assert list(app.query(AssistantResponseView)) == []
            assert sentinel in str(view.render())
            assert view.parent is app.query_one("#transcript")

    asyncio.run(scenario())


def test_reasoning_and_response_are_separate_and_follow_event_order() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", "think", 1))
                is True
            )
            assert (
                await app.project_engine_event(TextDelta("run-1", "answer", 2)) is True
            )
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", " more", 3))
                is True
            )
            assert await app.project_engine_event(TextDelta("run-1", "!", 4)) is True
            await pilot.pause()

            transcript = app.query_one("#transcript")
            output = [
                child
                for child in transcript.children
                if isinstance(child, (ReasoningView, AssistantResponseView))
            ]
            assert output == [
                app.query(ReasoningView).first(),
                app.query(AssistantResponseView).first(),
            ]
            reasoning = app.query(ReasoningView).first()
            response = app.query(AssistantResponseView).first()
            assert reasoning.text == "think more"
            assert response.text == "answer!"
            assert "think" not in response.text
            assert "answer" not in reasoning.text

    asyncio.run(scenario())


def test_full_reasoning_mode_keeps_complete_source_visible() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(reasoning_display_mode=ReasoningDisplayMode.FULL)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            text = "完整思考 " + ("x" * 120) + "\n尾部"
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", text, 1)) is True
            )
            await pilot.pause()

            view = app.query(ReasoningView).first()
            assert view.mode is ReasoningDisplayMode.FULL
            assert view.expanded is True
            assert view.text == text
            assert str(view.render()) == text
            assert view.region.height > 2

            assert (
                await app.project_engine_event(ReasoningDelta("run-1", " +", 2)) is True
            )
            assert app.query(ReasoningView).first() is view
            assert view.text == text + " +"

    asyncio.run(scenario())


def test_hidden_reasoning_accepts_source_without_visible_placeholder() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(reasoning_display_mode=ReasoningDisplayMode.HIDDEN)
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            hidden = "hidden reasoning sentinel"
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", hidden, 1))
                is True
            )
            await pilot.pause()

            assert list(app.query(ReasoningView)) == []
            assert hidden not in " ".join(
                str(child.render()) for child in app.query_one("#transcript").children
            )
            assert (
                await app.project_engine_event(TextDelta("run-1", "answer", 2)) is True
            )
            await pilot.pause()
            assert len(app.query(AssistantResponseView)) == 1
            assert app.query(AssistantResponseView).first().text == "answer"
            assert list(app.query(ReasoningView)) == []

    asyncio.run(scenario())


def test_collapsed_reasoning_preview_is_limited_and_ctrl_o_expands_current_view() -> (
    None
):
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            text = "折叠预览 " + ("x" * 140)
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", text, 1)) is True
            )
            await pilot.pause()

            view = app.query(ReasoningView).first()
            preview = str(view.render())
            assert view.mode is ReasoningDisplayMode.COLLAPSED
            assert view.expanded is False
            assert view.text == text
            assert preview.startswith("折叠预览")
            assert preview.endswith("…")
            assert len(preview) <= ReasoningView.PREVIEW_CHAR_LIMIT + 2
            assert view.region.height <= 2

            view.focus()
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert view.expanded is True
            assert str(view.render()) == text
            assert view.region.height > 2

            await pilot.press("ctrl+o")
            await pilot.pause()
            assert view.expanded is False
            assert str(view.render()).endswith("…")
            assert app.query(ReasoningView).first() is view

    asyncio.run(scenario())


def test_ctrl_o_changes_only_the_focused_reasoning_view() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert (
                await app.project_engine_event(
                    ReasoningDelta("run-1", "first " + "a" * 120, 1)
                )
                is True
            )
            assert await app.project_engine_event(RunStarted("run-2")) is True
            assert (
                await app.project_engine_event(
                    ReasoningDelta("run-2", "second " + "b" * 120, 1)
                )
                is True
            )
            await pilot.pause()

            first, second = list(app.query(ReasoningView))
            first.focus()
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert first.expanded is True
            assert second.expanded is False

            second.focus()
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert first.expanded is True
            assert second.expanded is True
            await pilot.press("ctrl+o")
            assert second.expanded is False

    asyncio.run(scenario())


def test_completed_response_keeps_raw_markdown_source_and_removes_cursor() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            source = "# Title\n\n- item\n\n[link](https://example.test)\n\n```python\nprint('x')\n```"
            assert await app.project_engine_event(TextDelta("run-1", source, 1)) is True
            view = app.query(AssistantResponseView).first()
            assert len(view.query(".streaming-cursor")) == 1

            assert await app.project_engine_event(RunCompleted("run-1", 2)) is True
            await pilot.pause()
            assert view.text == source
            assert view.markdown.source == source
            assert view.is_streaming is False
            assert len(view.query(".streaming-cursor")) == 0
            assert len(app.query(Markdown)) == 1

    asyncio.run(scenario())


def test_delayed_cursor_positioning_is_noop_after_cursor_removed() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            source = "completed answer"
            assert await app.project_engine_event(TextDelta("run-1", source, 1)) is True
            await pilot.pause()

            view = app.query(AssistantResponseView).first()
            assert view.is_streaming is True
            assert len(view.query(".streaming-cursor")) == 1

            view.on_resize(Resize(view.size, view.size))
            assert await app.project_engine_event(RunCompleted("run-1", 2)) is True
            assert view.is_streaming is False
            assert len(view.query(".streaming-cursor")) == 0

            await pilot.pause()
            await view.finish(stable_markdown=True)
            assert view.text == source
            assert view.markdown.source == source
            assert view.is_streaming is False
            assert len(view.query(".streaming-cursor")) == 0
            assert len(app.query(Markdown)) == 1

    asyncio.run(scenario())


def test_no_color_keeps_reasoning_indented_and_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    async def scenario() -> None:
        app = MicaTuiApp(ansi_color=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", "thought", 1))
                is True
            )
            assert (
                await app.project_engine_event(TextDelta("run-1", "answer", 2)) is True
            )
            await pilot.pause()

            reasoning = app.query(ReasoningView).first()
            response = app.query(AssistantResponseView).first()
            assert app.no_color is True
            assert reasoning.content_region.x > response.content_region.x
            assert "thought" in str(reasoning.render())
            assert "answer" in response.markdown.source

    asyncio.run(scenario())


def test_resize_does_not_replace_output_views_or_duplicate_components() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", "thought", 1))
                is True
            )
            assert (
                await app.project_engine_event(TextDelta("run-1", "answer", 2)) is True
            )
            await pilot.pause()
            reasoning = app.query(ReasoningView).first()
            response = app.query(AssistantResponseView).first()
            cursor = response.query_one(".streaming-cursor")

            for size in ((40, 16), (24, 8), (100, 30)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                assert app.query(ReasoningView).first() is reasoning
                assert app.query(AssistantResponseView).first() is response
                assert len(app.query(ReasoningView)) == 1
                assert len(app.query(AssistantResponseView)) == 1
                assert response.query(".streaming-cursor").first() is cursor
                assert reasoning.text == "thought"
                assert response.text == "answer"

    asyncio.run(scenario())


def test_output_view_repr_never_contains_sensitive_body_or_traceback() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(reasoning_display_mode=ReasoningDisplayMode.FULL)
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            sentinel = "p6-sensitive-model-body"
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", sentinel, 1))
                is True
            )
            assert (
                await app.project_engine_event(TextDelta("run-1", sentinel, 2)) is True
            )
            await pilot.pause()

            for view in (
                app.query(ReasoningView).first(),
                app.query(AssistantResponseView).first(),
            ):
                representation = repr(view)
                assert sentinel not in representation
                assert "Traceback" not in representation

    asyncio.run(scenario())


def test_unsupported_tool_seq_is_consumed_without_placeholder_or_blocking_later_output() -> (
    None
):
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert (
                await app.project_engine_event(
                    ToolStarted("run-1", 1, "call-1", "read")
                )
                is False
            )
            assert (
                await app.project_engine_event(TextDelta("run-1", "same seq", 1))
                is False
            )
            assert (
                await app.project_engine_event(TextDelta("run-1", "visible", 2)) is True
            )
            await pilot.pause()

            assert list(app.query(ReasoningView)) == []
            assert len(app.query(AssistantResponseView)) == 1
            assert app.query(AssistantResponseView).first().text == "visible"
            assert "call-1" not in " ".join(
                str(child.render()) for child in app.query_one("#transcript").children
            )

    asyncio.run(scenario())


def test_multiple_runs_keep_objects_isolated_and_preserve_dom_event_order() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            await pilot.press("o", "n", "e", "enter")
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", "r1", 1)) is True
            )
            assert await app.project_engine_event(TextDelta("run-1", "a1", 2)) is True
            first_reasoning = app.query(ReasoningView).first()
            first_response = app.query(AssistantResponseView).first()
            assert await app.project_engine_event(RunCompleted("run-1", 3)) is True

            await pilot.press("t", "w", "o", "enter")
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-2")) is True
            assert await app.project_engine_event(TextDelta("run-2", "a2", 1)) is True
            assert (
                await app.project_engine_event(ReasoningDelta("run-2", "r2", 2)) is True
            )
            second_response = list(app.query(AssistantResponseView))[1]
            second_reasoning = list(app.query(ReasoningView))[1]
            assert await app.project_engine_event(TextDelta("run-2", "!", 3)) is True
            assert await app.project_engine_event(RunCancelled("run-2", 4)) is True
            await pilot.pause()

            assert first_response.text == "a1"
            assert first_response.is_streaming is False
            assert first_reasoning.text == "r1"
            assert second_response.text == "a2!"
            assert second_response.is_streaming is False
            assert second_reasoning.text == "r2"
            assert app.query(AssistantResponseView).first() is first_response
            assert app.query(ReasoningView).first() is first_reasoning
            transcript = app.query_one("#transcript")
            visible = [
                child
                for child in transcript.children
                if isinstance(
                    child,
                    (UserMessageCard, ReasoningView, AssistantResponseView),
                )
            ]
            assert [type(child) for child in visible] == [
                UserMessageCard,
                ReasoningView,
                AssistantResponseView,
                UserMessageCard,
                AssistantResponseView,
                ReasoningView,
            ]
            assert [
                card.submitted.display_text for card in app.query(UserMessageCard)
            ] == [
                "one",
                "two",
            ]

    asyncio.run(scenario())


def test_run_events_before_start_and_reused_run_ids_are_rejected() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(TextDelta("run-1", "no", 1)) is False
            assert await app.project_engine_event(RunCompleted("run-1", 1)) is False
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert await app.project_engine_event(RunStarted("run-1")) is False
            assert await app.project_engine_event(RunCancelled("run-1", 1)) is True
            assert await app.project_engine_event(RunStarted("run-1")) is False
            assert list(app.query(AssistantResponseView)) == []
            assert list(app.query(ReasoningView)) == []

    asyncio.run(scenario())


def test_terminal_run_without_deltas_does_not_mount_blank_views() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert await app.project_engine_event(RunCompleted("run-1", 1)) is True
            await pilot.pause()
            assert list(app.query(AssistantResponseView)) == []
            assert list(app.query(ReasoningView)) == []
            assert (
                await app.project_engine_event(TextDelta("run-1", "late", 2)) is False
            )

    asyncio.run(scenario())


def test_streaming_cursor_does_not_create_a_separate_vertical_row() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            assert (
                await app.project_engine_event(TextDelta("run-1", "hello", 1)) is True
            )
            await pilot.pause()

            view = app.query_one(AssistantResponseView)
            markdown = view.markdown
            cursor = view.query_one(".streaming-cursor")
            content = markdown.query("*").first()
            assert content is not None
            assert "hello" in str(content.render())
            assert cursor.region.y == content.region.y
            assert cursor.region.x == content.region.x + len("hello")
            assert cursor.region.bottom <= markdown.region.bottom
            assert view.region.height == markdown.region.height

    asyncio.run(scenario())


def test_streaming_cursor_stays_in_the_end_region_of_multiline_markdown() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            source = "# Title\n\n- first\n- second\n\n```python\nprint('x')\n```"
            assert await app.project_engine_event(TextDelta("run-1", source, 1)) is True
            await pilot.pause()

            view = app.query_one(AssistantResponseView)
            markdown = view.markdown
            cursor = view.query_one(".streaming-cursor")
            code_label = markdown.query("Label").first()
            assert code_label is not None
            assert "print('x')" in str(code_label.render())
            assert cursor.region.y >= code_label.region.y
            assert cursor.region.y < code_label.region.bottom
            assert cursor.region.bottom <= markdown.region.bottom
            assert cursor.region.x > code_label.region.x
            assert view.region.height == markdown.region.height

    asyncio.run(scenario())


def test_narrow_collapsed_reasoning_keeps_ellipsis_in_last_visible_line() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(24, 8)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            text = "think\n" + ("x" * 200)
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", text, 1)) is True
            )
            await pilot.pause()

            view = app.query_one(ReasoningView)
            lines = view.render_lines(
                Region(0, 0, view.region.width, view.region.height)
            )
            assert len(lines) <= 2
            assert "…" in lines[-1].text.rstrip()

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (40, 16), (24, 8)])
def test_collapsed_reasoning_that_fits_two_rendered_lines_has_no_ellipsis(
    size: tuple[int, int],
) -> None:
    async def scenario() -> None:
        app = MicaTuiApp(ansi_color=False)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            source = "中文🙂\n😀abc"
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", source, 1))
                is True
            )
            await pilot.pause()
            await pilot.pause()

            view = app.query_one(ReasoningView)
            lines = view.render_lines(
                Region(0, 0, view.region.width, view.region.height)
            )
            assert len(lines) <= 2
            assert all("…" not in line.text for line in lines)
            assert str(view.render()) == source
            assert view.text == source

    asyncio.run(scenario())


def test_collapsed_reasoning_preview_tracks_real_width_across_resizes() -> None:
    async def scenario() -> None:
        app = MicaTuiApp(ansi_color=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert await app.project_engine_event(RunStarted("run-1")) is True
            source = "思考🙂中文😀 " * 80
            assert (
                await app.project_engine_event(ReasoningDelta("run-1", source, 1))
                is True
            )
            await pilot.pause()
            await pilot.pause()

            view = app.query_one(ReasoningView)
            original_text = view.text
            original_identity = id(view)
            observed_widths: list[int] = []
            observed_cells: list[int] = []
            for size in ((80, 24), (40, 16), (24, 8)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                await pilot.pause()
                lines = view.render_lines(
                    Region(0, 0, view.region.width, view.region.height)
                )
                assert len(lines) <= 2
                assert "…" in lines[-1].text.rstrip()
                observed_widths.append(view.content_region.width)
                observed_cells.append(sum(line.cell_length for line in lines))
                assert id(view) == original_identity
                assert view.text == original_text

            assert observed_widths == sorted(observed_widths, reverse=True)
            assert observed_cells == sorted(observed_cells, reverse=True)

            view.focus()
            await pilot.press("ctrl+o")
            await pilot.pause()
            assert view.expanded is True
            assert str(view.render()) == source
            assert view.region.height > 2

            await pilot.press("ctrl+o")
            await pilot.pause()
            lines = view.render_lines(
                Region(0, 0, view.region.width, view.region.height)
            )
            assert view.expanded is False
            assert "…" in lines[-1].text.rstrip()
            assert view.text == original_text

    asyncio.run(scenario())
