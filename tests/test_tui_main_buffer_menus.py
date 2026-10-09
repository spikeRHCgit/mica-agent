"""模型菜单的公共输入、独立标记、颜色与窗口降级。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mica_agent.app import ModelChoice
from mica_agent.session_contracts import SessionSelection
from mica_agent.tui.main_buffer.editor import Key
from mica_agent.tui.main_buffer.menus import FOOTER_CONFIRM, FOOTER_MODEL, ModelMenu
from mica_agent.tui.main_buffer.renderer import Palette, cell_len, strip_ansi


def choice(
    index,
    *,
    current=False,
    default=False,
    provider="A",
    capacity=200000,
    capacity_default=False,
    name=None,
):
    return ModelChoice(
        SessionSelection(provider, f"model-{index}"),
        name or f"model-{index}",
        f"Provider {provider}",
        capacity,
        capacity_default,
        current,
        default,
    )


def rows(menu, *, width=100, color=False, limit=None):
    return menu.panel(width, Palette(color), limit=limit)


def text(menu, **kwargs):
    return "\n".join(strip_ansi(line) for line in rows(menu, **kwargs).lines)


def test_model_menu_layout_separates_navigation_current_default_and_colors():
    first = choice("a", current=True, default=True)
    menu = ModelMenu((first, choice("b"), choice("c", provider="B")))
    palette = Palette(True)
    view = rows(menu, color=True)
    plain = [strip_ansi(line) for line in view.lines]
    assert plain[0] == plain[-1] == "─" * 100
    assert plain[1:4] == [" Select model", " Type to filter...", ""]
    assert plain[view.selected_row].startswith(" › ✓ model-a [Provider A]")
    assert plain[view.selected_row].endswith("Current Default")
    assert " Enter Switch · Ctrl+S Set default · ↑/↓ Move · Esc Close" in plain
    assert "200,000" not in "\n".join(plain)
    assert (
        palette.paint("model-a [Provider A]", "interaction")
        in view.lines[view.selected_row]
    )
    assert palette.paint("✓", "interaction") in view.lines[view.selected_row]
    assert palette.paint("Current", "interaction") in view.lines[view.selected_row]
    assert palette.paint("Default", "muted") in view.lines[view.selected_row]
    assert menu.handle(Key("down")).action == "none"
    moved = rows(menu, color=True)
    current_line = next(line for line in moved.lines if "model-a" in line)
    selected_line = moved.lines[moved.selected_row]
    assert strip_ansi(current_line).startswith("   ✓ ")
    assert strip_ansi(selected_line).startswith(" ›   ")
    assert palette.paint("model-a [Provider A]", "interaction") in current_line
    assert palette.paint("model-b [Provider A]", "muted") in selected_line
    assert palette.paint("›", "interaction") in selected_line
    plain = rows(menu).lines
    assert next(line for line in plain if "model-a" in line).index("✓") == 3
    assert next(line for line in plain if "model-b" in line).index("›") == 1
    assert "Current Default" in next(line for line in plain if "model-a" in line)


def test_model_menu_initial_current_noop_and_unconfigured_first_selection():
    menu = ModelMenu((choice("a"), choice("b", current=True)))
    assert menu.selected.selection == SessionSelection("A", "model-b")
    assert menu.handle(Key("enter")).action == "close"
    empty_current = ModelMenu((choice("a"), choice("b")))
    outcome = empty_current.handle(Key("enter"))
    assert outcome.action == "switch" and outcome.selection == SessionSelection(
        "A", "model-a"
    )


def test_model_menu_filter_name_supplier_space_backspace_and_empty_result():
    menu = ModelMenu(
        (choice("alpha", name="Alpha Model"), choice("beta", provider="B"))
    )
    for character in "pRoViDeR B":
        menu.handle(Key("text", character))
    assert len(menu.filtered) == 1 and menu.selected.model_name == "model-beta"
    assert "Filter: pRoViDeR B" in text(menu)
    assert "Future requests" not in text(menu)
    menu.handle(Key("text", "xyz"))
    assert not menu.filtered and "No matching models" in text(menu)
    assert menu.handle(Key("enter")).action == "none"
    assert menu.handle(Key("ctrl_s")).action == "none"
    for _ in range(len(menu.filter_text)):
        menu.handle(Key("backspace"))
    menu.handle(Key("paste", "ALPHA"))
    assert menu.selected.model_name == "Alpha Model"
    assert menu.handle(Key("escape")).action == "close"


@pytest.mark.parametrize(
    "current_default,target_default",
    [(True, False), (False, True), (True, True), (False, False)],
)
def test_smaller_context_requires_confirmation_and_escape_returns_to_list(
    current_default, target_default
):
    current = choice(
        "a", current=True, capacity=200000, capacity_default=current_default
    )
    target = choice("b", provider="B", capacity=32000, capacity_default=target_default)
    menu = ModelMenu((current, target))
    menu.handle(Key("down"))
    assert "Future requests will send this session's history to Provider B." in text(
        menu
    )
    assert menu.handle(Key("enter")).action == "none"
    warning = f"Context window will shrink: 200,000{' (default)' if current_default else ''} → 32,000{' (default)' if target_default else ''}"
    assert warning in text(menu)
    assert "Existing history may exceed the new limit." in text(menu)
    assert FOOTER_CONFIRM in text(menu)
    assert menu.handle(Key("escape")).action == "none"
    assert "Context window will shrink" not in text(menu) and FOOTER_MODEL in text(menu)
    assert menu.handle(Key("enter")).action == "none"
    outcome = menu.handle(Key("enter"))
    assert outcome.action == "switch" and outcome.selection == target.selection


@pytest.mark.parametrize("capacity", [200000, 400000])
def test_context_not_smaller_switches_without_confirmation(capacity):
    menu = ModelMenu((choice("a", current=True), choice("b", capacity=capacity)))
    menu.handle(Key("down"))
    assert menu.handle(Key("enter")).action == "switch"
    assert "Context window will shrink" not in text(menu)


def test_empty_menu_only_responds_to_escape_and_busy_ignores_edits():
    empty = ModelMenu(())
    assert "No models available" in text(empty) and "Esc Close" in text(empty)
    assert FOOTER_MODEL not in text(empty)
    for name in ("text", "down", "enter", "ctrl_s", "backspace"):
        assert empty.handle(Key(name, "x")).action == "none"
    assert empty.filter_text == "" and empty.handle(Key("escape")).action == "close"
    menu = ModelMenu((choice("a", current=True), choice("b")))
    menu.handle(Key("down"))
    menu.set_busy(True)
    for key in (
        Key("up"),
        Key("text", "x"),
        Key("paste", "y"),
        Key("enter"),
        Key("ctrl_s"),
    ):
        assert menu.handle(key).action == "none"
    assert menu.selected.selection == SessionSelection("A", "model-b")
    assert menu.filter_text == "" and "Switching..." in text(menu)
    assert menu.handle(Key("escape")).action == "close"


def test_choices_refresh_preserves_selection_and_distinct_default_flag():
    choices = (choice("a", current=True, default=True), choice("b"))
    menu = ModelMenu(choices)
    menu.handle(Key("down"))
    assert menu.handle(Key("ctrl_s")).selection == choices[1].selection
    menu.set_choices(
        (replace(choices[0], is_default=False), replace(choices[1], is_default=True))
    )
    menu.set_hint("Saved as global default.")
    assert menu.selected.selection == choices[1].selection
    lines = rows(menu).lines
    assert "Current" in next(line for line in lines if "model-a" in line)
    assert "Default" not in next(line for line in lines if "model-a" in line)
    assert "Default" in next(line for line in lines if "model-b" in line)
    assert "Saved as global default." in text(menu)


@pytest.mark.parametrize("width", [1, 4, 12, 20, 24, 40, 80])
def test_narrow_menu_fits_width_and_preserves_full_markers_when_possible(width):
    menu = ModelMenu(
        (choice("very-long-model-name", current=True, default=True), choice("b"))
    )
    view = rows(menu, width=width)
    assert all(cell_len(strip_ansi(line)) <= width for line in view.lines)
    if width >= 20:
        current = next(line for line in view.lines if "Current" in line)
        assert current.startswith(" › ✓") and current.endswith("Current Default")


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 8, 12])
def test_tall_menu_keeps_navigation_anchor_and_english_window_hints(limit):
    menu = ModelMenu(tuple(choice(index, current=index == 0) for index in range(25)))
    for _ in range(20):
        menu.handle(Key("down"))
    view = rows(menu, width=60, limit=limit)
    assert 1 <= len(view.lines) <= limit
    assert view.lines[view.selected_row].startswith(" ›   model-20")
    assert "上方" not in "\n".join(view.lines) and "下方" not in "\n".join(view.lines)


def test_model_and_supplier_controls_are_sanitized_before_layout_and_history_notice():
    private = "https://fake.example.test/v1 TEST_ONLY_KEY"
    target = replace(
        choice("b", provider="B"),
        model_name=f"\x1b]0;{private}\x07Beta",
        provider_name=f"\x1b]0;{private}\x07Provider B",
    )
    menu = ModelMenu((choice("a", current=True), target))
    menu.handle(Key("down"))
    rendered = text(menu)
    assert "Beta [Provider B]" in rendered
    assert "Future requests will send this session's history to Provider B." in rendered
    for unsafe in ("https://fake.example.test/v1", "TEST_ONLY_KEY", "\x1b", "\x07"):
        assert unsafe not in rendered
