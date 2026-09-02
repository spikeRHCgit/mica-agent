"""P6-02A1 Composer 输入与提交契约测试。"""

from __future__ import annotations

import asyncio
import builtins
import pathlib
import subprocess
import sys

import pytest
from textual.containers import VerticalScroll
from textual.widgets import TextArea

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
    assert MicaComposer.Submitted.__slots__ == ("_text",)


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
