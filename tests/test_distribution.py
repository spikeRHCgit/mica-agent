from __future__ import annotations

import os
import subprocess
import tarfile
import venv
import zipfile
from collections.abc import Callable
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
class BuiltDistribution:
    wheel: Path
    sdist: Path


@dataclass(frozen=True)
class InstalledDistribution:
    python: Path
    mica: Path
    outside_repository: Path
    home: Path


EntryFactory = Callable[[InstalledDistribution], list[str | Path]]


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
    command: list[str | Path],
    *,
    cwd: Path,
    home: Path | None = None,
    environment: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    resolved = isolated_environment(home) if home is not None else None
    if environment is not None:
        resolved = environment
    return subprocess.run(
        command,
        cwd=cwd,
        env=resolved,
        capture_output=True,
        text=True,
        check=False,
        input=input_text,
    )


@pytest.fixture(scope="session")
def built_distribution(
    tmp_path_factory: pytest.TempPathFactory,
) -> BuiltDistribution:
    """构建产物只生成一次，供裸安装与完整安装两个 fixture 复用。"""
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
    return BuiltDistribution(wheel=wheel, sdist=sdist)


def create_venv(root: Path) -> tuple[Path, Path, Path]:
    """创建全新 Python 3.11 venv，返回 (environment, python, mica 入口)。"""
    environment = root / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    mica = scripts / ("mica.exe" if os.name == "nt" else "mica")
    return environment, python, mica


@pytest.fixture(scope="session")
def bare_distribution(
    tmp_path_factory: pytest.TempPathFactory,
    built_distribution: BuiltDistribution,
) -> InstalledDistribution:
    """裸安装环境：只装 Mica wheel，不安装任何运行依赖（含 httpx）。"""
    root = tmp_path_factory.mktemp("bare-environment")
    home = tmp_path_factory.mktemp("bare-home")
    environment, python, mica = create_venv(root)

    install = run_command(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            python,
            built_distribution.wheel,
        ],
        cwd=root,
        home=home,
    )
    assert install.returncode == 0, install.stderr
    assert mica.is_file()

    missing_httpx = run_command(
        [python, "-c", "import httpx"],
        cwd=root,
        home=home,
    )
    assert missing_httpx.returncode != 0
    assert "No module named 'httpx'" in missing_httpx.stderr

    return InstalledDistribution(
        python=python,
        mica=mica,
        outside_repository=tmp_path_factory.mktemp("bare-outside-repository"),
        home=home,
    )


@pytest.fixture(scope="session")
def full_distribution(
    tmp_path_factory: pytest.TempPathFactory,
    built_distribution: BuiltDistribution,
) -> InstalledDistribution:
    """完整安装环境：uv 按 wheel 元数据解析并安装全部运行依赖。"""
    root = tmp_path_factory.mktemp("full-environment")
    home = tmp_path_factory.mktemp("full-home")
    environment, python, mica = create_venv(root)

    install = run_command(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            python,
            built_distribution.wheel,
        ],
        cwd=root,
        home=home,
    )
    assert install.returncode == 0, install.stderr
    assert mica.is_file()

    has_httpx = run_command(
        [python, "-c", "import httpx"],
        cwd=root,
        home=home,
    )
    assert has_httpx.returncode == 0, has_httpx.stderr

    return InstalledDistribution(
        python=python,
        mica=mica,
        outside_repository=tmp_path_factory.mktemp("full-outside-repository"),
        home=home,
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
    built_distribution: BuiltDistribution,
) -> None:
    with zipfile.ZipFile(built_distribution.wheel) as wheel:
        members = set(wheel.namelist())

    assert members
    assert {member.split("/", maxsplit=1)[0] for member in members} == {
        "mica_agent",
        f"mica_agent-{VERSION}.dist-info",
    }


def test_sdist_excludes_genericagent_implementation(
    built_distribution: BuiltDistribution,
) -> None:
    with tarfile.open(built_distribution.sdist) as sdist:
        members = [Path(member.name) for member in sdist.getmembers()]

    assert members
    assert all(member.parts[0] == PACKAGE_ROOT for member in members)
    packaged_paths = [member.parts[1:] for member in members if len(member.parts) > 1]
    assert not any(
        parts and parts[0] in PROHIBITED_SDIST_PATHS for parts in packaged_paths
    )


