"""P6-10A 启动首屏布局、固定控制区和事实快照测试。"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from mica_agent.context_builder import AgentFileSource
from mica_agent.tui import MicaComposer, MicaTuiApp
from mica_agent.tui.models import StartupResourceSnapshot, TuiSessionSnapshot
from mica_agent.tui.transcript import ConversationTranscript


def make_snapshots() -> tuple[StartupResourceSnapshot, TuiSessionSnapshot]:
    return (
        StartupResourceSnapshot(
            system_prompt_name="Mica built-in system prompt",
            agent_sources=(AgentFileSource.GLOBAL, AgentFileSource.WORKSPACE),
            global_agent_path="~/.mica/AGENTS.md",
            workspace_agent_path="./AGENTS.md",
            tool_names=("ask", "bash", "edit", "read", "write"),
        ),
        TuiSessionSnapshot(
            workspace="C:/workspace unicode/项目",
            session_label="session-layout",
            model="loopback-model",
        ),
    )


def test_session_snapshot_is_frozen_redacted_and_strict() -> None:
    _startup, snapshot = make_snapshots()

    assert repr(snapshot) == "TuiSessionSnapshot(<redacted>)"
    assert not hasattr(snapshot, "__dict__")
    with pytest.raises(FrozenInstanceError):
        snapshot.model = "changed"  # type: ignore[misc]
    for kwargs in (
        {"workspace": "private\npath"},
        {"session_label": "private\nlabel"},
        {"model": "\x1b[31msecret"},
    ):
        with pytest.raises(ValueError) as captured:
            TuiSessionSnapshot(**kwargs)
        assert str(captured.value) == "invalid TUI session snapshot"
        assert "private" not in str(captured.value)
        assert "secret" not in str(captured.value)


def test_facts_keep_long_unicode_values_bounded_and_identifiable() -> None:
    async def scenario() -> None:
        from mica_agent.tui.bottom import TuiFactsView

        snapshot = TuiSessionSnapshot(
            workspace="C:/Users/测试用户/非常长的项目目录/可识别工作区",
            session_label="session-with-a-very-long-unicode-id-项目",
            model="provider/非常长的模型名称-with-details",
        )
        for size in ((110, 36), (80, 24), (40, 16), (24, 8)):
            app = MicaTuiApp(session_snapshot=snapshot)
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                facts = app.query_one("#runtime-facts", TuiFactsView)
                rendered = facts.render().plain
                assert facts.region.height == 2
                assert facts.region.bottom <= size[1]
                assert "工作区" in rendered
                assert "sessions:" in rendered
                assert "W:" not in rendered and "M:" not in rendered
                if size[0] <= 32:
                    assert "…" in rendered

    asyncio.run(scenario())


def test_wide_new_session_starts_at_top_with_complete_control_hierarchy() -> None:
    async def scenario() -> None:
        startup, session = make_snapshots()
        app = MicaTuiApp(
            startup_resources=startup,
            session_snapshot=session,
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            transcript = app.query_one(ConversationTranscript)
            composer = app.query_one(MicaComposer)

            assert transcript.scroll_y == 0
            assert app.query_one("#mica-logo").region.y <= 1
            assert [child.id for child in transcript.children] == [
                "mica-logo",
                "startup-intro",
                "startup-resources",
                "startup-ready",
            ]
            assert [child.id for child in app.screen.children] == [
                "transcript",
                "work-status",
                "composer",
                "runtime-facts",
            ]
            assert app.query_one("#startup-intro").render().plain == (
                "Mica — lightweight AI coding agent\nReady to build."
            )
            ready = app.query_one("#startup-ready").render().plain
            assert "Mica is ready." in ready
            assert "Hi! What can I help you build today?" not in ready
            assert app.query_one("#startup-resources").region.width <= 84

            assert composer.placeholder == "Type your request or command..."
            composer_line = composer.render_line(0).text
            assert "Enter to send · Shift+Enter for new line" in composer_line
            borders = (
                composer.styles.border_top[0],
                composer.styles.border_right[0],
                composer.styles.border_bottom[0],
                composer.styles.border_left[0],
            )
            assert borders == ("solid", "solid", "solid", "solid")
            assert composer_line.startswith("› ")

            facts = app.query_one("#runtime-facts").render().plain
            assert "sessions: session-layout" in facts
            assert "loopback-model" in facts
            assert "W:" not in facts
            assert "M:" not in facts
            assert "P:" not in facts
            assert "Tools:" not in facts
            assert "0m" not in facts

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(40, 16), (24, 8)])
def test_compact_layout_keeps_transcript_composer_and_facts_visible(
    size: tuple[int, int],
) -> None:
    async def scenario() -> None:
        startup, session = make_snapshots()
        app = MicaTuiApp(
            startup_resources=startup,
            session_snapshot=session,
            ansi_color=False,
        )
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            transcript = app.query_one("#transcript")
            composer = app.query_one(MicaComposer)
            facts = app.query_one("#runtime-facts")

            assert transcript.region.height >= 1
            assert transcript.scroll_y == 0
            assert composer.region.height >= 3
            assert composer.region.bottom <= size[1]
            assert facts.region.bottom == size[1]
            if size == (24, 8):
                assert (
                    app.query_one("#startup-intro").render().plain == "Ready to build."
                )
            assert "sessions:" in facts.render().plain
            assert "W:" not in facts.render().plain
            assert "M:" not in facts.render().plain
            assert "P:" not in facts.render().plain
            assert (
                app.query_one("#startup-resources").region.width
                <= transcript.region.width
            )

    asyncio.run(scenario())
