"""P6-01C1 响应式 Mica Logo 测试。"""

from __future__ import annotations

import asyncio

import pytest
from textual.containers import VerticalScroll

from mica_agent.tui import MicaTuiApp
from mica_agent.tui.logo import MicaLogo
from mica_agent.tui.theme import BRAND, BRAND_DEEP, BRAND_LIGHT

_FULL_LOGO = """ ███        ███ 
██████    ██████
██ ████  ████ ██   mica
██   ██████   ██
██     ██     ██
▀              ▀"""


def test_logo_is_first_item_inside_transcript() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            transcript = app.query_one("#transcript", VerticalScroll)
            logo = app.query_one("#mica-logo", MicaLogo)

            assert len(app.query("#mica-logo")) == 1
            assert logo.parent is transcript
            assert transcript.children[0] is logo
            assert logo.parent is not app.screen
            assert [child.id for child in app.screen.children] == [
                "transcript",
                "work-status",
                "composer",
            ]

    asyncio.run(scenario())


def test_logo_uses_full_compact_and_plain_renderings() -> None:
    async def scenario() -> None:
        expected = {
            (100, 30): (
                "full",
                _FULL_LOGO,
            ),
            (40, 16): (
                "compact",
                " ██    ██ \n██ █  █ ██   mica\n██      ██\n▀        ▀",
            ),
            (24, 8): ("plain", "Mica"),
        }
        for size, (mode, text) in expected.items():
            app = MicaTuiApp()
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                logo = app.query_one("#mica-logo", MicaLogo)
                assert logo.display_mode == mode
                assert logo.render().plain == text
                assert not {"◢", "◣", "◥", "◤"} & set(text)
                assert app.query_one("#composer").region.bottom == size[1]

    asyncio.run(scenario())


def test_full_logo_overflow_stays_inside_scrollable_transcript() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(100, 8)) as pilot:
            await pilot.pause()
            transcript = app.query_one("#transcript", VerticalScroll)
            logo = app.query_one("#mica-logo", MicaLogo)

            assert logo.display_mode == "full"
            assert logo.region.height > transcript.size.height
            assert transcript.max_scroll_y > 0
            assert app.query_one("#composer").region.bottom == 8

    asyncio.run(scenario())


def test_logo_switches_mode_during_terminal_resize_without_duplication() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(100, 30)) as pilot:
            logo = app.query_one("#mica-logo", MicaLogo)
            assert logo.display_mode == "full"

            await pilot.resize_terminal(40, 16)
            await pilot.pause()
            assert logo.display_mode == "compact"
            assert app.query_one("#mica-logo", MicaLogo) is logo

            await pilot.resize_terminal(24, 8)
            await pilot.pause()
            assert logo.display_mode == "plain"
            assert logo.render().plain == "Mica"
            assert len(app.query("#mica-logo")) == 1
            assert app.query_one("#composer").region.bottom == 8

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("size", "no_color", "ansi_color"),
    [((100, 30), True, None), ((40, 16), False, True)],
)
def test_logo_remains_readable_with_color_degradation(
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
    no_color: bool,
    ansi_color: bool | None,
) -> None:
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    async def scenario() -> None:
        app = MicaTuiApp(ansi_color=ansi_color)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            logo = app.query_one("#mica-logo", MicaLogo)
            rendered = logo.render()
            styles = {str(span.style) for span in rendered.spans}

            if no_color:
                assert app.no_color is True
            else:
                assert app.ansi_color is True
                assert app.native_ansi_color is True
            assert "mica" in rendered.plain.lower()
            assert "\x1b" not in rendered.plain
            assert styles == {
                f"bold {BRAND_LIGHT}",
                f"bold {BRAND}",
                f"bold {BRAND_DEEP}",
            }

    asyncio.run(scenario())
