"""Mica Textual 外壳的语义化主题令牌。"""

from __future__ import annotations

from textual.theme import Theme

BACKGROUND = "#0f172a"
SURFACE = "#172033"
TEXT = "#f1f5f9"
MUTED = "#94a3b8"
BRAND_LIGHT = "#7dd3fc"
BRAND = "#3b82f6"
BRAND_DEEP = "#1d4ed8"
SUCCESS = "#22c55e"
ERROR = "#ef4444"
BORDER = "#475569"

MICA_THEME = Theme(
    name="mica",
    primary=BRAND,
    secondary=BRAND_DEEP,
    accent=BRAND_LIGHT,
    success=SUCCESS,
    error=ERROR,
    foreground=TEXT,
    background=BACKGROUND,
    surface=SURFACE,
    panel=SURFACE,
    dark=True,
    variables={
        "mica-muted": MUTED,
        "mica-border": BORDER,
        "mica-brand-light": BRAND_LIGHT,
        "mica-brand": BRAND,
        "mica-brand-deep": BRAND_DEEP,
    },
)

SHELL_CSS = """
Screen {
    layout: vertical;
    background: $background;
    color: $foreground;
}

#transcript {
    width: 100%;
    height: 1fr;
    overflow-y: auto;
    padding: 0 1;
    border-bottom: solid $mica-border;
}

#mica-logo {
    width: 100%;
    height: auto;
    margin-bottom: 1;
}

#startup-resources {
    width: 100%;
    height: auto;
    margin-bottom: 1;
    padding: 0 1;
    border-left: solid $mica-border;
}

#work-status {
    width: 100%;
    height: 1;
    padding: 0 1;
    background: $surface;
    color: $mica-muted;
}

#composer {
    width: 100%;
    height: 3;
    min-height: 3;
    padding: 0 1;
    border-top: solid $mica-border;
    background: $surface;
}
"""

__all__ = ["MICA_THEME", "SHELL_CSS"]