def test_installed_distribution_runtime_dependencies_are_only_httpx(
    bare_distribution: InstalledDistribution,
) -> None:
    result = run_installed(
        bare_distribution,
        [
            bare_distribution.python,
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
    bare_distribution: InstalledDistribution,
    arguments: tuple[str, ...],
    returncode: int,
    stdout: str,
    stderr: str,
    description: str | None,
) -> None:
    console = run_installed(bare_distribution, [bare_distribution.mica, *arguments])
    module = run_installed(
        bare_distribution,
        [bare_distribution.python, "-m", "mica_agent", *arguments],
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
    bare_distribution: InstalledDistribution,
) -> None:
    before_workspace = {
        path.relative_to(bare_distribution.outside_repository)
        for path in bare_distribution.outside_repository.rglob("*")
    }
    before_home = {
        path.relative_to(bare_distribution.home)
        for path in bare_distribution.home.rglob("*")
    }
    repository = str(PROJECT_ROOT).replace("\\", "/")
    environment = str(bare_distribution.python.parent.parent).replace("\\", "/")
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
    result = run_installed(bare_distribution, [bare_distribution.python, "-c", code])
    after_workspace = {
        path.relative_to(bare_distribution.outside_repository)
        for path in bare_distribution.outside_repository.rglob("*")
    }
    after_home = {
        path.relative_to(bare_distribution.home)
        for path in bare_distribution.home.rglob("*")
    }

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert after_workspace == before_workspace
    assert after_home == before_home


FAKE_CLI_ENVIRONMENT = {
    "MICA_API_URL": "https://api.example.test/v1",
    "MICA_API_KEY": "sk-dummy-test-key",
    "MICA_MODEL": "test-model",
}


def cli_environment(home: Path, *, fake_credentials: bool) -> dict[str, str]:
    """隔离 CLI 环境：清除全部 MICA_*，可选注入假的非敏感连接参数。"""
    environment = isolated_environment(home)
    for variable in FAKE_CLI_ENVIRONMENT:
        environment.pop(variable, None)
    if fake_credentials:
        environment.update(FAKE_CLI_ENVIRONMENT)
    return environment


def assert_no_file_side_effects(
    root: Path, before: set[Path], after: set[Path]
) -> None:
    assert after == before, f"unexpected file side effects under {root}"


def snapshot_files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*")}


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(lambda d: [d.mica, "cli"], id="console"),
        pytest.param(lambda d: [d.python, "-m", "mica_agent", "cli"], id="module"),
    ],
)
def test_installed_cli_exits_cleanly_on_blank_input_eof(
    full_distribution: InstalledDistribution,
    entry: EntryFactory,
) -> None:
    """完整安装态 `mica cli`：空白输入 + EOF 后退出码 0，无输出、无副作用。"""
    dist = full_distribution
    command = entry(dist)
    before_workspace = snapshot_files(dist.outside_repository)
    before_home = snapshot_files(dist.home)

    result = run_command(
        command,
        cwd=dist.outside_repository,
        environment=cli_environment(dist.home, fake_credentials=True),
        input_text="\n   \n\t\n",
    )

    assert_no_file_side_effects(
        dist.outside_repository,
        before_workspace,
        snapshot_files(dist.outside_repository),
    )
    assert_no_file_side_effects(dist.home, before_home, snapshot_files(dist.home))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""  # 无 traceback、无诊断
    assert "Traceback" not in result.stderr


def test_installed_cli_missing_environment_variables_exit_two(
    full_distribution: InstalledDistribution,
) -> None:
    """完整安装态 `mica cli` 缺环境变量：两入口各执行一次，退出码 2、诊断一致。"""
    dist = full_distribution
    console: EntryFactory = lambda d: [d.mica, "cli"]
    module: EntryFactory = lambda d: [d.python, "-m", "mica_agent", "cli"]
    results = [
        run_command(
            console(dist),
            cwd=dist.outside_repository,
            environment=cli_environment(dist.home, fake_credentials=False),
        ),
        run_command(
            module(dist),
            cwd=dist.outside_repository,
            environment=cli_environment(dist.home, fake_credentials=False),
        ),
    ]

    for result in results:
        assert result.returncode == 2, result.stderr
        assert result.stdout == ""
        for variable in FAKE_CLI_ENVIRONMENT:
            assert variable in result.stderr
    assert results[0].stderr == results[1].stderr  # 两入口诊断一致
