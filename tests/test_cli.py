from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from mica_agent import cli


@pytest.fixture(params=[("mica",), (sys.executable, "-m", "mica_agent")])
def command(request: pytest.FixtureRequest) -> tuple[str, ...]:
    return request.param


def run_module(*arguments: str) -> subprocess.CompletedProcess[str]:
    return run_command((sys.executable, "-m", "mica_agent"), *arguments)


def run_command(
    command: tuple[str, ...], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*command, *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_config_precedes_context_prompt_and_provider_setup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("MICA_API_URL", "MICA_API_KEY", "MICA_MODEL"):
        monkeypatch.delenv(name, raising=False)
    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> object:
        calls.append("startup")
        raise AssertionError("startup work must not run")

    monkeypatch.setattr(cli.ContextBuilder, "build", forbidden)
    monkeypatch.setattr(cli, "build_system_prompt", forbidden)
    monkeypatch.setattr(cli, "run_interactive", forbidden)

    assert cli._main_cli() == 2
    captured = capsys.readouterr()
    assert calls == []
    assert captured.out == ""
    assert captured.err.startswith("mica: missing required environment variables")


def test_main_cli_shuts_down_async_generators_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = False

    async def unfinished_stream():
        nonlocal closed
        try:
            yield "one event"
        finally:
            closed = True

    async def fake_run_interactive(*args: object, **kwargs: object) -> int:
        stream = unfinished_stream()
        assert await anext(stream) == "one event"
        return 0

    monkeypatch.setenv("MICA_API_URL", "https://api.example.test/v1")
    monkeypatch.setenv("MICA_API_KEY", "sk-test-key")
    monkeypatch.setenv("MICA_MODEL", "test-model")
    monkeypatch.setattr(cli, "run_interactive", fake_run_interactive)

    assert cli._main_cli() == 0
    assert closed is True


def test_main_cli_shuts_down_async_generators_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = False

    async def unfinished_stream():
        nonlocal closed
        try:
            yield "one event"
        finally:
            closed = True

    def interrupt_loop() -> None:
        raise KeyboardInterrupt

    async def fake_run_interactive(*args: object, **kwargs: object) -> int:
        stream = unfinished_stream()
        assert await anext(stream) == "one event"
        asyncio.get_running_loop().call_soon(interrupt_loop)
        await asyncio.Event().wait()
        return 0

    monkeypatch.setenv("MICA_API_URL", "https://api.example.test/v1")
    monkeypatch.setenv("MICA_API_KEY", "sk-test-key")
    monkeypatch.setenv("MICA_MODEL", "test-model")
    monkeypatch.setattr(cli, "run_interactive", fake_run_interactive)

    assert cli._main_cli() == 0
    assert closed is True
    with pytest.raises(RuntimeError, match="There is no current event loop"):
        asyncio.get_event_loop()


def test_module_version() -> None:
    result = run_module("--version")

    assert result.returncode == 0
    assert result.stdout == "mica 0.1.0\n"
    assert result.stderr == ""


def test_console_version() -> None:
    result = run_command(("mica",), "--version")

    assert result.returncode == 0
    assert result.stdout == "mica 0.1.0\n"
    assert result.stderr == ""


def test_help_for_both_public_entrypoints(command: tuple[str, ...]) -> None:
    result = run_command(command, "--help")

    assert result.returncode == 0
    assert "usage: mica" in result.stdout
    assert "A lightweight agent foundation" in result.stdout
    assert result.stderr == ""


def test_no_arguments_prints_help(command: tuple[str, ...]) -> None:
    result = run_command(command)

    assert result.returncode == 0
    assert "usage: mica" in result.stdout
    assert result.stderr == ""


def test_invalid_argument_reports_diagnostic(command: tuple[str, ...]) -> None:
    result = run_command(command, "--not-a-real-option")

    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "command",
    [("mica",), (sys.executable, "-m", "mica_agent")],
)
def test_invalid_permission_mode_is_rejected_by_argument_parsing(
    command: tuple[str, ...],
) -> None:
    result = run_command(command, "cli", "--permission-mode", "unsafe")

    assert result.returncode == 2
    assert "invalid choice" in result.stderr
    assert "MICA_API_URL" not in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "command",
    [("mica",), (sys.executable, "-m", "mica_agent")],
)
def test_restore_flags_are_mutually_exclusive(command: tuple[str, ...]) -> None:
    result = run_command(command, "cli", "--continue", "--resume")

    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr
    assert result.stdout == ""


def test_public_entrypoints_have_matching_behavior() -> None:
    for arguments in ((), ("--help",), ("--version",), ("--not-a-real-option",)):
        console = run_command(("mica",), *arguments)
        module = run_module(*arguments)

        assert (console.returncode, console.stdout, console.stderr) == (
            module.returncode,
            module.stdout,
            module.stderr,
        )
