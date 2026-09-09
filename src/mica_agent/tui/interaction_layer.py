"""Unique bottom overlay that temporarily replaces the ordinary control row."""

from __future__ import annotations

from textual.containers import VerticalScroll
from textual.events import Resize


class InteractionOverlay(VerticalScroll):
    """One App-lifetime overlay for a single active Ask or permission view."""

    can_focus = False
    DEFAULT_CSS = """
    InteractionOverlay {
        width: 100%;
        height: auto;
        min-height: 1;
        overflow-y: auto;
        background: $surface;
        color: $foreground;
        scrollbar-size: 1 1;
        scrollbar-color: $mica-border;
        scrollbar-background: $background;
    }
    """

    def on_mount(self) -> None:
        self._sync_max_height()

    def on_resize(self, _event: Resize) -> None:
        self._sync_max_height()

    def _sync_max_height(self) -> None:
        if self.app is None:
            return
        self.styles.max_height = max(1, self.app.size.height - 1)

    def __repr__(self) -> str:
        return "<InteractionOverlay>"


__all__ = ["InteractionOverlay"]
