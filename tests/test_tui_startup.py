"""P6-01C2 只读启动资源区测试。"""

from __future__ import annotations

import asyncio
import builtins
import pathlib
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields

import pytest
from rich.text import Text
from textual.containers import VerticalScroll

from mica_agent.context_builder import AgentFileSource, ContextBuildResult
from mica_agent.tui import MicaTuiApp, StartupResourceSnapshot
from mica_agent.tui.startup import StartupResourcesView


def test_startup_snapshot_defaults_are_immutable() -> None:
    snapshot = StartupResourceSnapshot()

    assert snapshot.system_prompt_name is None
    assert snapshot.agent_sources == ()
    assert snapshot.tool_names == ()
    assert snapshot.skill_names == ()
    assert snapshot.extension_names == ()
    assert not hasattr(snapshot, "__dict__")
    with pytest.raises(FrozenInstanceError):
        snapshot.system_prompt_name = "changed"  # type: ignore[misc]


def test_startup_snapshot_copies_lists_and_preserves_injected_order() -> None:
    sources = [AgentFileSource.GLOBAL, AgentFileSource.WORKSPACE]
    tools = ["ask", "bash", "edit", "read", "write"]
    skills = ["safe-skill"]
    extensions = ["safe-extension"]

    snapshot = StartupResourceSnapshot(
        system_prompt_name="Mica built-in system prompt",
        agent_sources=sources,  # type: ignore[arg-type]
        tool_names=tools,  # type: ignore[arg-type]
        skill_names=skills,  # type: ignore[arg-type]
        extension_names=extensions,  # type: ignore[arg-type]
    )
    sources.clear()
    tools.reverse()
    skills.append("later")
    extensions.clear()

    assert snapshot == StartupResourceSnapshot(
        system_prompt_name="Mica built-in system prompt",
        agent_sources=(AgentFileSource.GLOBAL, AgentFileSource.WORKSPACE),
        tool_names=("ask", "bash", "edit", "read", "write"),
        skill_names=("safe-skill",),
        extension_names=("safe-extension",),
    )


def test_startup_snapshot_validates_agent_sources_with_fixed_errors() -> None:
    invalid_cases = (
        ("global", TypeError, "agent_sources must be a tuple or list"),
        (
            ("C:/private/AGENTS.md",),
            TypeError,
            "agent_sources must contain only AgentFileSource",
        ),
        (
            (AgentFileSource.GLOBAL, AgentFileSource.GLOBAL),
            ValueError,
            "agent_sources must be unique and ordered GLOBAL, WORKSPACE",
        ),
        (
            (AgentFileSource.WORKSPACE, AgentFileSource.GLOBAL),
            ValueError,
            "agent_sources must be unique and ordered GLOBAL, WORKSPACE",
        ),
    )

    for value, exception, message in invalid_cases:
        with pytest.raises(exception) as captured:
            StartupResourceSnapshot(agent_sources=value)  # type: ignore[arg-type]
        assert str(captured.value) == message
        assert "C:/private" not in str(captured.value)


@pytest.mark.parametrize(
    ("field_name", "value", "exception", "message"),
    [
        (
            "system_prompt_name",
            42,
            TypeError,
            "system_prompt_name must be a string or None",
        ),
        (
            "system_prompt_name",
            "  ",
            ValueError,
            "system_prompt_name must be a safe single-line non-empty string",
        ),
        (
            "system_prompt_name",
            "C:/private/system.md",
            ValueError,
            "system_prompt_name must be a safe single-line non-empty string",
        ),
        (
            "tool_names",
            "bash",
            TypeError,
            "tool_names must be a tuple or list",
        ),
        (
            "tool_names",
            (object(),),
            TypeError,
            "tool_names must contain only strings",
        ),
        (
            "skill_names",
            ("safe\nC:/private",),
            ValueError,
            "skill_names must contain safe single-line non-empty strings",
        ),
        (
            "skill_names",
            ("safe\u2028C:/private",),
            ValueError,
            "skill_names must contain safe single-line non-empty strings",
        ),
        (
            "extension_names",
            ("\x1b[31msecret",),
            ValueError,
            "extension_names must contain safe single-line non-empty strings",
        ),
        (
            "extension_names",
            ("\t",),
            ValueError,
            "extension_names must contain safe single-line non-empty strings",
        ),
    ],
)
def test_startup_snapshot_rejects_unsafe_names_without_echoing_values(
    field_name: str,
    value: object,
    exception: type[Exception],
    message: str,
) -> None:
    with pytest.raises(exception) as captured:
        StartupResourceSnapshot(**{field_name: value})

    error = str(captured.value)
    assert error == message
    for forbidden in ("C:/private", "secret", "object at", "Traceback"):
        assert forbidden not in error


