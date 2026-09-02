"""P6-02A1 Composer 输入与提交契约测试。"""

from __future__ import annotations

import asyncio
import builtins
import pathlib
import subprocess
import sys

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Static, TextArea

from mica_agent.tui import MicaComposer, MicaTuiApp


class RecordingTuiApp(MicaTuiApp):
    """仅在测试中观察 Composer 的公开提交消息。"""

    def __init__(self, *, ansi_color: bool | None = None) -> None:
        super().__init__(ansi_color=ansi_color)
        self.submissions: list[MicaComposer.Submitted] = []

    def on_mica_composer_submitted(
        self,
        message: MicaComposer.Submitted,
    ) -> None:
        self.submissions.append(message)


def test_composer_accepts_text_and_enter_submits_once() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            assert isinstance(composer, TextArea)
            assert composer.has_focus
            await pilot.press("h", "i", "space", "你")
            assert composer.text == "hi 你"

            await pilot.press("enter")
            assert [message.text for message in app.submissions] == ["hi 你"]
            assert composer.text == ""
            assert composer.has_focus

    asyncio.run(scenario())


def test_submit_preserves_original_text_and_message_snapshot() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            original = "  cafe\u0301 世界  "
            composer.load_text(original)

            await pilot.press("enter")
            submitted = app.submissions[0]
            assert submitted.text == original
            assert composer.text == ""

            await pilot.press("n", "e", "x", "t")
            assert submitted.text == original
            with pytest.raises(AttributeError):
                submitted.text = "changed"  # type: ignore[misc]

    asyncio.run(scenario())


def test_real_newline_grows_composer_to_two_content_lines() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            assert composer.content_region.height == 1
            assert composer.region.height == 3
            await pilot.press("a", "shift+enter", "b")

            assert composer.text == "a\nb"
            assert composer.content_region.height == 2
            assert composer.region.height == 4

    asyncio.run(scenario())


def test_eight_content_lines_cap_height_and_scroll_inside() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            await pilot.press(
                *[key for _ in range(7) for key in ("x", "shift+enter")],
                "x",
            )
            await pilot.pause()
            assert composer.text == "\n".join("x" for _ in range(8))
            assert composer.content_region.height == 8
            assert composer.region.height == 10
            assert composer.virtual_size.height == 8
            assert composer.max_scroll_y == 0

            await pilot.press("shift+enter", "x")
            await pilot.pause()
            assert composer.text == "\n".join("x" for _ in range(9))
            assert composer.content_region.height == 8
            assert composer.region.height == 10
            assert composer.virtual_size.height == 9
            assert composer.max_scroll_y > 0

    asyncio.run(scenario())


def test_shift_enter_inserts_real_newline_without_submitting() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            await pilot.press("a", "shift+enter", "b")
            assert composer.text == "a\nb"
            assert app.submissions == []

            await pilot.press("enter")
            assert [message.text for message in app.submissions] == ["a\nb"]

    asyncio.run(scenario())


def test_shift_enter_replaces_selection_at_cursor() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            composer.load_text("beforeafter")
            composer.cursor_location = (0, 6)

            await pilot.press("shift+enter")
            assert composer.text == "before\nafter"
            assert composer.cursor_location == (1, 0)
            assert app.submissions == []

    asyncio.run(scenario())


def test_resize_narrows_grows_and_widens_shrinks() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            text = "abcdefghijklmnopqrstuvwxyz0123456789"
            composer.load_text(text)
            await pilot.pause()

            assert composer.document.line_count == 1
            assert composer.content_region.height == 1
            assert composer.region.height == 3

            await pilot.resize_terminal(24, 20)
            await pilot.pause()
            assert composer.text == text
            assert "\n" not in composer.text
            assert composer.content_region.height > 1
            assert composer.region.height == composer.content_region.height + 2
            assert app.query_one("#composer", MicaComposer) is composer

            await pilot.resize_terminal(100, 20)
            await pilot.pause()
            assert composer.text == text
            assert composer.content_region.height == 1
            assert composer.region.height == 3
            assert app.query_one("#composer", MicaComposer) is composer

    asyncio.run(scenario())


