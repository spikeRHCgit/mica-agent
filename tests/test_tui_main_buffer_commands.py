"""本地命令匹配、候选导航与样式窗口。"""

from __future__ import annotations

import pytest

from mica_agent.tui.main_buffer.commands import (
    Command,
    CommandCandidates,
    CommandRegistry,
)
from mica_agent.tui.main_buffer.renderer import Palette, cell_len, strip_ansi


def registry():
    return CommandRegistry(
        (
            Command("/model", "Switch model or set global default", lambda: None),
            Command("/more", "More", lambda: None),
        )
    )


@pytest.mark.parametrize(
    "draft,names",
    [
        ("/", ("/model", "/more")),
        ("/M", ("/model", "/more")),
        ("/MoD", ("/model",)),
        ("/model", ("/model",)),
        ("/model extra", ()),
        ("/xyz", ()),
        ("model", ()),
        (" /m", ()),
        ("/m\n", ()),
        ("/m\r", ()),
    ],
)
def test_registry_prefix_matches_in_registration_order(draft, names):
    assert tuple(item.name for item in registry().candidates(draft)) == names


@pytest.mark.parametrize(
    "draft,name",
    [
        (" /MODEL \n", "/model"),
        ("/more", "/more"),
        ("/m", None),
        ("/model extra", None),
        ("", None),
    ],
)
def test_registry_exact_strips_outer_whitespace_only(draft, name):
    result = registry().exact(draft)
    assert (None if result is None else result.name) == name


def test_candidate_filter_dismiss_and_draft_change_preserve_input():
    candidates = CommandCandidates(registry())
    draft = "/"
    candidates.sync(draft)
    assert candidates.visible and candidates.selected.name == "/model"
    candidates.move(1)
    assert candidates.selected.name == "/more"
    candidates.dismiss()
    assert not candidates.visible and candidates.selected is None
    candidates.sync(draft)
    assert not candidates.visible and draft == "/"
    candidates.sync("/mod")
    assert candidates.visible and candidates.selected.name == "/model"
    candidates.sync("/no-match")
    assert not candidates.visible
    candidates.sync("/m\nlong paste")
    assert not candidates.visible


def test_candidate_layout_aligns_names_and_follows_selection_with_five_rows():
    commands = tuple(
        Command("/" + "x" * (index + 1), f"Description {index}", lambda: None)
        for index in range(9)
    )
    candidates = CommandCandidates(CommandRegistry(commands))
    candidates.sync("/")
    palette = Palette(True)
    first = candidates.lines(80, palette)
    assert len(first) == 5
    assert first[0] == palette.paint("  › /x            Description 0", "interaction")
    assert first[1] == palette.paint("    /xx           Description 1", "muted")
    description_columns = [strip_ansi(line).index("Description") for line in first]
    assert len(set(description_columns)) == 1
    for _ in range(8):
        candidates.move(1)
        lines = candidates.lines(80, palette)
        assert len(lines) <= 5
        assert any(
            "› " + candidates.selected.name + " " in strip_ansi(line) for line in lines
        )
    assert "Description 8" in strip_ansi(lines[-1])
    assert all(
        "─" not in strip_ansi(line) and "Select" not in strip_ansi(line)
        for line in lines
    )
    assert all(
        cell_len(strip_ansi(line)) <= 12 for line in candidates.lines(12, palette)
    )
