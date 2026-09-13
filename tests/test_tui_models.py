"""共享 TUI 视图模型：快照规范化、校验与安全 repr。

这些契约属于 ``mica_agent.tui.models``，不依赖任何前端实现。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError, fields

import pytest

from mica_agent.context_builder import AgentFileSource, ContextBuildResult
from mica_agent.tui import (
    ReasoningDisplayMode,
    StartupResourceSnapshot,
    TuiSessionSnapshot,
)


def test_reasoning_display_mode_values_are_stable() -> None:
    assert [mode.value for mode in ReasoningDisplayMode] == [
        "full",
        "collapsed",
        "hidden",
    ]
    assert ReasoningDisplayMode.COLLAPSED is ReasoningDisplayMode("collapsed")


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
    with pytest.raises(ValueError) as captured:
        StartupResourceSnapshot(
            agent_sources=(AgentFileSource.GLOBAL,),
            global_agent_path="C:/Users/private/AGENTS.md",
        )
    assert "C:/Users/private" not in str(captured.value)


def test_session_snapshot_is_frozen_redacted_and_strict() -> None:
    snapshot = TuiSessionSnapshot(
        workspace="C:/workspace unicode/项目",
        session_label="session-layout",
        model="loopback-model",
    )

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


def test_shared_tui_models_are_not_top_level_exports() -> None:
    code = """
import sys
import mica_agent
assert "mica_agent.tui" not in sys.modules
assert "textual" not in sys.modules
for name in (
    "StartupResourceSnapshot",
    "TuiSessionSnapshot",
    "ReasoningDisplayMode",
    "MainBufferScreen",
):
    assert not hasattr(mica_agent, name)
    assert name not in mica_agent.__all__
from mica_agent.tui import (
    ReasoningDisplayMode,
    StartupResourceSnapshot,
    TuiSessionSnapshot,
)
assert StartupResourceSnapshot.__name__ == "StartupResourceSnapshot"
assert TuiSessionSnapshot.__name__ == "TuiSessionSnapshot"
assert ReasoningDisplayMode.COLLAPSED.value == "collapsed"
assert "textual" not in sys.modules
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
