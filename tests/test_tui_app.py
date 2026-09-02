"""P6-01B Textual 外壳与主题测试。"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Static

from mica_agent.tui import MicaTuiApp
from mica_agent.tui.theme import MICA_THEME


def test_mica_tui_app_is_a_textual_app_with_semantic_dark_theme() -> None:
    assert issubclass(MicaTuiApp, App)
    assert MICA_THEME.dark is True
    assert MICA_THEME.primary == "#3b82f6"
    assert MICA_THEME.secondary == "#1d4ed8"
    assert MICA_THEME.accent == "#7dd3fc"
    assert MICA_THEME.success == "#22c55e"
    assert MICA_THEME.error == "#ef4444"
    assert MICA_THEME.foreground == "#f1f5f9"
    assert MICA_THEME.background == "#0f172a"
    assert MICA_THEME.surface == "#172033"
    assert MICA_THEME.variables["mica-muted"] == "#94a3b8"
    assert MICA_THEME.variables["mica-border"] == "#475569"


@pytest.mark.parametrize("size", [(100, 30), (24, 8)])
def test_shell_mounts_ordered_regions_with_visible_bottom_composer(
    size: tuple[int, int],
) -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert app.theme == "mica"
            assert app.current_theme is MICA_THEME
            transcript = app.query_one("#transcript", VerticalScroll)
            work_status = app.query_one("#work-status", Static)
            composer = app.query_one("#composer", Static)

            assert len(app.query("#transcript")) == 1
            assert len(app.query("#work-status")) == 1
            assert len(app.query("#composer")) == 1
            assert [child.id for child in app.screen.children] == [
                "transcript",
                "work-status",
                "composer",
            ]
            assert transcript.parent is app.screen
            assert work_status.parent is app.screen
            assert composer.parent is app.screen
            assert work_status not in transcript.query("*")
            assert transcript.region.bottom <= work_status.region.y
            assert work_status.region.bottom <= composer.region.y
            assert composer.region.bottom == app.screen.size.height
            assert composer.region.height > 0

    asyncio.run(scenario())


def test_shell_exit_leaves_no_pending_tasks() -> None:
    async def scenario() -> None:
        async with MicaTuiApp().run_test() as pilot:
            await pilot.pause()
        current = asyncio.current_task()
        assert [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ] == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("no_color", "ansi_color"),
    [(True, None), (False, True)],
)
def test_shell_mounts_with_terminal_color_degradation(
    monkeypatch: pytest.MonkeyPatch,
    no_color: bool,
    ansi_color: bool | None,
) -> None:
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    async def scenario() -> None:
        app = MicaTuiApp(ansi_color=ansi_color)
        async with app.run_test(size=(32, 10)) as pilot:
            await pilot.pause()
            if no_color:
                assert app.no_color is True
            else:
                assert app.ansi_color is True
                assert app.native_ansi_color is True
            assert app.query_one("#transcript") is not None
            assert app.query_one("#work-status") is not None
            assert app.query_one("#composer").region.bottom == app.screen.size.height

    asyncio.run(scenario())