def test_startup_snapshot_consumes_context_loaded_sources_without_other_state() -> None:
    result = ContextBuildResult(
        messages=(),
        diagnostics=(),
        loaded_sources=(AgentFileSource.GLOBAL, AgentFileSource.WORKSPACE),
    )

    snapshot = StartupResourceSnapshot(agent_sources=result.loaded_sources)

    assert snapshot.agent_sources == result.loaded_sources
    assert tuple(field.name for field in fields(snapshot)) == (
        "system_prompt_name",
        "agent_sources",
        "global_agent_path",
        "workspace_agent_path",
        "tool_names",
        "skill_names",
        "extension_names",
    )
    with pytest.raises(ValueError):
        StartupResourceSnapshot(
            agent_sources=(AgentFileSource.GLOBAL,),
            global_agent_path="C:/Users/private/AGENTS.md",
        )


def test_startup_view_is_second_item_and_renders_only_injected_facts() -> None:
    async def scenario() -> None:
        snapshot = StartupResourceSnapshot(
            system_prompt_name="Mica built-in system prompt",
            agent_sources=(AgentFileSource.GLOBAL, AgentFileSource.WORKSPACE),
            global_agent_path="~/.mica/AGENTS.md",
            workspace_agent_path="./AGENTS.md",
            tool_names=("ask", "bash", "edit", "read", "write"),
            skill_names=(),
            extension_names=(),
        )
        app = MicaTuiApp(startup_resources=snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            transcript = app.query_one("#transcript", VerticalScroll)
            startup = app.query_one("#startup-resources")
            rendered = startup.render()

            assert [child.id for child in transcript.children] == [
                "mica-logo",
                "startup-intro",
                "startup-resources",
                "startup-ready",
            ]
            assert transcript.children[2] is startup
            assert startup.parent is transcript
            assert startup.parent is not app.screen
            assert [child.id for child in app.screen.children] == [
                "transcript",
                "work-status",
                "composer",
                "runtime-facts",
            ]
            assert isinstance(rendered, Text)
            assert rendered.plain == (
                "  System prompt: Mica built-in system prompt\n"
                "  Global AGENTS.md: ~/.mica/AGENTS.md\n"
                "  Workspace AGENTS.md: ./AGENTS.md\n"
                "Skills ────────\nnone\n"
                "Extensions ────────\nnone\n"
                "Tools ────────\n5 available: ask, bash, edit, read, write"
            )

    asyncio.run(scenario())


def test_startup_view_treats_names_as_plain_text() -> None:
    async def scenario() -> None:
        snapshot = StartupResourceSnapshot(
            system_prompt_name="[bold red]prompt[/]",
            tool_names=("[green]ask[/]", "<not markup>"),
        )
        app = MicaTuiApp(startup_resources=snapshot)
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            rendered = app.query_one("#startup-resources").render()

            assert "[bold red]prompt[/]" in rendered.plain
            assert "[green]ask[/], <not markup>" in rendered.plain
            assert "\x1b" not in rendered.plain

    asyncio.run(scenario())


def test_app_normalizes_none_to_empty_snapshot_and_rejects_other_inputs() -> None:
    explicit = MicaTuiApp(startup_resources=None)
    compatible = MicaTuiApp()

    assert explicit.startup_resources == StartupResourceSnapshot()
    assert compatible.startup_resources == StartupResourceSnapshot()
    assert explicit.startup_resources is not compatible.startup_resources
    with pytest.raises(TypeError) as captured:
        MicaTuiApp(startup_resources=object())  # type: ignore[arg-type]
    assert str(captured.value) == (
        "startup_resources must be a StartupResourceSnapshot or None"
    )


def test_empty_snapshot_renders_none_without_inventing_capabilities() -> None:
    async def scenario() -> None:
        app = MicaTuiApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            startup = app.query_one("#startup-resources", StartupResourcesView)

            assert startup.render().plain == (
                "  System prompt: none\n"
                "  Global AGENTS.md: none\n"
                "  Workspace AGENTS.md: none\n"
                "Skills ────────\nnone\n"
                "Extensions ────────\nnone\n"
                "Tools ────────\n0 available: none"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("size", "no_color", "ansi_color"),
    [((100, 30), True, None), ((24, 8), False, True)],
)
def test_startup_view_remains_readable_and_composer_visible(
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
        snapshot = StartupResourceSnapshot(
            system_prompt_name="Mica built-in system prompt",
            agent_sources=(AgentFileSource.GLOBAL, AgentFileSource.WORKSPACE),
            tool_names=("ask", "bash", "edit", "read", "write"),
        )
        app = MicaTuiApp(
            startup_resources=snapshot,
            ansi_color=ansi_color,
        )
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            startup = app.query_one("#startup-resources", StartupResourcesView)
            transcript = app.query_one("#transcript", VerticalScroll)
            rendered = startup.render().plain

            if no_color:
                assert app.no_color is True
            else:
                assert app.ansi_color is True
                assert app.native_ansi_color is True
            assert startup.border_title.strip() == "Context"
            assert "Global AGENTS.md" in rendered
            assert "Workspace AGENTS.md" in rendered
            if size == (100, 30):
                assert "ask, bash, edit, read, write" in rendered
            assert startup.region.width <= transcript.region.width
            assert app.query_one("#composer").region.bottom <= size[1]
            visible_children = [child for child in app.screen.children if child.display]
            assert visible_children[-1].region.bottom == size[1]

    asyncio.run(scenario())


def test_startup_overflow_scrolls_inside_transcript() -> None:
    async def scenario() -> None:
        snapshot = StartupResourceSnapshot(
            system_prompt_name="Mica built-in system prompt",
            agent_sources=(AgentFileSource.GLOBAL, AgentFileSource.WORKSPACE),
            tool_names=("ask", "bash", "edit", "read", "write"),
            skill_names=("skill-one", "skill-two"),
            extension_names=("extension-one", "extension-two"),
        )
        app = MicaTuiApp(startup_resources=snapshot)
        async with app.run_test(size=(24, 8)) as pilot:
            await pilot.pause()
            transcript = app.query_one("#transcript", VerticalScroll)
            startup = app.query_one("#startup-resources", StartupResourcesView)

            assert startup.region.height > 0
            assert transcript.max_scroll_y > 0
            assert startup.parent is transcript
            assert app.query_one("#composer").region.bottom <= 8
            assert app.query_one("#transcript").region.height >= 1

    asyncio.run(scenario())


def test_tui_import_and_mount_do_not_load_business_runtime() -> None:
    code = """
import asyncio
import sys
from mica_agent.context_builder import AgentFileSource
from mica_agent.tui import MicaTuiApp, StartupResourceSnapshot

snapshot = StartupResourceSnapshot(
    agent_sources=(AgentFileSource.GLOBAL, AgentFileSource.WORKSPACE),
    tool_names=("ask", "bash"),
)

async def main():
    app = MicaTuiApp(startup_resources=snapshot)
    async with app.run_test(size=(40, 12)) as pilot:
        await pilot.pause()
        assert app.query_one("#startup-resources") is not None

asyncio.run(main())
for name in (
    "mica_agent.app",
    "mica_agent.engine",
    "mica_agent.cli",
):
    assert name not in sys.modules, name
assert not any(
    name == "mica_agent.providers" or name.startswith("mica_agent.providers.")
    for name in sys.modules
)
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


def test_startup_snapshot_is_not_a_top_level_export() -> None:
    code = """
import sys
import mica_agent
assert "mica_agent.tui" not in sys.modules
assert not hasattr(mica_agent, "StartupResourceSnapshot")
assert "StartupResourceSnapshot" not in mica_agent.__all__
from mica_agent.tui import StartupResourceSnapshot
assert StartupResourceSnapshot.__name__ == "StartupResourceSnapshot"
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


def test_startup_mount_does_not_read_files_or_create_runtime_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked(*_: object, **__: object) -> None:
        raise AssertionError("prohibited startup side effect")

    monkeypatch.setattr(builtins, "open", blocked)
    monkeypatch.setattr(pathlib.Path, "open", blocked)
    monkeypatch.setattr(pathlib.Path, "home", blocked)
    monkeypatch.setattr(pathlib.Path, "cwd", blocked)
    monkeypatch.setattr(pathlib.Path, "mkdir", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)

    async def scenario() -> None:
        current = asyncio.current_task()
        app = MicaTuiApp(startup_resources=StartupResourceSnapshot())
        async with app.run_test(size=(40, 12)) as pilot:
            await pilot.pause()
            assert app.query_one("#startup-resources") is not None
        assert [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ] == []

    asyncio.run(scenario())
