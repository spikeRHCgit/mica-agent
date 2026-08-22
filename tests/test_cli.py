from __future__ import annotations

import subprocess
import sys

import pytest


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
