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
    home: Path
    wheel: Path
    sdist: Path


def isolated_environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for variable in "PYTHONPATH", "PYTHONHOME":
        environment.pop(variable, None)
    environment.update(
        HOME=str(home),
        USERPROFILE=str(home),
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return environment


def run_command(
    command: list[str | Path], *, cwd: Path, home: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=isolated_environment(home) if home is not None else None,
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

    home = tmp_path_factory.mktemp("home")
    environment = tmp_path_factory.mktemp("environment") / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    mica = scripts / ("mica.exe" if os.name == "nt" else "mica")

    install = run_command(
        ["uv", "pip", "install", "--offline", "--no-deps", "--python", python, wheel],
        cwd=build_dir,
        home=home,
    )
    assert install.returncode == 0, install.stderr
    assert mica.is_file()

    return InstalledDistribution(
        python=python,
        mica=mica,
        outside_repository=tmp_path_factory.mktemp("outside-repository"),
        home=home,
        wheel=wheel,
        sdist=sdist,
    )


def run_installed(
    installed_distribution: InstalledDistribution, command: list[str | Path]
) -> subprocess.CompletedProcess[str]:
    return run_command(
        command,
        cwd=installed_distribution.outside_repository,
        home=installed_distribution.home,
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


def test_installed_distribution_runtime_dependencies_are_only_httpx(
    installed_distribution: InstalledDistribution,
) -> None:
    result = run_installed(
        installed_distribution,
        [
            installed_distribution.python,
            "-c",
            "import importlib.metadata; print(importlib.metadata.requires('mica-agent'))",
        ],
    )

    assert result.returncode == 0
    assert result.stdout == "['httpx>=0.28.1']\n"
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("arguments", "returncode", "stdout", "stderr", "description"),
    [
        ((), 0, "usage: mica", "", None),
        (("--help",), 0, "usage: mica", "", "A lightweight agent foundation"),
        (("--version",), 0, "mica 0.1.0\n", "", None),
        (("--invalid",), 2, "", "unrecognized arguments", None),
    ],
)
def test_installed_public_entrypoints_match_contract(
    installed_distribution: InstalledDistribution,
    arguments: tuple[str, ...],
    returncode: int,
    stdout: str,
    stderr: str,
    description: str | None,
) -> None:
    console = run_installed(
        installed_distribution, [installed_distribution.mica, *arguments]
    )
    module = run_installed(
        installed_distribution,
        [installed_distribution.python, "-m", "mica_agent", *arguments],
    )

    for result in console, module:
        assert result.returncode == returncode
        if arguments == ("--version",):
            assert result.stdout == stdout
        elif stdout:
            assert stdout in result.stdout
        else:
            assert result.stdout == ""
        if description is not None:
            assert description in result.stdout
        if stderr:
            assert stderr in result.stderr
        else:
            assert result.stderr == ""
    assert (console.returncode, console.stdout, console.stderr) == (
        module.returncode,
        module.stdout,
        module.stderr,
    )


def test_installed_import_is_silent_side_effect_free_and_outside_repository(
    installed_distribution: InstalledDistribution,
) -> None:
    before_workspace = {
        path.relative_to(installed_distribution.outside_repository)
        for path in installed_distribution.outside_repository.rglob("*")
    }
    before_home = {
        path.relative_to(installed_distribution.home)
        for path in installed_distribution.home.rglob("*")
    }
    repository = str(PROJECT_ROOT).replace("\\", "/")
    environment = str(installed_distribution.python.parent.parent).replace("\\", "/")
    code = (
        "import os\n"
        "import sys\n"
        "import threading\n"
        f"repository = {repository!r}\n"
        f"environment = {environment!r}\n"
        "threads_before = {thread.ident for thread in threading.enumerate()}\n"
        "def reject_side_effects(event, arguments):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen'}:\n"
        "        raise RuntimeError(f'prohibited import side effect: {event}')\n"
        "    if event == 'open' and any(flag in str(arguments[1]) for flag in ('w', 'a', 'x', '+')):\n"
        "        raise RuntimeError('attempted file modification')\n"
        "    if event == 'open' and str(arguments[0]).replace('\\\\', '/').startswith(repository):\n"
        "        raise RuntimeError('attempted repository access')\n"
        "sys.addaudithook(reject_side_effects)\n"
        "import mica_agent\n"
        "module_file = str(mica_agent.__file__).replace('\\\\', '/')\n"
        "assert module_file.startswith(environment), module_file\n"
        "assert not module_file.startswith(repository), module_file\n"
        "assert {thread.ident for thread in threading.enumerate()} == threads_before\n"
        "assert not os.path.exists(os.path.join(os.environ['HOME'], '.mica'))\n"
    )
    result = run_installed(
        installed_distribution, [installed_distribution.python, "-c", code]
    )
    after_workspace = {
        path.relative_to(installed_distribution.outside_repository)
        for path in installed_distribution.outside_repository.rglob("*")
    }
    after_home = {
        path.relative_to(installed_distribution.home)
        for path in installed_distribution.home.rglob("*")
    }

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert after_workspace == before_workspace
    assert after_home == before_home