def test_delete_submit_and_clear_shrink_composer() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            await pilot.press("x", "shift+enter", "x", "shift+enter", "x")
            await pilot.pause()
            assert composer.content_region.height == 3
            assert composer.region.height == 5

            await pilot.press("backspace", "backspace")
            await pilot.pause()
            assert composer.text == "x\nx"
            assert composer.content_region.height == 2
            assert composer.region.height == 4

            composer.clear()
            await pilot.pause()
            assert composer.text == ""
            assert composer.content_region.height == 1
            assert composer.region.height == 3
            assert composer.scroll_y == 0

            await pilot.press("x", "shift+enter", "x")
            await pilot.press("enter")
            await pilot.pause()
            assert composer.text == ""
            assert app.submissions[0].text == "x\nx"
            assert composer.content_region.height == 1
            assert composer.region.height == 3

    asyncio.run(scenario())


def test_soft_wrapped_unicode_grows_without_changing_raw_text() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(24, 20)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            text = "你好世界 cafe\u0301 你好世界 cafe\u0301"

            composer.load_text(text)
            await pilot.pause()

            assert composer.document.line_count == 1
            assert composer.virtual_size.height > 1
            assert composer.content_region.height == composer.virtual_size.height
            assert composer.region.height == composer.content_region.height + 2
            assert composer.text == text
            assert "\n" not in composer.text

    asyncio.run(scenario())


def test_long_line_soft_wraps_without_changing_raw_text() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(24, 8)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            text = "abcdefghijklmnopqrstuvwxyz0123456789"
            await pilot.press(*text)

            assert composer.soft_wrap is True
            assert composer.document.line_count == 1
            assert composer.virtual_size.height > 1
            assert composer.text == text
            assert "\n" not in composer.text
            assert composer.region.bottom == 8

    asyncio.run(scenario())


def test_whitespace_is_cleared_without_submission_or_replay() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            await pilot.press("space", "shift+enter", "space", "enter")
            assert composer.text == ""
            assert app.submissions == []

            await pilot.press("o", "n", "c", "e", "enter", "enter")
            assert [message.text for message in app.submissions] == ["once"]
            assert composer.text == ""

    asyncio.run(scenario())


def test_cursor_stays_visible_when_filling_at_the_end() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            for index in range(14):
                await pilot.press("x")
                if index < 13:
                    await pilot.press("shift+enter")
            await pilot.pause()

            assert composer.virtual_size.height >= 14
            assert composer.content_region.height <= 8
            assert composer.scroll_y > 0
            cursor_line, _cursor_column = composer.cursor_location
            visible_top = int(composer.scroll_y)
            visible_bottom = visible_top + composer.content_region.height - 1
            assert visible_top <= cursor_line <= visible_bottom

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(24, 8), (100, 8)])
def test_short_terminal_caps_composer_and_keeps_transcript_visible(
    size: tuple[int, int],
) -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            transcript = app.query_one("#transcript", VerticalScroll)
            work_status = app.query_one("#work-status", Static)

            await pilot.press(
                *[key for _ in range(12) for key in ("x", "shift+enter")],
                "x",
            )
            await pilot.pause()
            assert composer.region.bottom == size[1]
            assert work_status.region.y >= transcript.region.bottom
            assert transcript.region.height >= 1
            assert transcript.region.height < composer.region.height
            assert composer.content_region.height >= 1
            assert composer.content_region.height <= 8
            assert composer.max_scroll_y > 0
            assert len(composer.text.split("\n")) == 13

    asyncio.run(scenario())


def test_repeated_pause_keeps_height_stable_without_oscillation() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            await pilot.press("x", "shift+enter", "x", "shift+enter", "x")
            await pilot.pause()
            expected_content = composer.content_region.height
            expected_region = composer.region.height
            for _ in range(5):
                await pilot.pause()
                assert composer.content_region.height == expected_content
                assert composer.region.height == expected_region

    asyncio.run(scenario())


def test_over_eight_lines_scrolls_internally_without_losing_text() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            lines = [f"line {index}" for index in range(14)]

            for index, line in enumerate(lines):
                await pilot.press(*line)
                if index < len(lines) - 1:
                    await pilot.press("shift+enter")
            await pilot.pause()

            expected = "\n".join(lines)
            assert composer.text == expected
            assert composer.virtual_size.height >= len(lines)
            assert composer.content_region.height <= 8
            assert composer.max_scroll_y > 0
            assert composer.scroll_y <= composer.max_scroll_y

            await pilot.press("backspace")
            await pilot.pause()
            assert composer.text.count("\n") == len(lines) - 1
            assert "line 0" in composer.text
            assert "line 13" in composer.text or "line 1" in composer.text
            assert composer.scroll_y <= composer.max_scroll_y

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(24, 8), (40, 16), (100, 30), (100, 8)])
def test_composer_stays_at_bottom_without_changing_screen_structure(
    size: tuple[int, int],
) -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            transcript = app.query_one("#transcript", VerticalScroll)
            composer = app.query_one("#composer", MicaComposer)

            assert [child.id for child in app.screen.children] == [
                "transcript",
                "work-status",
                "composer",
            ]
            assert [child.id for child in transcript.children] == [
                "mica-logo",
                "startup-resources",
            ]
            assert composer.parent is app.screen
            assert composer.region.height == 3
            assert composer.region.bottom == size[1]

    asyncio.run(scenario())


