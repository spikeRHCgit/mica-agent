"""响应终端宽度变化的只读 Mica Logo。"""

from __future__ import annotations

from typing import Literal

from rich.text import Text
from textual.events import Resize
from textual.reactive import reactive
from textual.widgets import Static

from .theme import BRAND, BRAND_DEEP, BRAND_LIGHT

FULL_MIN_WIDTH = 54
COMPACT_MIN_WIDTH = 28

LogoMode = Literal["full", "compact", "plain"]

_FULL_ROWS = (
    (" ███        ███ ", "LLLLL......BBBBB", ""),
    ("██████    ██████", "LLLLLLB..BBBBBDD", ""),
    ("██ ████  ████ ██", "LLBBBBBBDDDDDDDD", "   mica"),
    ("██   ██████   ██", "LL..BBBBBBDD..DD", ""),
    ("██     ██     ██", "LL....BBBB....DD", ""),
    ("▀              ▀", "L......BB......D", ""),
)

_COMPACT_ROWS = (
    (" ██    ██ ", "LLLL..BBBB", ""),
    ("██ █  █ ██", "LLBBBDDDDD", "   mica"),
    ("██      ██", "LL..BB..DD", ""),
    ("▀        ▀", "L........D", ""),
)


class MicaLogo(Static):
    """静态 Logo，根据组件可用宽度切换稳定显示档位。"""

    display_mode: reactive[LogoMode] = reactive("plain", layout=True)

    def on_mount(self) -> None:
        self._update_mode(self.size.width)

    def on_resize(self, event: Resize) -> None:
        self._update_mode(event.size.width)

    def render(self) -> Text:
        if self.display_mode == "plain":
            return Text("Mica")
        rows = _FULL_ROWS if self.display_mode == "full" else _COMPACT_ROWS
        logo = Text()
        for index, (row, facets, label) in enumerate(rows):
            for character, facet in zip(row, facets, strict=True):
                if facet == ".":
                    logo.append(character)
                    continue
                color = (
                    BRAND_LIGHT
                    if facet == "L"
                    else BRAND
                    if facet == "B"
                    else BRAND_DEEP
                )
                logo.append(character, style=f"bold {color}")
            logo.append(label)
            if index < len(rows) - 1:
                logo.append("\n")
        return logo

    def _update_mode(self, width: int) -> None:
        if width >= FULL_MIN_WIDTH:
            mode, height = "full", 6
        elif width >= COMPACT_MIN_WIDTH:
            mode, height = "compact", 4
        else:
            mode, height = "plain", 1
        self.display_mode = mode
        self.styles.height = height


__all__ = ["MicaLogo"]
