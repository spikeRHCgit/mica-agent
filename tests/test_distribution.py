from __future__ import annotations

import os
import subprocess
import tarfile
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
PACKAGE_ROOT = f"mica_agent-{VERSION}"
PROHIBITED_SDIST_PATHS = {
    "agent_loop.py",
    "agentmain.py",
    "ga.py",
    "llmcore.py",
    "TMWebDriver.py",
    "simphtml.py",
    "ga_cli",
    "frontends",
    "memory",
    "reflect",
    "plugins",
    "assets",
}


@dataclass(frozen=True)
class InstalledDistribution:
    python: Path
    mica: Path
    outside_repository: Path
    wheel: Path
    sdist: Path


def run_command(
    command: list[str | Path], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="session")
def installed_distribution(
    tmp_path_factory: pytest.TempPathFactory,
) -> InstalledDistribution:
    build_dir = tmp_path_factory.mktemp("distribution")
    build = run_command(
        ["uv", "build", "--offline", "--out-dir", build_dir],
        cwd=PROJECT_ROOT,
    )
    assert build.returncode == 0, build.stderr

    wheel = build_dir / f"mica_agent-{VERSION}-py3-none-any.whl"
    sdist = build_dir / f"mica_agent-{VERSION}.tar.gz"
    assert wheel.is_file()
    assert sdist.is_file()

    environment = tmp_path_factory.mktemp("environment") / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    mica = scripts / ("mica.exe" if os.name == "nt" else "mica")

    install = run_command(
        ["uv", "pip", "install", "--offline", "--no-deps", "--python", python, wheel],
        cwd=build_dir,
    )
    assert install.returncode == 0, install.stderr
    assert mica.is_file()

    return InstalledDistribution(
        python=python,
        mica=mica,
        outside_repository=tmp_path_factory.mktemp("outside-repository"),
        wheel=wheel,
        sdist=sdist,
    )


def test_wheel_contains_only_mica_package_and_metadata(
    installed_distribution: InstalledDistribution,
) -> None:
    with zipfile.ZipFile(installed_distribution.wheel) as wheel:
        members = set(wheel.namelist())

    assert members
    assert {member.split("/", maxsplit=1)[0] for member in members} == {
        "mica_agent",
        f"mica_agent-{VERSION}.dist-info",
    }


def test_sdist_excludes_genericagent_implementation(
    installed_distribution: InstalledDistribution,
) -> None:
    with tarfile.open(installed_distribution.sdist) as sdist:
        members = [Path(member.name) for member in sdist.getmembers()]

    assert members
    assert all(member.parts[0] == PACKAGE_ROOT for member in members)
    packaged_paths = [member.parts[1:] for member in members if len(member.parts) > 1]
    assert not any(
        parts and parts[0] in PROHIBITED_SDIST_PATHS for parts in packaged_paths
    )


def test_installed_distribution_has_no_runtime_dependencies(
    installed_distribution: InstalledDistribution,
) -> None:
    result = run_command(
        [
            installed_distribution.python,
            "-c",
            "import importlib.metadata; print(importlib.metadata.requires('mica-agent'))",
        ],
        cwd=installed_distribution.outside_repository,
    )

    assert result.returncode == 0
    assert result.stdout == "None\n"
    assert result.stderr == ""


@pytest.mark.parametrize("arguments", [(), ("--help",), ("--version",), ("--invalid",)])
def test_installed_public_entrypoints_match(
    installed_distribution: InstalledDistribution, arguments: tuple[str, ...]
) -> None:
    console = run_command(
        [installed_distribution.mica, *arguments],
        cwd=installed_distribution.outside_repository,
    )
    module = run_command(
        [installed_distribution.python, "-m", "mica_agent", *arguments],
        cwd=installed_distribution.outside_repository,
    )

    assert (console.returncode, console.stdout, console.stderr) == (
        module.returncode,
        module.stdout,
        module.stderr,
    )


def test_installed_import_is_silent_side_effect_free_and_outside_repository(
    installed_distribution: InstalledDistribution,
) -> None:
    before = {
        path.relative_to(installed_distribution.outside_repository)
        for path in installed_distribution.outside_repository.rglob("*")
    }
    repository = str(PROJECT_ROOT).replace("\\", "/")
    code = (
        "import sys\n"
        f"repository = {repository!r}\n"
        "def reject_repository_access(event, arguments):\n"
        "    if event == 'open' and str(arguments[0]).replace('\\\\', '/').startswith(repository):\n"
        "        raise RuntimeError('attempted repository access')\n"
        "sys.addaudithook(reject_repository_access)\n"
        "import mica_agent\n"
        "assert not str(mica_agent.__file__).replace('\\\\', '/').startswith(repository)\n"
    )
    result = run_command(
        [installed_distribution.python, "-c", code],
        cwd=installed_distribution.outside_repository,
    )
    after = {
        path.relative_to(installed_distribution.outside_repository)
        for path in installed_distribution.outside_repository.rglob("*")
    }

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert after == before