def test_resize_keeps_cursor_text_and_position() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 16)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)

            await pilot.press("a", "b", "c", "shift+enter", "d")
            await pilot.pause()
            assert companion_cursor_is_valid(composer)

            await pilot.resize_terminal(24, 12)
            await pilot.pause()
            assert app.query_one("#composer", MicaComposer) is composer
            assert composer.text == "abc\nd"
            assert composer.has_focus
            assert companion_cursor_is_valid(composer)

            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            assert app.query_one("#composer", MicaComposer) is composer
            assert composer.text == "abc\nd"
            assert composer.has_focus
            assert companion_cursor_is_valid(composer)

    asyncio.run(scenario())


def companion_cursor_is_valid(composer: MicaComposer) -> bool:
    """辅助函数：光标落在文档合法范围。"""
    row, _column = composer.cursor_location
    return 0 <= row <= composer.document.line_count - 1


def test_resize_keeps_one_focused_composer_instance() -> None:
    async def scenario() -> None:
        app = RecordingTuiApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            await pilot.press("k", "e", "e", "p")

            for width, height in ((40, 16), (24, 8), (100, 8)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                assert app.query_one("#composer", MicaComposer) is composer
                assert len(app.query("#composer")) == 1
                assert composer.region.bottom == height
                assert composer.text == "keep"
                assert composer.has_focus

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("no_color", "ansi_color"),
    [(True, None), (False, True)],
)
def test_composer_submits_with_terminal_color_degradation(
    monkeypatch: pytest.MonkeyPatch,
    no_color: bool,
    ansi_color: bool | None,
) -> None:
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    async def scenario() -> None:
        app = RecordingTuiApp(ansi_color=ansi_color)
        async with app.run_test(size=(32, 10)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", MicaComposer)
            await pilot.press("可", "编", "辑", "enter")

            if no_color:
                assert app.no_color is True
            else:
                assert app.ansi_color is True
                assert app.native_ansi_color is True
            assert [message.text for message in app.submissions] == ["可编辑"]
            assert composer.has_focus

    asyncio.run(scenario())


def test_submitted_repr_does_not_disclose_text() -> None:
    sentinel = "sk-p6-02a1-sensitive-sentinel"
    message = MicaComposer.Submitted(sentinel)

    assert message.text == sentinel
    assert sentinel not in repr(message)
    assert "Traceback" not in repr(message)
    assert set(MicaComposer.Submitted.__slots__) == {"_text", "_display_text"}


def test_tui_explicit_import_exposes_composer_without_top_level_export() -> None:
    code = """
import sys
import mica_agent
assert "mica_agent.tui" not in sys.modules
assert not hasattr(mica_agent, "MicaComposer")
assert "MicaComposer" not in mica_agent.__all__
from mica_agent.tui import MicaComposer
assert issubclass(MicaComposer, __import__("textual.widgets").widgets.TextArea)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_composer_mount_has_no_file_or_runtime_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked(*_: object, **__: object) -> None:
        raise AssertionError("prohibited composer side effect")

    monkeypatch.setattr(builtins, "open", blocked)
    monkeypatch.setattr(pathlib.Path, "open", blocked)
    monkeypatch.setattr(pathlib.Path, "home", blocked)
    monkeypatch.setattr(pathlib.Path, "cwd", blocked)
    monkeypatch.setattr(pathlib.Path, "mkdir", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)

    async def scenario() -> None:
        current = asyncio.current_task()
        app = RecordingTuiApp()
        async with app.run_test(size=(40, 12)) as pilot:
            await pilot.pause()
            assert app.query_one("#composer", MicaComposer).has_focus
            await pilot.press("s", "a", "f", "e", "enter")
            assert [message.text for message in app.submissions] == ["safe"]
        assert [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ] == []

    asyncio.run(scenario())
