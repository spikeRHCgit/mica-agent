"""Mica Textual 外壳的语义化主题令牌。"""

from __future__ import annotations

from textual.theme import Theme

BACKGROUND = "#000000"
SURFACE = "#111111"
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
    min-height: 1;
    overflow-y: auto;
    padding: 0 3;
    border-bottom: solid $mica-border;
    scrollbar-size: 1 1;
    scrollbar-color: $mica-border;
    scrollbar-color-hover: $mica-muted;
    scrollbar-color-active: $mica-muted;
    scrollbar-background: $background;
    scrollbar-background-hover: $background;
    scrollbar-background-active: $background;
}

#mica-logo {
    width: 100%;
    height: auto;
    margin-bottom: 0;
}

#startup-intro {
    width: 100%;
    height: auto;
    margin-bottom: 0;
}

#startup-resources {
    width: 68;
    max-width: 100%;
    min-width: 20;
    height: auto;
    margin-bottom: 0;
}

#startup-ready {
    width: 100%;
    height: auto;
    margin-bottom: 0;
}

#work-status {
    width: 1fr;
    height: 1;
    margin: 0 1;
    padding: 0 1;
    background: $surface;
}

#composer {
    width: 1fr;
    height: auto;
    min-height: 3;
    max-height: 10;
    margin: 0 1;
    padding: 0 1;
    border: solid $mica-border;
    background: $background;
}

#composer:focus {
    border: solid $mica-brand;
}

#runtime-facts {
    width: 1fr;
    height: 2;
    margin: 0 1;
}

#interaction-overlay {
    width: 100%;
    height: auto;
    min-height: 1;
    border-top: solid $mica-brand-light;
    background: $surface;
}
"""

__all__ = ["MICA_THEME", "SHELL_CSS"]
